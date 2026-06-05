"""Built-in skill for listing active pinned messages."""

from __future__ import annotations

from typing import Any

SKILL_META = {
    "name": "list_pinned_messages",
    "description": (
        "查看当前聊天中已经钉住的消息。需要取消钉住、确认现有长期上下文时使用。"
    ),
    "version": "1.0.0",
    "model_visible": False,
    "tags": ["memory", "pinned_message"],
    "dependencies": [],
    "parameters": [],
}


def run(
    chat_context: dict[str, Any] | None = None,
    engine_context: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if engine_context is None:
        return {
            "success": False,
            "error": "engine_context 未就绪，无法查看钉住消息",
        }

    group_id = (chat_context or {}).get("group_id", "")
    if not group_id:
        return {
            "success": False,
            "error": "当前聊天上下文缺少 group_id",
        }

    messages = engine_context.get_pinned_messages(group_id)
    if not messages:
        return {
            "success": True,
            "summary": "当前聊天没有钉住消息",
            "text_blocks": ["当前聊天没有钉住消息。"],
        }

    lines = [f"当前聊天共有 {len(messages)} 条钉住消息："]
    for item in messages:
        speaker = item.get("speaker") or "系统"
        reason = item.get("reason") or "无原因"
        content = str(item.get("content", "")).replace("\n", " ")
        lines.append(
            f"- {item.get('message_id')}: {speaker} | {reason} | {content[:120]}"
        )

    return {
        "success": True,
        "summary": f"列出 {len(messages)} 条钉住消息",
        "text_blocks": ["\n".join(lines)],
        "internal_metadata": {"messages": messages},
    }
