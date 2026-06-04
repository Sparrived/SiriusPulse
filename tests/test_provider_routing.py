"""Tests for provider registry and auto-routing behavior."""

from __future__ import annotations

import pytest

from sirius_pulse.providers.base import GenerationRequest, GenerationResult
from sirius_pulse.providers.routing import (
    AutoRoutingProvider,
    ProviderConfig,
    ProviderRegistry,
    WorkspaceProviderManager,
    ensure_provider_platform_supported,
    merge_provider_sources,
    normalize_provider_type,
    probe_provider_availability,
    run_provider_detection_flow,
)


class FakeAsyncProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.requests: list[GenerationRequest] = []

    async def generate_async(
        self,
        request: GenerationRequest,
        return_reasoning: bool = False,
    ) -> GenerationResult:
        self.requests.append(request)
        return GenerationResult(content=self.response)


class FakeRoutingProvider(AutoRoutingProvider):
    def __init__(self, providers: dict[str, ProviderConfig]) -> None:
        super().__init__(providers)
        self.created: list[ProviderConfig] = []
        self.fake_provider = FakeAsyncProvider("routed")

    def _create_provider(self, config: ProviderConfig) -> FakeAsyncProvider:
        self.created.append(config)
        return self.fake_provider


def test_provider_type_when_aliases_are_supplied_then_normalizes_to_supported_keys():
    assert normalize_provider_type("openai") == "openai-compatible"
    assert normalize_provider_type("ARK") == "volcengine-ark"
    assert normalize_provider_type("dashscope") == "aliyun-bailian"
    assert normalize_provider_type("zhipuai") == "bigmodel"
    assert ensure_provider_platform_supported("xiaomi-mimo") == "mimo"


def test_provider_type_when_unknown_then_support_check_raises():
    with pytest.raises(RuntimeError):
        ensure_provider_platform_supported("unknown-provider")


def test_provider_registry_when_saved_then_loads_normalized_enabled_configs(tmp_path):
    registry = ProviderRegistry(tmp_path)

    registry.save(
        {
            "openai-compatible": ProviderConfig(
                provider_type="openai-compatible",
                api_key="sk-test",
                base_url="https://example.test",
                healthcheck_model="gpt-test",
                enabled=False,
                models=["gpt-test", "gpt-fast"],
                models_url="https://models.test",
            )
        }
    )
    loaded = registry.load()

    assert registry.path == tmp_path / "providers" / "provider_keys.json"
    assert loaded["openai-compatible"].api_key == "sk-test"
    assert loaded["openai-compatible"].enabled is False
    assert loaded["openai-compatible"].models == ["gpt-test", "gpt-fast"]
    assert loaded["openai-compatible"].models_url == "https://models.test"


def test_workspace_provider_manager_when_entries_are_partial_then_preserves_existing_fields(tmp_path):
    manager = WorkspaceProviderManager(tmp_path)
    manager.register(
        provider_type="openai",
        api_key="old-key",
        base_url="https://old.test",
        healthcheck_model="old-health",
        models=["old-model"],
    )

    merged = manager.merge_entries([{"type": "openai", "api_key": "", "enabled": False}])

    assert merged["openai-compatible"].api_key == "old-key"
    assert merged["openai-compatible"].base_url == "https://old.test"
    assert merged["openai-compatible"].healthcheck_model == "old-health"
    assert merged["openai-compatible"].models == ["old-model"]
    assert merged["openai-compatible"].enabled is False


def test_merge_provider_sources_when_session_omits_models_then_keeps_persistent_models(tmp_path):
    ProviderRegistry(tmp_path).save(
        {
            "deepseek": ProviderConfig(
                provider_type="deepseek",
                api_key="stored-key",
                base_url="https://stored.test",
                healthcheck_model="deepseek-chat",
                models=["deepseek-chat"],
            )
        }
    )

    merged = merge_provider_sources(
        work_path=tmp_path,
        providers_config=[
            {
                "type": "deepseek",
                "api_key": "session-key",
                "base_url": "https://session.test",
            }
        ],
    )

    assert merged["deepseek"].api_key == "session-key"
    assert merged["deepseek"].base_url == "https://session.test"
    assert merged["deepseek"].models == ["deepseek-chat"]


@pytest.mark.asyncio
async def test_auto_routing_when_model_matches_explicit_list_then_forwards_request():
    provider = FakeRoutingProvider(
        {
            "openai-compatible": ProviderConfig(
                provider_type="openai-compatible",
                api_key="sk",
                base_url="",
                healthcheck_model="health-model",
                models=["chat-model"],
            )
        }
    )
    request = GenerationRequest(
        model="chat-model",
        system_prompt="system",
        messages=[{"role": "user", "content": "hello"}],
    )

    result = await provider.generate_async(request)

    assert result.content == "routed"
    assert provider.created[0].provider_type == "openai-compatible"
    assert provider.fake_provider.requests[0].model == "chat-model"


def test_auto_routing_when_only_healthcheck_matches_then_uses_healthcheck_model():
    provider = AutoRoutingProvider(
        {
            "deepseek": ProviderConfig(
                provider_type="deepseek",
                api_key="sk",
                base_url="",
                healthcheck_model="deepseek-chat",
                models=[],
            )
        }
    )

    selected, matched_by = provider._pick_provider("deepseek-chat")

    assert selected.provider_type == "deepseek"
    assert matched_by == "healthcheck_model"


@pytest.mark.asyncio
async def test_probe_provider_availability_when_provider_returns_text_then_succeeds():
    provider = FakeAsyncProvider("ok")

    await probe_provider_availability(provider=provider, model_name="health-model")

    assert provider.requests[0].purpose == "provider_healthcheck"
    assert provider.requests[0].max_tokens == 8


@pytest.mark.asyncio
async def test_probe_provider_availability_when_provider_returns_empty_then_raises():
    provider = FakeAsyncProvider("  ")

    with pytest.raises(RuntimeError):
        await probe_provider_availability(provider=provider, model_name="health-model")


@pytest.mark.asyncio
async def test_detection_flow_when_required_fields_missing_then_raises_before_network():
    with pytest.raises(RuntimeError):
        await run_provider_detection_flow(
            providers={
                "deepseek": ProviderConfig(
                    provider_type="deepseek",
                    api_key="",
                    base_url="",
                    healthcheck_model="deepseek-chat",
                )
            }
        )
