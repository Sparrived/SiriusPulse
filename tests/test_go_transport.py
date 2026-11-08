"""LLM 传输层（Go 侧车）：真跑进程的端到端测试。

这一层被测的是**两个进程之间的真实链路**：``GoTransportProvider`` → 真 Go 侧车
（``go/llm-transport/bin/llm-transport.exe``）→ 假 AMKR（``tests/fake_amkr.py``）。
侧车不 mock、HTTP 不 mock、SSE 用真的 chunked 逐帧下发，因此这里断言的是
``CONTRACT.md`` 真正被实现，而不是实现「看起来像」。

覆盖三件事：

1. **契约语义**（§2 报文 / §3 响应 / §4 聚合与超时 / §5 多模态 / §6 错误映射）；
2. **与 Python 既有传输层的行为等价**（``openai_compatible.py`` 的四类异常、
   usage 记账、空响应与 ``return_reasoning`` 的判定）；
3. **默认配置回归防线**——不配 ``llm_transport`` 时必须仍然构造
   ``OpenAICompatibleProvider``，即这次迁移对默认用户是零行为变化。

侧车二进制缺失时整组跳过（不伪装成通过），提示先跑构建脚本。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import socket
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from sirius_pulse.exceptions import (
    ProviderAuthError,
    ProviderConnectionError,
    ProviderContentFilteredError,
    ProviderResponseError,
)
from sirius_pulse.providers.amkr import (
    LLM_TRANSPORT_GO,
    LLM_TRANSPORT_PYTHON,
    AmkrSettings,
    load_amkr_settings,
)
from sirius_pulse.providers.base import GenerationRequest, get_last_generation_usage
from sirius_pulse.providers.go_transport import GoTransportProvider
from sirius_pulse.providers.openai_compatible import OpenAICompatibleProvider
from sirius_pulse.utils.llm_sidecar import LLMSidecar, resolve_binary
from tests.fake_amkr import FakeAmkr

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GO_MODULE = PROJECT_ROOT / "go" / "llm-transport"

#: 1×1 透明 PNG：侧车按魔数判定 mime，本用例只关心「字节被原样内联」。
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)

_BINARY = resolve_binary("")

requires_binary = pytest.mark.skipif(
    _BINARY is None,
    reason="未找到 LLM 传输层二进制：先运行 python scripts/build_llm_transport.py",
)


# ── 夹具 ──────────────────────────────────────────────────
@pytest.fixture(scope="module")
def sidecar() -> Iterator[LLMSidecar]:
    """整个模块共用一个真实侧车进程：它是无状态传输层，且启动一次省掉重复握手。

    无论用例怎么结束都会 ``stop()``，并在收尾时确认进程真的不在了（不留孤儿）。
    """
    assert _BINARY is not None  # requires_binary 已挡住没有二进制的情况
    handle = LLMSidecar(binary=str(_BINARY))
    handle.start()
    try:
        yield handle
    finally:
        handle.stop()
    assert not handle.is_alive(), "侧车进程没有随 stop() 退出"
    assert handle.base_url == "", "stop() 之后不该再保留地址"


@pytest.fixture
def amkr() -> Iterator[FakeAmkr]:
    server = FakeAmkr().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(autouse=True)
def _clear_thread_local_usage() -> Iterator[None]:
    """``_LAST_GENERATION_USAGE`` 是线程局部的一次性读：用例之间必须清干净。"""
    get_last_generation_usage()
    yield
    get_last_generation_usage()


# ── 辅助 ──────────────────────────────────────────────────
def _provider(
    amkr: FakeAmkr | None, sidecar: LLMSidecar, **overrides: object
) -> GoTransportProvider:
    fields: dict[str, object] = {
        "sidecar_url": sidecar.base_url,
        "sidecar_token": sidecar.token,
        "base_url": amkr.base_url if amkr is not None else "http://127.0.0.1:1",
        "api_key": "amkr_ik_test",
        "timeout_seconds": 10,
        "workspace": "sirius-pulse/yuebai",
    }
    fields.update(overrides)
    return GoTransportProvider(**fields)  # type: ignore[arg-type]


def _request(**overrides: object) -> GenerationRequest:
    fields: dict[str, object] = {
        "model": "response_generate",
        "system_prompt": "你是月白。",
        "messages": [{"role": "user", "content": "在吗"}],
        "purpose": "response_generate",
    }
    fields.update(overrides)
    return GenerationRequest(**fields)  # type: ignore[arg-type]


def _generate(
    provider: GoTransportProvider,
    request: GenerationRequest,
    *,
    return_reasoning: bool = False,
):
    """在同一个事件循环里跑一次生成并关闭客户端，避免 httpx 客户端泄漏告警。"""

    async def main():
        try:
            return await provider.generate_async(request, return_reasoning=return_reasoning)
        finally:
            await provider.aclose()

    return asyncio.run(main())


def _closed_port() -> int:
    """拿一个刚刚被释放的端口，用来构造「侧车不可达」。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ── 契约 §2：请求报文 ─────────────────────────────────────
@requires_binary
def test_upstream_request_matches_contract(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """侧车发往 AMKR 的请求必须是恒定流式、无采样参数、原样任务名。"""
    result = _generate(_provider(amkr, sidecar), _request())
    assert result.content == "你好，世界"

    request = amkr.last_request
    assert request.path == "/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer amkr_ik_test"
    assert request.headers["x-amkr-workspace"] == "sirius-pulse/yuebai"

    body = request.json_body
    assert body is not None
    assert body["model"] == "response_generate", "任务名必须原样透传，不得改写"
    assert body["stream"] is True, "契约 §4.1 要求恒定流式"
    assert body["stream_options"] == {"include_usage": True}
    # system_prompt 由侧车补成 messages[0]
    assert body["messages"][0] == {"role": "system", "content": "你是月白。"}
    assert body["messages"][1] == {"role": "user", "content": "在吗"}
    # 契约 §2：绝不能出现采样参数（AMKR 的任务定义才是权威）
    for forbidden in ("temperature", "max_tokens", "top_p", "presence_penalty"):
        assert forbidden not in body, f"传输层不该下发采样参数 {forbidden}"


@requires_binary
def test_tools_are_passed_through(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "搜一下",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    _generate(
        _provider(amkr, sidecar),
        _request(
            tools=tools,
            tool_choice="auto",
            response_format={"type": "json_object"},
        ),
    )

    body = amkr.last_request.json_body or {}
    assert body["tools"] == tools
    assert body["tool_choice"] == "auto"
    assert body["response_format"] == {"type": "json_object"}


# ── 契约 §3/§4：成功路径与增量聚合 ────────────────────────
@requires_binary
def test_text_reply(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    result = _generate(_provider(amkr, sidecar), _request())
    assert result.content == "你好，世界", "多帧 delta.content 必须按序拼接"
    assert result.reasoning_content == ""
    assert result.tool_calls is None
    assert result.finish_reason == "stop"


@requires_binary
def test_reasoning_is_merged(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    amkr.set_scenario("reasoning")
    result = _generate(_provider(amkr, sidecar), _request())
    assert result.reasoning_content == "先想一下"
    assert result.content == "答案"


@requires_binary
def test_tool_calls_are_merged_by_index(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """分块 tool_calls 按 index 归位，name/arguments 跨块累加（契约 §4.4）。"""
    amkr.set_scenario("tool_calls")
    result = _generate(_provider(amkr, sidecar), _request())

    assert result.tool_calls is not None
    assert len(result.tool_calls) == 2, "两个 index 必须聚合成两个 ToolCall"
    first, second = result.tool_calls
    assert (first.id, first.type, first.function_name) == ("call_a", "function", "lookup")
    assert first.function_arguments == '{"q":"天气"}', "arguments 必须跨块拼接"
    assert (second.id, second.function_name, second.function_arguments) == (
        "call_b",
        "send",
        "{}",
    )
    assert result.finish_reason == "tool_calls"
    # 只有 tool_calls、没有正文是正常场景，不能被判成空响应
    assert result.content == ""


@requires_binary
def test_bad_frames_are_skipped(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """注释行 / event: 行 / 半截 JSON / 非 JSON 帧都不能毁掉整段回复（契约 §4.3）。"""
    amkr.set_scenario("bad_frames")
    result = _generate(_provider(amkr, sidecar), _request())
    assert result.content == "前半后半"


@requires_binary
def test_stream_without_done_keeps_content(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """上游没发 ``[DONE]`` 就 EOF 时，已聚合的内容不该丢。"""
    amkr.set_scenario("truncated")
    result = _generate(_provider(amkr, sidecar), _request())
    assert result.content == "说到一半"


# ── usage：上游原始 dict 原样透传 ─────────────────────────
@requires_binary
def test_usage_is_passed_through(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    amkr.set_scenario("usage")
    _generate(_provider(amkr, sidecar), _request())
    assert get_last_generation_usage() == {
        "prompt_tokens": 1234,
        "completion_tokens": 56,
        "total_tokens": 1290,
    }


@requires_binary
def test_empty_usage_is_none(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """上游 ``usage: {}`` 视为没有（与 OpenAICompatibleProvider 一致，契约 §4.5）。"""
    amkr.set_scenario("no_usage")
    _generate(_provider(amkr, sidecar), _request())
    assert get_last_generation_usage() is None


# ── 契约 §6：错误映射（四类 + 重试语义）──────────────────
@requires_binary
@pytest.mark.parametrize(
    ("scenario", "expected_type", "retryable", "http_status"),
    [
        ("status_401", ProviderAuthError, False, 401),
        ("status_429", ProviderResponseError, True, 429),
        ("status_500", ProviderResponseError, True, 500),
        ("status_400", ProviderResponseError, False, 400),
    ],
)
def test_upstream_status_mapping(
    amkr: FakeAmkr,
    sidecar: LLMSidecar,
    scenario: str,
    expected_type: type[Exception],
    retryable: bool,
    http_status: int,
) -> None:
    """上游状态码 → 本仓库既有异常类型 + 正确的重试语义。

    关键点在 400：侧车统一回 502，若把 502 当作 ``http_status`` 就会让一个
    「请求本身有问题」的 400 变成可重试，那是迁移前没有的行为。
    """
    amkr.set_scenario(scenario)
    with pytest.raises(expected_type) as excinfo:
        _generate(_provider(amkr, sidecar), _request())

    error = excinfo.value
    assert error.is_retryable is retryable  # type: ignore[attr-defined]
    assert getattr(error, "http_status", None) == http_status
    assert f"提供商 HTTP 错误 {http_status}" in str(error)


@requires_binary
def test_sidecar_rejects_wrong_token(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """侧车自己的 401（不是上游 401）同样要变成不可重试的认证失败。"""
    provider = _provider(amkr, sidecar, sidecar_token="wrong-token")
    with pytest.raises(ProviderAuthError) as excinfo:
        _generate(provider, _request())
    assert excinfo.value.is_retryable is False
    assert "鉴权失败" in str(excinfo.value)


@requires_binary
def test_invalid_request_is_not_retryable(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """缺 ``upstream.base_url`` 时侧车回 400 INVALID_REQUEST。"""
    provider = _provider(amkr, sidecar, base_url="")
    with pytest.raises(ProviderResponseError) as excinfo:
        _generate(provider, _request())
    assert excinfo.value.is_retryable is False
    assert getattr(excinfo.value, "http_status", None) == 400


@requires_binary
def test_empty_response_raises_response_error(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """契约 v1.1：侧车不再判空响应，判定归 Python（普通调用算失败）。"""
    amkr.set_scenario("reasoning_only")
    with pytest.raises(ProviderResponseError) as excinfo:
        _generate(_provider(amkr, sidecar), _request())
    assert str(excinfo.value) == "提供商响应内容为空。"
    assert getattr(excinfo.value, "http_status", None) == 200


@requires_binary
def test_content_filtered_clears_usage(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """命中内容策略标记 → 专用异常、不可重试、usage 丢弃（契约 §4.7）。"""
    amkr.set_scenario("content_filtered")
    with pytest.raises(ProviderContentFilteredError) as excinfo:
        _generate(_provider(amkr, sidecar), _request())

    error = excinfo.value
    assert error.is_retryable is False
    assert "could not be submitted" in error.content_preview
    assert get_last_generation_usage() is None, "这次调用没有任何可用输出，usage 必须丢"


@requires_binary
def test_stream_error_event(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """流内 ``error`` 事件必须当失败抛，不能把残片当成一次成功（契约 §4.8）。"""
    amkr.set_scenario("error_event")
    with pytest.raises(ProviderResponseError) as excinfo:
        _generate(_provider(amkr, sidecar), _request())
    assert str(excinfo.value).startswith("提供商流式响应错误：")
    assert "上游模型中断" in str(excinfo.value)


# ── 契约 §4.9/§5：非 SSE 回退与多模态 ────────────────────
@requires_binary
def test_non_sse_json_fallback(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """网关忽略 stream、直接回一整段 JSON 时的回退路径。"""
    amkr.set_scenario("json")
    result = _generate(_provider(amkr, sidecar), _request())

    assert result.content == "整段 JSON 回复"
    assert result.reasoning_content == "先想一下"
    assert result.tool_calls is not None
    assert result.tool_calls[0].function_name == "search"
    assert get_last_generation_usage() == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
    }


@requires_binary
def test_html_200_is_response_error(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """2xx 但正文是 HTML 错误页 → 明确的响应异常，且不可重试（上游 200）。"""
    amkr.set_scenario("html")
    with pytest.raises(ProviderResponseError) as excinfo:
        _generate(_provider(amkr, sidecar), _request())
    assert excinfo.value.is_retryable is False
    assert "非 JSON" in str(excinfo.value)


@requires_binary
def test_local_image_is_inlined_as_data_url(
    amkr: FakeAmkr, sidecar: LLMSidecar, tmp_path: Path
) -> None:
    """本地图片路径由 **侧车** 转成 data URL：Python 这条路径不碰图片字节。"""
    image_path = tmp_path / "avatar.png"
    image_path.write_bytes(PNG_1PX)

    request = _request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": str(image_path)}},
                    {"type": "text", "text": "看图"},
                ],
            }
        ]
    )
    _generate(_provider(amkr, sidecar), request)

    body = amkr.last_request.json_body or {}
    parts = body["messages"][1]["content"]
    assert len(parts) == 2
    url = parts[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,"), url[:40]
    assert base64.b64decode(url.split(",", 1)[1]) == PNG_1PX, "内联字节必须与原图一致"
    assert parts[1] == {"type": "text", "text": "看图"}


@requires_binary
def test_invalid_image_path_is_dropped(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """既不是本地文件也不是网络地址的 part 丢弃，请求照常成功（契约 §5.3）。"""
    request = _request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "D:/no/such/file.png"}},
                    {"type": "text", "text": "还能说话吗"},
                ],
            }
        ]
    )
    result = _generate(_provider(amkr, sidecar), request)
    assert result.content == "你好，世界"

    parts = (amkr.last_request.json_body or {})["messages"][1]["content"]
    assert parts == [{"type": "text", "text": "还能说话吗"}], "无效图片 part 必须被丢掉"


@requires_binary
def test_remote_image_url_is_passed_verbatim(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """http(s) 与 data: 地址原样透传（契约 §5.1）。"""
    data_url = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()
    request = _request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
    )
    _generate(_provider(amkr, sidecar), request)

    parts = (amkr.last_request.json_body or {})["messages"][1]["content"]
    assert parts[0]["image_url"]["url"] == "https://example.invalid/a.png"
    assert parts[1]["image_url"]["url"] == data_url


# ── 契约 §3：transport 统计字段（直接打侧车，绕过 Python 桥接）──
@requires_binary
def test_sidecar_transport_fields(amkr: FakeAmkr, sidecar: LLMSidecar, tmp_path: Path) -> None:
    """``transport`` 是契约 §3 的一部分，Python 桥接未消费它，这里直接验侧车。

    ``local_image_conversions`` / ``path`` / ``upstream_ms`` 目前只进侧车日志，
    Python 侧没有暴露到 ``GenerationResult``；因此这条断言必须打在线上。
    """
    image_path = tmp_path / "inline.png"
    image_path.write_bytes(PNG_1PX)

    payload = {
        "upstream": {
            "base_url": amkr.base_url,
            "api_key": "amkr_ik_test",
            "workspace": "",
            "timeout_seconds": 5,
        },
        "request": {
            "model": "response_generate",
            "system_prompt": "s",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": str(image_path)}},
                        {"type": "text", "text": "看图"},
                    ],
                }
            ],
            "purpose": "qa",
        },
    }
    response = httpx.post(
        f"{sidecar.base_url}/v1/generate",
        json=payload,
        headers={"Authorization": f"Bearer {sidecar.token}"},
        timeout=15,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["content"] == "你好，世界"
    transport = body["transport"]
    assert transport["path"] == "sse"
    assert transport["local_image_conversions"] == 1
    assert "text/event-stream" in transport["content_type"]
    assert transport["upstream_ms"] > 0


@requires_binary
def test_health_reports_source_version(sidecar: LLMSidecar) -> None:
    """``/health`` 的 version 必须等于仓库里 Go 源码的版本，防止跑到旧二进制。"""
    source = (GO_MODULE / "internal" / "contract" / "contract.go").read_text(encoding="utf-8")
    matched = re.search(r'Version\s*=\s*"([^"]+)"', source)
    assert matched is not None, "contract.go 里找不到 Version 常量"
    assert sidecar.version == matched.group(1)
    assert sidecar.version.split(".", 1)[0] == "0", "v1 契约期内主版本必须是 0（契约 §8）"


# ── 契约 §4.2：块间静默超时 ───────────────────────────────
@requires_binary
def test_inter_chunk_silence_times_out(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """静默超时是**块间**语义：先收到一块，再静默超过 timeout 即判超时。"""
    amkr.set_scenario("silence", silence_seconds=2.0)
    started = time.monotonic()
    with pytest.raises(ProviderConnectionError) as excinfo:
        _generate(_provider(amkr, sidecar, timeout_seconds=1), _request(timeout_seconds=1))
    elapsed = time.monotonic() - started

    assert "没有返回数据" in str(excinfo.value)
    assert elapsed < 1.9, f"应在 ~1s 判超时，实际 {elapsed:.2f}s（说明走了整请求超时）"


# ── 侧车不可达 ────────────────────────────────────────────
def test_sidecar_unreachable_is_connection_error() -> None:
    """端口关闭 → ``ProviderConnectionError``（可重试），不抛 httpx 原生异常。

    这条用例与侧车二进制无关（正是「没有侧车」的场景），因此不依赖二进制夹具。
    """
    provider = GoTransportProvider(
        sidecar_url=f"http://127.0.0.1:{_closed_port()}",
        sidecar_token="whatever",
        base_url="http://127.0.0.1:8000",
        api_key="amkr_ik_test",
        timeout_seconds=5,
    )
    with pytest.raises(ProviderConnectionError) as excinfo:
        _generate(provider, _request())
    assert excinfo.value.is_retryable is True
    assert "LLM 传输层请求异常" in str(excinfo.value)


# ── return_reasoning（契约 v1.1 的核心变更）───────────────
@requires_binary
def test_return_reasoning_is_a_tuple(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    amkr.set_scenario("reasoning")
    reasoning, result = _generate(_provider(amkr, sidecar), _request(), return_reasoning=True)
    assert reasoning == "先想一下"
    assert result.content == "答案"


@requires_binary
def test_reasoning_only_succeeds_with_return_reasoning(amkr: FakeAmkr, sidecar: LLMSidecar) -> None:
    """``return_reasoning=True`` 只取思维链是合法场景，不能按空响应报错。"""
    amkr.set_scenario("reasoning_only")
    reasoning, result = _generate(_provider(amkr, sidecar), _request(), return_reasoning=True)
    assert reasoning == "只有想法没有正文"
    assert result.content == ""


# ── 配置：llm_transport 的解析与 provider 选择（回归防线）──
def _settings(**overrides: object) -> AmkrSettings:
    fields: dict[str, object] = {
        "base_url": "http://127.0.0.1:8000",
        "api_key": "amkr_local_admin_key",
        "workspace": "sirius-pulse",
    }
    fields.update(overrides)
    return AmkrSettings(**fields)  # type: ignore[arg-type]


def test_default_transport_stays_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """**回归防线**：没配 ``llm_transport`` 时仍然构造纯 Python provider。"""
    from sirius_pulse.platforms import runtime as runtime_module

    def _boom(**kwargs: object) -> object:
        raise AssertionError("默认配置不该拉起 Go 侧车")

    monkeypatch.setattr(runtime_module, "get_sidecar", _boom)

    provider = runtime_module._build_provider(_settings(), "sirius-pulse/yuebai", "amkr_ik_test")
    assert isinstance(provider, OpenAICompatibleProvider)
    assert not isinstance(provider, GoTransportProvider)


def test_go_transport_builds_sidecar_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from sirius_pulse.platforms import runtime as runtime_module

    class _FakeSidecar:
        base_url = "http://127.0.0.1:18080"
        token = "sidecar-token"

    monkeypatch.setattr(runtime_module, "get_sidecar", lambda **kwargs: _FakeSidecar())
    provider = runtime_module._build_provider(
        _settings(llm_transport=LLM_TRANSPORT_GO), "sirius-pulse/yuebai", "amkr_ik_test"
    )
    assert isinstance(provider, GoTransportProvider)


@pytest.mark.parametrize(("fallback", "expect_provider"), [(True, True), (False, False)])
def test_go_transport_falls_back_to_python(
    monkeypatch: pytest.MonkeyPatch, fallback: bool, expect_provider: bool
) -> None:
    """侧车起不来时：默认回退 Python；关掉回退则显式「不就绪」。"""
    from sirius_pulse.platforms import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "get_sidecar", lambda **kwargs: None)
    provider = runtime_module._build_provider(
        _settings(llm_transport=LLM_TRANSPORT_GO, llm_transport_fallback=fallback),
        "sirius-pulse/yuebai",
        "amkr_ik_test",
    )
    if expect_provider:
        assert isinstance(provider, OpenAICompatibleProvider)
    else:
        assert provider is None


def test_build_provider_without_credentials_is_none() -> None:
    from sirius_pulse.platforms import runtime as runtime_module

    assert runtime_module._build_provider(_settings(api_key=""), "", "amkr_ik_test") is None
    assert runtime_module._build_provider(_settings(), "", "") is None


def test_settings_parse_transport_keys(tmp_path: Path) -> None:
    """四个传输层键都可选：缺失即纯 Python，写错也按纯 Python 处理。"""
    config = tmp_path / "global_config.json"
    config.write_text(
        json.dumps(
            {
                "amkr_local_api_key": "amkr_local_admin_key",
                "llm_transport": "GO",
                "llm_transport_port": "18099",
                "llm_transport_fallback": "false",
            }
        ),
        encoding="utf-8",
    )
    settings = load_amkr_settings(tmp_path)
    assert settings.llm_transport == LLM_TRANSPORT_GO
    assert settings.uses_go_transport is True
    assert settings.llm_transport_port == 18099
    assert settings.llm_transport_fallback is False

    config.write_text(json.dumps({"amkr_local_api_key": "k"}), encoding="utf-8")
    default_settings = load_amkr_settings(tmp_path)
    assert default_settings.llm_transport == LLM_TRANSPORT_PYTHON
    assert default_settings.uses_go_transport is False
    assert default_settings.llm_transport_port == 0
    assert default_settings.llm_transport_fallback is True

    config.write_text(json.dumps({"llm_transport": "golang"}), encoding="utf-8")
    assert load_amkr_settings(tmp_path).llm_transport == LLM_TRANSPORT_PYTHON
