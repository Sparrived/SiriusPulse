from __future__ import annotations

import base64
import json

import pytest

from sirius_pulse.providers.base import (
    GenerationRequest,
    GenerationResult,
    ToolCall,
    build_chat_completion_payload,
    build_generation_debug_context,
    extract_prompt_cache_usage,
    get_last_generation_usage,
    prepare_openai_compatible_messages,
    resolve_generation_timeout_seconds,
    set_last_generation_usage,
)
from sirius_pulse.providers.openai_compatible import OpenAICompatibleProvider
from sirius_pulse.providers.opencode import (
    DEFAULT_OPENCODE_BASE_URL,
    DEFAULT_OPENCODE_GO_BASE_URL,
    OpenCodeGoProvider,
    OpenCodeProvider,
)
from sirius_pulse.providers.proxy import (
    ProxySettings,
    get_current_proxy,
    httpx_proxy_kwargs,
    load_proxy_settings,
    save_proxy_settings,
    set_current_proxy,
)
from sirius_pulse.providers.response_utils import extract_assistant_text
from sirius_pulse.providers.routing import (
    AutoRoutingProvider,
    ProviderConfig,
    ProviderRegistry,
    _create_provider_instance,
    ensure_provider_platform_supported,
    get_supported_provider_platforms,
    merge_provider_sources,
    normalize_provider_name,
    normalize_provider_type,
    probe_provider_models,
)


@pytest.fixture(autouse=True)
def _reset_proxy_settings():
    """每个用例前后重置进程级代理配置，避免用例间泄漏。"""
    set_current_proxy(ProxySettings())
    yield
    set_current_proxy(ProxySettings())


def test_generation_result_when_tool_calls_are_present_then_reports_tool_call_state():
    result = GenerationResult(
        content="",
        tool_calls=[ToolCall(id="call-1", function_name="lookup", function_arguments='{"q": "x"}')],
    )

    assert result.has_tool_calls is True
    assert result.tool_calls[0].function_name == "lookup"
    assert result.finish_reason == "stop"


def test_generation_usage_when_read_then_clears_thread_local_value():
    set_last_generation_usage({"prompt_tokens": 3})

    assert get_last_generation_usage() == {"prompt_tokens": 3}
    assert get_last_generation_usage() is None


def test_prompt_cache_usage_when_provider_shapes_vary_then_normalizes_hit_and_miss():
    assert extract_prompt_cache_usage(
        {
            "prompt_tokens": 100,
            "prompt_cache_hit_tokens": 70,
            "prompt_cache_miss_tokens": 30,
        },
        prompt_tokens=100,
    ) == {
        "cache_info_available": True,
        "cached_prompt_tokens": 70,
        "uncached_prompt_tokens": 30,
        "cache_creation_prompt_tokens": 0,
    }
    assert (
        extract_prompt_cache_usage(
            {"prompt_tokens_details": {"cached_tokens": 12}},
            prompt_tokens=20,
        )["uncached_prompt_tokens"]
        == 8
    )


def test_chat_payload_when_sampling_params_delegated_then_omits_temperature_and_max_tokens():
    """任务名路由下采样参数由 AMKR 任务定义固定，调用方不能再传。"""
    request = GenerationRequest(
        model="cognition_analyze",
        system_prompt="system",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=50,
        temperature=0.2,
        tools=[{"type": "function", "function": {"name": "lookup"}}],
        tool_choice="auto",
        response_format={"type": "json_object"},
    )

    payload = build_chat_completion_payload(
        request,
        provider_name="openai-compatible",
        delegate_sampling_params=True,
    )

    assert "temperature" not in payload
    assert "max_tokens" not in payload
    # 非采样参数照常透传
    assert payload["model"] == "cognition_analyze"
    assert payload["messages"][0] == {"role": "system", "content": "system"}
    assert payload["messages"][1] == {"role": "user", "content": "hello"}
    assert payload["tools"] == request.tools
    assert payload["tool_choice"] == "auto"
    assert payload["response_format"] == {"type": "json_object"}


def test_chat_payload_when_sampling_not_delegated_then_keeps_temperature_and_max_tokens():
    """普通模型名（非任务）直连时，采样参数仍由本框架决定。"""
    payload = build_chat_completion_payload(
        GenerationRequest(
            model="gpt-4o-mini",
            system_prompt="",
            messages=[],
            temperature=0.9,
            max_tokens=123,
        ),
        provider_name="openai-compatible",
    )

    assert payload["temperature"] == 0.9
    assert payload["max_tokens"] == 123


def test_chat_payload_when_no_vendor_thinking_params_then_never_injects_them():
    """单一 OpenAI-compatible 客户端不再注入任何厂商私有思考参数。"""
    payload = build_chat_completion_payload(
        GenerationRequest(
            model="deepseek-chat",
            system_prompt="",
            messages=[{"role": "user", "content": "hello"}],
            reasoning_effort="low",
        ),
        provider_name="openai-compatible",
    )

    assert "thinking" not in payload
    assert "enable_thinking" not in payload
    assert "reasoning_effort" not in payload


def test_chat_payload_when_opencode_provider_then_does_not_inject_unknown_params():
    payload = build_chat_completion_payload(
        GenerationRequest(
            model="kimi-k3",
            system_prompt="",
            messages=[{"role": "user", "content": "hello"}],
            reasoning_effort="low",
        ),
        provider_name="opencode-go",
    )

    assert "thinking" not in payload
    assert "enable_thinking" not in payload
    assert "reasoning_effort" not in payload


def test_opencode_provider_when_building_url_then_uses_zen_chat_completions_endpoint():
    request = GenerationRequest(model="kimi-k3", system_prompt="", messages=[])

    assert (
        OpenCodeProvider(api_key="sk-opencode")._build_url(request)
        == f"{DEFAULT_OPENCODE_BASE_URL}/chat/completions"
    )
    # 省略 /v1 的 base URL 会被自动补全
    provider = OpenCodeProvider(api_key="sk-opencode", base_url="https://opencode.ai/zen")
    assert provider._build_url(request) == "https://opencode.ai/zen/v1/chat/completions"


def test_opencode_go_provider_when_building_url_then_uses_go_chat_completions_endpoint():
    request = GenerationRequest(model="kimi-k3", system_prompt="", messages=[])

    assert (
        OpenCodeGoProvider(api_key="sk-opencode-go")._build_url(request)
        == f"{DEFAULT_OPENCODE_GO_BASE_URL}/chat/completions"
    )
    # 省略 /v1 的 base URL 会被自动补全
    provider = OpenCodeGoProvider(api_key="sk-opencode-go", base_url="https://opencode.ai/zen/go")
    assert provider._build_url(request) == "https://opencode.ai/zen/go/v1/chat/completions"


def test_provider_platform_when_opencode_types_are_used_then_normalized_and_supported():
    assert normalize_provider_type("opencode") == "opencode"
    assert normalize_provider_type("opencode-zen") == "opencode"
    assert normalize_provider_type("opencode-go") == "opencode-go"
    assert normalize_provider_type("opencode_go") == "opencode-go"
    assert normalize_provider_type("opencodego") == "opencode-go"

    assert ensure_provider_platform_supported("opencode-go") == "opencode-go"

    platforms = get_supported_provider_platforms()
    assert platforms["opencode"]["default_base_url"] == "https://opencode.ai/zen/v1"
    assert platforms["opencode-go"]["default_base_url"] == "https://opencode.ai/zen/go/v1"


def test_provider_factory_when_opencode_configs_used_then_creates_matching_providers():
    zen = _create_provider_instance(
        ProviderConfig(provider_type="opencode", api_key="sk-opencode", base_url="")
    )
    go = _create_provider_instance(
        ProviderConfig(provider_type="opencode-go", api_key="sk-opencode-go", base_url="")
    )

    assert isinstance(zen, OpenCodeProvider)
    assert isinstance(go, OpenCodeGoProvider)
    assert zen._provider_name == "opencode"
    assert go._provider_name == "opencode-go"
    assert zen._base_url == "https://opencode.ai/zen/v1"
    assert go._base_url == "https://opencode.ai/zen/go/v1"


def test_openai_compatible_messages_preserve_tool_call_results():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-bash",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"docker inspect minecraft"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-bash",
            "content": "[Tool result: success]\\ncontainer minecraft is running",
        },
    ]

    payload = build_chat_completion_payload(
        GenerationRequest(model="deepseek-v4-flash", system_prompt="system", messages=messages),
        provider_name="deepseek",
    )
    wire_messages, _ = prepare_openai_compatible_messages(payload["messages"])

    assert wire_messages[1:] == messages


def test_generation_debug_context_when_multimodal_messages_exist_then_counts_parts():
    request = GenerationRequest(
        model="test-model",
        system_prompt="system",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "one"},
                    {"type": "image_url", "image_url": {"url": "x"}},
                ],
            },
            {"role": "assistant", "content": "two"},
        ],
        tools=[{"type": "function"}],
    )

    context = build_generation_debug_context(request, provider_name="test")

    assert context["input_message_count"] == 2
    assert context["multimodal_part_count"] == 2
    assert context["total_message_count"] == 3


def test_prepare_messages_when_local_image_path_is_used_then_converts_to_data_url(tmp_path):
    image_path = tmp_path / "image.jpg"
    image_path.write_bytes(b"image-bytes")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "see"},
                {"type": "image_url", "image_url": {"url": str(image_path)}},
            ],
        }
    ]

    prepared, stats = prepare_openai_compatible_messages(messages)

    assert stats["local_image_path_conversions"] == 1
    data_url = prepared[0]["content"][1]["image_url"]["url"]
    assert data_url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == b"image-bytes"


def test_prepare_messages_when_invalid_local_image_path_is_used_then_drops_that_part():
    prepared, stats = prepare_openai_compatible_messages(
        [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "missing.png"}}],
            },
        ]
    )

    assert stats["local_image_path_conversions"] == 0
    assert prepared == [{"role": "user", "content": []}]


def test_timeout_when_request_overrides_default_then_uses_request_value():
    request = GenerationRequest(
        model="test-model", system_prompt="", messages=[], timeout_seconds=12
    )

    assert resolve_generation_timeout_seconds(request, 30) == 12


def test_timeout_when_value_is_invalid_then_raises():
    with pytest.raises(ValueError, match="timeout"):
        resolve_generation_timeout_seconds(
            GenerationRequest(model="test-model", system_prompt="", messages=[], timeout_seconds=0),
            30,
        )


def test_extract_assistant_text_when_provider_uses_nested_content_then_returns_first_text():
    assert (
        extract_assistant_text({"content": [{"text": "first"}, {"text": "second"}]})
        == "first\nsecond"
    )
    assert extract_assistant_text({"reasoning_content": {"text": "thought"}}) == "thought"
    assert extract_assistant_text({"refusal": "blocked"}) == "blocked"
    assert (
        extract_assistant_text(
            {"content": "", "reasoning_content": "private"}, include_reasoning=False
        )
        == ""
    )


@pytest.mark.asyncio
async def test_openai_compatible_provider_when_tool_round_has_reasoning_then_does_not_expose_it_as_content(
    monkeypatch,
):
    class _Response:
        status_code = 200
        headers = {"Content-Type": "application/json"}
        text = json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "private reasoning",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q":"x"}',
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        )

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return _Response()

    monkeypatch.setattr("sirius_pulse.providers.openai_compatible.httpx.AsyncClient", _Client)
    provider = OpenAICompatibleProvider(base_url="https://example.test", api_key="key")

    result = await provider.generate_async(
        GenerationRequest(
            model="deepseek-chat",
            system_prompt="",
            messages=[{"role": "user", "content": "lookup"}],
        )
    )

    assert result.content == ""
    assert result.reasoning_content == "private reasoning"
    assert result.tool_calls[0].id == "call-1"


class _CapturingAsyncProvider:
    def __init__(self) -> None:
        self.request: GenerationRequest | None = None

    async def generate_async(
        self, request: GenerationRequest, return_reasoning: bool = False
    ) -> GenerationResult:
        self.request = request
        return GenerationResult(content="ok")


class _CapturingRoutingProvider(AutoRoutingProvider):
    def __init__(self, providers: dict[str, ProviderConfig]) -> None:
        super().__init__(providers)
        self.created: dict[str, _CapturingAsyncProvider] = {}

    def _create_provider(self, config: ProviderConfig) -> _CapturingAsyncProvider:
        provider = _CapturingAsyncProvider()
        self.created[config.provider_type] = provider
        return provider


@pytest.mark.asyncio
async def test_auto_routing_provider_when_model_is_provider_scoped_then_uses_that_provider():
    router = _CapturingRoutingProvider(
        {
            "deepseek": ProviderConfig(
                provider_type="deepseek",
                api_key="sk-deepseek",
                base_url="",
                models=["shared-model"],
            ),
            "aliyun-bailian": ProviderConfig(
                provider_type="aliyun-bailian",
                api_key="sk-bailian",
                base_url="",
                models=["shared-model"],
            ),
        }
    )

    await router.generate_async(
        GenerationRequest(
            model="aliyun-bailian/shared-model",
            system_prompt="",
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    assert set(router.created) == {"aliyun-bailian"}
    assert router.created["aliyun-bailian"].request is not None
    assert router.created["aliyun-bailian"].request.model == "shared-model"


def test_provider_names_when_normalized_then_reject_ambiguous_values():
    assert normalize_provider_name(" team-a ") == "team-a"
    with pytest.raises(ValueError, match="不能为空"):
        normalize_provider_name("")
    with pytest.raises(ValueError, match="不能包含"):
        normalize_provider_name("team/a")


def test_provider_registry_when_same_type_has_different_names_then_round_trips(tmp_path):
    registry = ProviderRegistry(tmp_path)
    registry.save(
        {
            "team-a": ProviderConfig(
                name="team-a",
                provider_type="openai-compatible",
                api_key="key-a",
                base_url="https://same.example/v1",
                models=["shared-model"],
            ),
            "team-b": ProviderConfig(
                name="team-b",
                provider_type="openai-compatible",
                api_key="key-b",
                base_url="https://same.example/v1",
                models=["shared-model"],
            ),
        }
    )

    loaded = registry.load()

    assert list(loaded) == ["team-a", "team-b"]
    assert loaded["team-a"].api_key == "key-a"
    assert loaded["team-b"].api_key == "key-b"


@pytest.mark.asyncio
async def test_auto_routing_provider_when_same_type_has_different_names_then_routes_by_name():
    router = _CapturingRoutingProvider(
        {
            "team-a": ProviderConfig(
                name="team-a",
                provider_type="openai-compatible",
                api_key="key-a",
                base_url="https://same.example/v1",
                models=["shared-model"],
            ),
            "team-b": ProviderConfig(
                name="team-b",
                provider_type="openai-compatible",
                api_key="key-b",
                base_url="https://same.example/v1",
                models=["shared-model"],
            ),
        }
    )

    await router.generate_async(
        GenerationRequest(
            model="team-b/shared-model",
            system_prompt="",
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    assert set(router.created) == {"openai-compatible"}
    assert router._last_provider_name == "team-b"
    assert router.created["openai-compatible"].request.model == "shared-model"


@pytest.mark.asyncio
async def test_auto_routing_provider_when_legacy_type_scope_has_one_model_match_then_resolves_name():
    router = _CapturingRoutingProvider(
        {
            "team-a": ProviderConfig(
                name="team-a",
                provider_type="openai-compatible",
                api_key="key-a",
                base_url="",
                models=["model-a"],
            ),
            "team-b": ProviderConfig(
                name="team-b",
                provider_type="openai-compatible",
                api_key="key-b",
                base_url="",
                models=["model-b"],
            ),
        }
    )

    await router.generate_async(
        GenerationRequest(
            model="openai-compatible/model-b",
            system_prompt="",
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    assert router._last_provider_name == "team-b"
    assert router.created["openai-compatible"].request.model == "model-b"


def test_provider_registry_when_legacy_entries_are_loaded_then_adds_unique_names(tmp_path):
    path = tmp_path / "providers" / "provider_keys.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {"name": "same", "type": "deepseek", "api_key": "key-a"},
                    {"name": "same", "type": "deepseek", "api_key": "key-b"},
                ]
            }
        ),
        encoding="utf-8",
    )

    loaded = ProviderRegistry(tmp_path).load()

    assert list(loaded) == ["same", "same-2"]
    assert loaded["same"].api_key == "key-a"
    assert loaded["same-2"].api_key == "key-b"


def test_merge_provider_sources_when_legacy_same_type_entries_are_unnamed_then_keeps_both(tmp_path):
    merged = merge_provider_sources(
        work_path=tmp_path,
        providers_config=[
            {
                "type": "openai-compatible",
                "api_key": "key-a",
                "base_url": "https://same.example/v1",
                "models": ["model-a"],
            },
            {
                "type": "openai-compatible",
                "api_key": "key-b",
                "base_url": "https://same.example/v1",
                "models": ["model-b"],
            },
        ],
    )

    assert list(merged) == ["openai-compatible", "openai-compatible-2"]
    assert merged["openai-compatible"].api_key == "key-a"
    assert merged["openai-compatible-2"].api_key == "key-b"


def test_auto_routing_provider_when_provider_is_disabled_then_it_is_not_routable():
    router = AutoRoutingProvider(
        {
            "disabled": ProviderConfig(
                name="disabled",
                provider_type="deepseek",
                api_key="sk-disabled",
                base_url="",
                models=["disabled-model"],
                enabled=False,
            )
        }
    )

    with pytest.raises(RuntimeError, match="未配置任何提供商"):
        router._pick_provider("disabled/disabled-model")


def test_auto_routing_provider_when_bare_model_matches_multiple_providers_then_errors():
    router = AutoRoutingProvider(
        {
            "deepseek": ProviderConfig(
                provider_type="deepseek",
                api_key="sk-deepseek",
                base_url="",
                models=["shared-model"],
            ),
            "aliyun-bailian": ProviderConfig(
                provider_type="aliyun-bailian",
                api_key="sk-bailian",
                base_url="",
                models=["shared-model"],
            ),
        }
    )

    with pytest.raises(RuntimeError, match="同时存在于多个 provider"):
        router._pick_provider("shared-model")


@pytest.mark.asyncio
async def test_opencode_go_provider_when_generating_then_posts_to_go_chat_completions(monkeypatch):
    class _Response:
        status_code = 200
        headers = {"Content-Type": "application/json"}
        text = json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok", "reasoning_content": ""},
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            }
        )

    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, content=None, headers=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            return _Response()

    monkeypatch.setattr("sirius_pulse.providers.openai_compatible.httpx.AsyncClient", _Client)
    provider = OpenCodeGoProvider(api_key="sk-opencode-go")

    result = await provider.generate_async(
        GenerationRequest(
            model="kimi-k3",
            system_prompt="",
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    assert captured["url"] == "https://opencode.ai/zen/go/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-opencode-go"
    assert result.content == "ok"
    assert get_last_generation_usage()["total_tokens"] == 8


@pytest.mark.asyncio
async def test_auto_routing_provider_when_opencode_models_are_provider_scoped_then_route_works():
    router = _CapturingRoutingProvider(
        {
            "opencode": ProviderConfig(
                provider_type="opencode",
                api_key="sk-opencode",
                base_url="",
                models=["big-pickle"],
            ),
            "opencode-go": ProviderConfig(
                provider_type="opencode-go",
                api_key="sk-opencode-go",
                base_url="",
                models=["kimi-k3"],
            ),
        }
    )

    await router.generate_async(
        GenerationRequest(
            model="opencode-go/kimi-k3",
            system_prompt="",
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    assert set(router.created) == {"opencode-go"}
    assert router.created["opencode-go"].request is not None
    assert router.created["opencode-go"].request.model == "kimi-k3"


# ─── 网络代理配置 ────────────────────────────────────────


def test_proxy_settings_when_saved_and_loaded_then_round_trips(tmp_path):
    settings = ProxySettings(http="http://127.0.0.1:7890", https="", no_proxy="localhost")

    save_proxy_settings(tmp_path, settings)
    loaded = load_proxy_settings(tmp_path)

    assert loaded == settings
    assert get_current_proxy() == settings
    assert (tmp_path / "providers" / "proxy.json").exists()


def test_httpx_proxy_kwargs_when_proxy_disabled_then_empty():
    set_current_proxy(ProxySettings())
    assert httpx_proxy_kwargs() == {}

    set_current_proxy(ProxySettings(http="http://127.0.0.1:7890"))
    assert httpx_proxy_kwargs() == {"proxy": "http://127.0.0.1:7890"}

    # https 优先
    set_current_proxy(ProxySettings(http="http://h:1", https="https://h:2"))
    assert httpx_proxy_kwargs() == {"proxy": "https://h:2"}


@pytest.mark.asyncio
async def test_openai_compatible_provider_when_workspace_set_then_sends_amkr_header(monkeypatch):
    """共享 AMKR 时靠工作空间头隔离，两个请求路径都必须带上。"""
    captured: dict[str, object] = {}

    class _Response:
        status_code = 200
        headers = {"Content-Type": "application/json"}
        text = json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ]
            }
        )

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, content=None, headers=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json.loads(content.decode("utf-8"))
            return _Response()

    monkeypatch.setattr("sirius_pulse.providers.openai_compatible.httpx.AsyncClient", _Client)

    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:8000",
        api_key="sk-local",
        workspace="sirius-pulse",
        task_names={"cognition_analyze"},
    )
    await provider.generate_async(
        GenerationRequest(
            model="cognition_analyze",
            system_prompt="",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.7,
            max_tokens=512,
        )
    )

    assert captured["url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert captured["headers"]["X-AMKR-Workspace"] == "sirius-pulse"
    assert captured["headers"]["Authorization"] == "Bearer sk-local"
    # 任务名路由：采样参数不能随请求发出，否则 AMKR 会以参数冲突拒绝
    assert "temperature" not in captured["json"]
    assert "max_tokens" not in captured["json"]


# ─── models 接口探测 ─────────────────────────────────────


class _ModelsProbeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


@pytest.mark.asyncio
async def test_probe_provider_models_when_openai_format_then_returns_ids(monkeypatch):
    seen_urls: list[str] = []

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers=None):
            seen_urls.append(url)
            return _ModelsProbeResponse(
                200,
                {"object": "list", "data": [{"id": "kimi-k3"}, {"id": "glm-5.2"}]},
            )

    monkeypatch.setattr("sirius_pulse.providers.routing.httpx.AsyncClient", _Client)

    models, used_url = await probe_provider_models(
        provider_type="opencode",
        api_key="sk-x",
        base_url="https://opencode.ai/zen/v1",
    )

    assert models == ["kimi-k3", "glm-5.2"]
    assert used_url == "https://opencode.ai/zen/v1/models"
    assert seen_urls == ["https://opencode.ai/zen/v1/models"]


@pytest.mark.asyncio
async def test_probe_provider_models_when_first_candidate_fails_then_falls_back(monkeypatch):
    seen_urls: list[str] = []

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers=None):
            seen_urls.append(url)
            if url.endswith("/v1/models"):
                return _ModelsProbeResponse(
                    200, {"models": [{"name": "gpt-test"}, {"id": "claude-test"}]}
                )
            return _ModelsProbeResponse(404, {"error": "not found"})

    monkeypatch.setattr("sirius_pulse.providers.routing.httpx.AsyncClient", _Client)

    models, used_url = await probe_provider_models(
        provider_type="openai-compatible",
        api_key="sk-x",
        base_url="https://api.openai.com",
    )

    assert models == ["gpt-test", "claude-test"]
    assert used_url == "https://api.openai.com/v1/models"
    assert seen_urls == [
        "https://api.openai.com/models",
        "https://api.openai.com/v1/models",
    ]


@pytest.mark.asyncio
async def test_probe_provider_models_when_explicit_models_url_then_used_first(monkeypatch):
    seen_urls: list[str] = []

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers=None):
            seen_urls.append(url)
            return _ModelsProbeResponse(200, ["model-a", "model-b"])

    monkeypatch.setattr("sirius_pulse.providers.routing.httpx.AsyncClient", _Client)

    models, used_url = await probe_provider_models(
        provider_type="deepseek",
        api_key="sk-x",
        base_url="https://api.deepseek.com",
        models_url="https://api.deepseek.com/custom/models",
    )

    assert models == ["model-a", "model-b"]
    assert used_url == "https://api.deepseek.com/custom/models"
    assert seen_urls == ["https://api.deepseek.com/custom/models"]


@pytest.mark.asyncio
async def test_probe_provider_models_when_all_candidates_fail_then_raises(monkeypatch):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers=None):
            return _ModelsProbeResponse(401, {"error": "unauthorized"})

    monkeypatch.setattr("sirius_pulse.providers.routing.httpx.AsyncClient", _Client)

    with pytest.raises(RuntimeError, match="models 接口探测失败"):
        await probe_provider_models(
            provider_type="deepseek",
            api_key="sk-x",
            base_url="https://api.deepseek.com",
        )


@pytest.mark.asyncio
async def test_probe_provider_models_when_proxy_configured_then_client_uses_proxy(monkeypatch):
    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers=None):
            return _ModelsProbeResponse(200, {"data": [{"id": "m1"}]})

    monkeypatch.setattr("sirius_pulse.providers.routing.httpx.AsyncClient", _Client)
    set_current_proxy(ProxySettings(http="http://127.0.0.1:7890"))

    await probe_provider_models(
        provider_type="deepseek",
        api_key="sk-x",
        base_url="https://api.deepseek.com",
    )

    assert captured["kwargs"].get("proxy") == "http://127.0.0.1:7890"
