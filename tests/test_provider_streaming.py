"""传输层：模型调用统一走流式 SSE，逐块聚合成完整结果。

背景：非流式上游（CPA 这类）经常返回 200 空响应，所以 provider 改成流式请求。
流式聚合必须与原来的非流式结果逐字段等价，否则就是「修好空响应、弄坏回复内容」。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from sirius_pulse.exceptions import ProviderResponseError
from sirius_pulse.providers.base import (
    GenerationRequest,
    ToolCall,
    get_last_generation_usage,
)
from sirius_pulse.providers.openai_compatible import OpenAICompatibleProvider


def _provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="http://amkr.invalid/v1",
        api_key="amkr_ik_test",
        timeout_seconds=5,
    )


def _request() -> GenerationRequest:
    return GenerationRequest(
        model="response_generate",
        system_prompt="system",
        messages=[{"role": "user", "content": "hi"}],
        timeout_seconds=5,
    )


def _sse(*events: object) -> bytes:
    frames = [f"data: {json.dumps(event, ensure_ascii=False)}" for event in events]
    frames.append("data: [DONE]")
    return ("\n\n".join(frames) + "\n\n").encode("utf-8")


def _call(monkeypatch, provider: OpenAICompatibleProvider, handler):
    """用 MockTransport 驱动真实的 generate_async，返回聚合结果。"""
    import sirius_pulse.providers.openai_compatible as mod

    transport = httpx.MockTransport(handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)

    async def main():
        return await provider.generate_async(_request())

    return asyncio.run(main())


def _stream_response(*events: object) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream; charset=utf-8"},
        content=_sse(*events),
    )


def test_stream_request_aggregates_content_reasoning_and_usage(monkeypatch):
    """请求必须是 stream=true 且索取 usage；正文/推理/结束原因按块拼回。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _stream_response(
            {"choices": [{"delta": {"reasoning_content": "想一下"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "你好"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "世界"}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        )

    result = _call(monkeypatch, _provider(), handler)

    assert seen["stream"] is True
    assert seen["stream_options"] == {"include_usage": True}
    assert result.content == "你好世界"
    assert result.reasoning_content == "想一下"
    assert result.finish_reason == "stop"
    assert get_last_generation_usage() == {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
    }


def test_stream_tool_calls_are_reassembled_across_chunks(monkeypatch):
    """流式 tool_calls 的 name/arguments 是跨块增量，必须按 index 拼接。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _stream_response(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"q":'}}]},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"猫"}'}}]},
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    result = _call(monkeypatch, _provider(), handler)

    assert result.content == ""
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == [
        ToolCall(
            id="call_1",
            type="function",
            function_name="lookup",
            function_arguments='{"q":"猫"}',
        )
    ]


def test_empty_stream_surfaces_as_response_error(monkeypatch):
    """上游只回 [DONE] 时不能当成一次成功的空回复，要报错让上层处理。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _stream_response()

    with pytest.raises(ProviderResponseError, match="为空"):
        _call(monkeypatch, _provider(), handler)


def test_stream_error_event_surfaces_as_retryable_error(monkeypatch):
    """AMKR 在流中断时补发 error 事件，必须抛错而不是静默接受残片。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _stream_response(
            {
                "error": {
                    "type": "api_error",
                    "code": "upstream_stream_error",
                    "message": "上游流式响应失败或未完整结束",
                }
            }
        )

    with pytest.raises(ProviderResponseError, match="流式响应错误") as info:
        _call(monkeypatch, _provider(), handler)

    assert info.value.is_retryable is True


def test_gateway_ignoring_stream_still_returns_json_completion(monkeypatch):
    """个别网关忽略 stream、直接回一整段 JSON：仍要能正常解析。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "回退正常"}, "finish_reason": "stop"},
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            },
        )

    result = _call(monkeypatch, _provider(), handler)

    assert result.content == "回退正常"
    assert result.finish_reason == "stop"
    assert get_last_generation_usage() == {
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 7,
    }
