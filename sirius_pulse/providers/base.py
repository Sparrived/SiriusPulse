from __future__ import annotations

import base64
import logging
import mimetypes
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

# 默认超时秒数（各 provider 构造函数共用）
DEFAULT_TIMEOUT_SECONDS: int = 30

# Thread-local storage for passing real token usage from provider back to caller.
_LAST_GENERATION_USAGE: threading.local = threading.local()


def set_last_generation_usage(usage: dict[str, Any] | None) -> None:
    """Store the last provider response usage dict (e.g. {"prompt_tokens": 42, "completion_tokens": 7})."""
    _LAST_GENERATION_USAGE.usage = usage


def get_last_generation_usage() -> dict[str, Any] | None:
    """Retrieve and clear the last stored usage dict."""
    usage = getattr(_LAST_GENERATION_USAGE, "usage", None)
    _LAST_GENERATION_USAGE.usage = None
    return usage


@dataclass(slots=True)
class ToolCall:
    """表示一个 function_call 工具调用。"""

    id: str
    type: str = "function"
    function_name: str = ""
    function_arguments: str = ""


@dataclass(slots=True)
class GenerationResult:
    """LLM 生成结果，支持普通文本和 tool_calls。"""

    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    finish_reason: str = "stop"

    @property
    def has_tool_calls(self) -> bool:
        """是否有工具调用。"""
        return bool(self.tool_calls)


@dataclass(slots=True)
class GenerationRequest:
    model: str
    system_prompt: str
    messages: list[dict[str, object]]
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | None = None
    temperature: float = 0.7
    max_tokens: int = 512
    timeout_seconds: float | None = None
    purpose: str = "chat_main"
    response_format: dict[str, object] | None = None


def estimate_generation_request_input_tokens(request: GenerationRequest) -> int:
    """Estimate input tokens for logging and budget visibility.

    Uses tiktoken (preferred) or CJK-aware heuristic fallback.
    """
    from sirius_pulse.token.utils import estimate_tokens

    text_parts = [request.system_prompt]
    for msg in request.messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts.extend(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
            continue
        text_parts.append(str(content))
    merged = "\n".join(part for part in text_parts if part)
    if not merged:
        return 0
    return estimate_tokens(merged)


def build_generation_debug_context(
    request: GenerationRequest,
    *,
    provider_name: str,
    url: str = "",
    base_url: str = "",
    timeout_seconds: float | None = None,
    method: str = "POST",
) -> dict[str, object]:
    """Build structured debug metadata for upstream provider calls."""
    estimated_input_tokens = estimate_generation_request_input_tokens(request)
    estimated_total_upper = estimated_input_tokens + max(0, int(request.max_tokens))

    multimodal_message_count = 0
    multimodal_part_count = 0
    text_part_count = 0
    for msg in request.messages:
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        multimodal_message_count += 1
        multimodal_part_count += len(content)
        text_part_count += sum(
            1
            for part in content
            if isinstance(part, dict) and str(part.get("type", "")).strip() == "text"
        )

    return {
        "provider": provider_name,
        "method": method,
        "url": url,
        "base_url": base_url,
        "timeout_seconds": timeout_seconds,
        "purpose": request.purpose,
        "model": request.model,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "input_message_count": len(request.messages),
        "total_message_count": len(request.messages) + (1 if request.system_prompt else 0),
        "multimodal_message_count": multimodal_message_count,
        "multimodal_part_count": multimodal_part_count,
        "multimodal_text_part_count": text_part_count,
        "has_system_prompt": bool(request.system_prompt),
        "system_prompt_chars": len(request.system_prompt),
        "estimated_input_tokens": estimated_input_tokens,
        "estimated_total_token_upper_bound": estimated_total_upper,
    }


def _build_thinking_disabled_defaults(provider_name: str) -> dict[str, object]:
    normalized_name = provider_name.strip().lower()
    if normalized_name in {"aliyun-bailian", "siliconflow"}:
        return {"enable_thinking": False}
    if normalized_name in {"deepseek", "bigmodel", "volcengine-ark"}:
        return {"thinking": {"type": "disabled"}}
    return {}


def build_chat_completion_payload(
    request: GenerationRequest,
    *,
    provider_name: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": request.model,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "messages": [
            {"role": "system", "content": request.system_prompt},
            *request.messages,
        ],
    }
    if request.response_format is not None:
        payload["response_format"] = request.response_format
    if request.tools is not None:
        payload["tools"] = request.tools
    if request.tool_choice is not None:
        payload["tool_choice"] = request.tool_choice
    payload.update(_build_thinking_disabled_defaults(provider_name))
    return payload


def _resolve_local_file_reference(value: str) -> Path | None:
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith(("http://", "https://", "data:")):
        return None
    if lowered.startswith("file://"):
        parsed = urlparse(text)
        raw_path = unquote(parsed.path or "")
        if parsed.netloc:
            raw_path = f"//{parsed.netloc}{raw_path}"
        if raw_path.startswith("/") and len(raw_path) >= 3 and raw_path[2] == ":":
            raw_path = raw_path[1:]
        candidate = Path(raw_path)
        return candidate if candidate.is_file() else None

    candidate = Path(text).expanduser()
    return candidate if candidate.is_file() else None


def _file_to_data_url(file_path: Path, *, default_mime: str) -> str:
    mime_type, _ = mimetypes.guess_type(file_path.name)
    resolved_mime = str(mime_type or default_mime).strip() or default_mime
    if default_mime.startswith("image/") and not resolved_mime.startswith("image/"):
        resolved_mime = default_mime
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{resolved_mime};base64,{encoded}"


def prepare_openai_compatible_messages(
    messages: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, int]]:
    """Normalize OpenAI-compatible multimodal messages for transport.

    Local image paths are converted to data URLs so OpenAI-compatible HTTP
    endpoints can consume them without requiring SDK-specific file upload APIs.
    """
    prepared_messages: list[dict[str, object]] = []
    local_image_path_conversions = 0

    for message in messages:
        prepared_message = dict(message)
        content = prepared_message.get("content")
        if not isinstance(content, list):
            prepared_messages.append(prepared_message)
            continue

        prepared_parts: list[object] = []
        for part in content:
            if not isinstance(part, dict):
                prepared_parts.append(part)
                continue

            prepared_part = dict(part)
            if str(prepared_part.get("type", "")).strip() == "image_url":
                image_url = prepared_part.get("image_url")
                if isinstance(image_url, dict):
                    prepared_image_url = dict(image_url)
                    raw_url = str(prepared_image_url.get("url", "")).strip()
                    local_file = _resolve_local_file_reference(raw_url)
                    if local_file is not None:
                        prepared_image_url["url"] = _file_to_data_url(
                            local_file,
                            default_mime="image/jpeg",
                        )
                        prepared_part["image_url"] = prepared_image_url
                        local_image_path_conversions += 1
                    elif not raw_url.lower().startswith(("http://", "https://", "data:")):
                        # 跳过无效的图片 URL（例如已清理的本地缓存路径），
                        # 避免提供商返回 400 Bad Request。
                        logger.warning("跳过无效的图片 URL（非本地文件且非网络地址）: %s", raw_url)
                        continue
            prepared_parts.append(prepared_part)

        prepared_message["content"] = prepared_parts
        prepared_messages.append(prepared_message)

    return prepared_messages, {
        "local_image_path_conversions": local_image_path_conversions,
    }


def resolve_generation_timeout_seconds(
    request: GenerationRequest,
    default_timeout_seconds: float,
) -> float:
    """Return the effective timeout for a provider call.

    Request-scoped timeout overrides provider defaults when supplied.
    """
    timeout_seconds = request.timeout_seconds
    if timeout_seconds is None:
        timeout_seconds = default_timeout_seconds
    resolved_timeout = float(timeout_seconds)
    if resolved_timeout <= 0:
        raise ValueError("GenerationRequest.timeout_seconds must be greater than 0.")
    return resolved_timeout


@runtime_checkable
class LLMProvider(Protocol):
    def generate(self, request: GenerationRequest) -> str:
        """Generate one assistant message from the upstream provider."""
        ...


@runtime_checkable
class AsyncLLMProvider(Protocol):
    async def generate_async(
        self, request: GenerationRequest, return_reasoning: bool = False
    ) -> GenerationResult | tuple[str, GenerationResult]:
        """Generate one assistant message asynchronously from the upstream provider.

        When return_reasoning=True, returns (reasoning_content, GenerationResult) tuple.
        """
        ...
