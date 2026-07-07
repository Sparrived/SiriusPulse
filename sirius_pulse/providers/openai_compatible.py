from __future__ import annotations

import json
import logging
from typing import cast

import httpx

from sirius_pulse.providers.base import (
    DEFAULT_TIMEOUT_SECONDS,
    AsyncLLMProvider,
    GenerationRequest,
    GenerationResult,
    ToolCall,
    build_chat_completion_payload,
    build_generation_debug_context,
    prepare_openai_compatible_messages,
    resolve_generation_timeout_seconds,
    set_last_generation_usage,
)
from sirius_pulse.providers.response_utils import extract_assistant_text

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(AsyncLLMProvider):
    """OpenAI-compatible provider backed by /v1/chat/completions."""

    _provider_name = "openai-compatible"

    def __init__(
        self, *, base_url: str, api_key: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def _build_url(self, request: GenerationRequest) -> str:
        return f"{self._base_url}/v1/chat/completions"

    async def generate_async(
        self, request: GenerationRequest, return_reasoning: bool = False
    ) -> GenerationResult | tuple[str, GenerationResult]:
        timeout_seconds = resolve_generation_timeout_seconds(request, self._timeout_seconds)
        url = self._build_url(request)
        debug_context = build_generation_debug_context(
            request,
            provider_name=self._provider_name,
            url=url,
            base_url=self._base_url,
            timeout_seconds=timeout_seconds,
        )

        logger.info(
            f"正准备向 {self._provider_name} 的 {request.model} 请教问题，"
            f"手头有 {debug_context['input_message_count']} 条消息想说，"
            f"温度调到 {request.temperature}，Token 上限设了 {request.max_tokens}，"
            f"预计要花 {debug_context['estimated_input_tokens']} 个 Token，"
            f"超时 {timeout_seconds:.1f} 秒～"
        )
        payload = build_chat_completion_payload(request, provider_name=self._provider_name)
        wire_messages, transport_stats = prepare_openai_compatible_messages(
            cast(list[dict[str, object]], payload["messages"])
        )
        wire_payload = dict(payload)
        wire_payload["messages"] = wire_messages

        body = json.dumps(wire_payload).encode("utf-8")
        logger.debug(
            f"[模型调用详情] {request.model} | 请求详情:\n"
            f"{json.dumps({**debug_context, **transport_stats, 'request_body_bytes': len(body), 'payload': payload}, ensure_ascii=False, indent=2)}"
        )

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(url, content=body, headers=headers)
                status_code = response.status_code
                content_type = str(response.headers.get("Content-Type", "")).strip()
                raw = response.text
        except httpx.HTTPError as exc:
            logger.error(
                f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} "
                f"| {type(exc).__name__}: {exc or '(无详细信息)'}",
                exc_info=True,
            )
            raise RuntimeError(f"提供商请求异常：{exc}") from exc

        if status_code >= 400:
            logger.error(
                f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} "
                f"| HTTP {status_code}: {raw[:500]}"
            )
            message = f"提供商 HTTP 错误 {status_code}：{raw[:200]}"
            if status_code == 400 and "Failed to download multimodal content" in raw:
                message = (
                    f"{message}。多模态文件下载失败：请确认 image_url 使用公网可访问的 http/https URL，"
                    "且响应头包含 Content-Type 与 Content-Length；若传入的是本地图片路径，"
                    "请直接传本地文件路径让框架自动转换为 data URL，或自行传入 data:*;base64,...。"
                )
            raise RuntimeError(message)

        logger.debug(
            f"[模型原始响应] {request.model} | Provider: {self._provider_name} | URL: {url} "
            f"| HTTP状态: {status_code} | Content-Type: {content_type or '(未知)'} | raw:\n{raw}"
        )

        data = json.loads(raw)
        choices = data.get("choices", [])
        if not choices:
            logger.error(
                f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} | 无 choices"
            )
            raise RuntimeError("提供商响应中没有 choices。")

        choice = choices[0]
        message = choice.get("message", {})
        if not isinstance(message, dict):
            logger.error(
                f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} | message 字段无效"
            )
            raise RuntimeError("提供商响应中 message 字段无效。")

        # 解析 tool_calls
        tool_calls: list[ToolCall] | None = None
        raw_tool_calls = message.get("tool_calls")
        if isinstance(raw_tool_calls, list) and raw_tool_calls:
            tool_calls = []
            for tc in raw_tool_calls:
                if not isinstance(tc, dict):
                    continue
                func = tc.get("function", {})
                tool_calls.append(
                    ToolCall(
                        id=str(tc.get("id", "")),
                        type=str(tc.get("type", "function")),
                        function_name=str(func.get("name", "")),
                        function_arguments=str(func.get("arguments", "")),
                    )
                )

        content = extract_assistant_text(message)
        finish_reason = str(choice.get("finish_reason", "stop"))

        # 如果没有 tool_calls 且内容为空，检查是否为错误
        if not content and not tool_calls and not return_reasoning:
            logger.error(
                f"[模型调用失败] {request.model} | Provider: {self._provider_name} | URL: {url} "
                f"| 响应为空 | message_keys={list(message.keys())}"
            )
            raise RuntimeError("提供商响应内容为空。")

        usage = data.get("usage")
        if usage and isinstance(usage, dict):
            set_last_generation_usage(dict(usage))
        else:
            set_last_generation_usage(None)

        result = GenerationResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

        logger.info(
            f"{self._provider_name} 的 {request.model} 回复我了，写了 {len(content or '')} 个字～"
        )
        logger.debug(
            f"[模型输出] {request.model} | Provider: {self._provider_name} | URL: {url} | 响应内容:\n{content}"
        )
        if return_reasoning:
            reasoning = message.get("reasoning_content", "") if isinstance(message, dict) else ""
            return (reasoning, result)
        return result

    async def generate_stream(self, request: GenerationRequest):
        """流式生成，逐 token yield (chunk_type, text) 其中 chunk_type 为 'reasoning' 或 'content'。"""
        timeout_seconds = resolve_generation_timeout_seconds(request, self._timeout_seconds)
        url = self._build_url(request)
        payload = build_chat_completion_payload(request, provider_name=self._provider_name)
        wire_messages, _ = prepare_openai_compatible_messages(
            cast(list[dict[str, object]], payload["messages"])
        )
        wire_payload = dict(payload)
        wire_payload["messages"] = wire_messages
        wire_payload["stream"] = True

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            async with client.stream("POST", url, json=wire_payload, headers=headers) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise RuntimeError(f"提供商 HTTP 错误 {response.status_code}：{body[:200]}")  # type: ignore[str-bytes-safe]
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        return
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    if not isinstance(delta, dict):
                        continue
                    if "reasoning_content" in delta and delta["reasoning_content"]:
                        yield ("reasoning", delta["reasoning_content"])
                    if "content" in delta and delta["content"]:
                        yield ("content", delta["content"])
