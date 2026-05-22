from __future__ import annotations

from typing import Any


def _normalize_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, dict):
        for key in ("text", "content", "reasoning_content", "output_text", "value", "refusal"):
            nested = _normalize_text(value.get(key))
            if nested:
                return nested
        return ""

    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            normalized = _normalize_text(item)
            if normalized:
                parts.append(normalized)
        return "\n".join(parts).strip()

    return ""


def extract_assistant_text(message: dict[str, Any]) -> str:
    """Extract assistant-visible text from heterogeneous provider payloads.

    Providers may return message.content as string/list/object, or place usable text
    in fallback fields such as reasoning_content/refusal.
    """

    for key in ("content", "reasoning_content", "output_text", "refusal", "reasoning"):
        text = _normalize_text(message.get(key))
        if text:
            return text
    return ""
