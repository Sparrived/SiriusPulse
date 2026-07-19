"""Helpers for deferring sticker delivery until after text replies."""

from __future__ import annotations

import json
from typing import Any


def normalize_sticker_names(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace("，", ",")
        return [part.strip().strip("'\"") for part in raw.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip().strip("'\"") for item in value if str(item).strip()]
    return []


def dedupe_sticker_names(names: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def defer_interaction_sticker_tool(
    params: dict[str, Any],
    *,
    available_names: list[str] | set[str] | tuple[str, ...],
) -> tuple[list[str], str]:
    """Return sticker candidates and a tool message for a deferred interaction call."""
    candidates = normalize_sticker_names(params.get("names"))
    if not candidates:
        return [], "names 不能为空"

    available = set(available_names)
    filtered = [name for name in candidates if name in available]
    if not filtered:
        preview = ", ".join(sorted(available)[:30])
        return [], f"没有匹配的表情包名称，可选名称：{preview}"

    selected = filtered[:3]
    return selected, f"表情包将在正文发送后发送：{', '.join(selected)}"


def collect_deferred_stickers_from_tool_calls(
    tool_calls: list[Any] | tuple[Any, ...] | None,
    *,
    available_names: list[str] | set[str] | tuple[str, ...],
) -> list[str]:
    names: list[str] = []
    for tool_call in tool_calls or []:
        function_name = getattr(tool_call, "function_name", "")
        if function_name != "interaction":
            continue
        try:
            params = json.loads(getattr(tool_call, "function_arguments", "") or "{}")
        except Exception:
            params = {}
        if str(params.get("action", "")).strip().lower() != "sticker":
            continue
        selected, _tool_content = defer_interaction_sticker_tool(
            params,
            available_names=available_names,
        )
        names.extend(selected)
    return dedupe_sticker_names(names)
