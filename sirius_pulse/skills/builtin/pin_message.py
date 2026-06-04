"""Built-in skill for pinning recent messages into persistent chat context."""

from __future__ import annotations

from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder

_config = ConfigBuilder()
_config.group("钉住消息").add(
    "msg_id",
    type="str",
    description="要钉住的最近消息 msg_id。只能使用最近消息中真实出现的 msg_id。",
    required=True,
)
_config.group("钉住消息").add(
    "reason",
    type="str",
    description="钉住原因，例如：重要约定、长期规则、待办事项。",
    default="",
)

SKILL_META = {
    "name": "pin_message",
    "description": (
        "把最近聊天中的一条重要消息钉住，让它在后续对话中自动作为上下文携带。"
        "当用户提出长期规则、重要约定、需要稍后持续记住的信息时使用。"
    ),
    "version": "1.0.0",
    "tags": ["memory", "pinned_message"],
    "silent": True,
    "dependencies": [],
    "parameters": _config.build(),
}


def run(
    msg_id: str = "",
    reason: str = "",
    chat_context: dict[str, Any] | None = None,
    engine_context: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if engine_context is None:
        return {
            "success": False,
            "error": "engine_context 未就绪，无法钉住消息",
        }

    group_id = (chat_context or {}).get("group_id", "")
    if not group_id:
        return {
            "success": False,
            "error": "当前聊天上下文缺少 group_id",
        }

    result = engine_context.pin_recent_message_by_id(
        group_id=group_id,
        msg_id=msg_id,
        reason=reason.strip(),
    )
    if result.get("success"):
        pinned = result.get("pinned_message", {})
        return {
            "success": True,
            "summary": result.get("summary", "已钉住消息"),
            "text_blocks": [
                f"已钉住消息：{pinned.get('message_id', '')}".strip()
            ],
            "internal_metadata": result,
        }
    return {
        "success": False,
        "error": str(result.get("error", "钉住消息失败")),
        "internal_metadata": result,
    }
