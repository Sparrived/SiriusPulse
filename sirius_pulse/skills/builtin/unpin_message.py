"""Built-in skill for removing pinned chat messages."""

from __future__ import annotations

from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder

_config = ConfigBuilder()
_config.group("取消钉住").add(
    "message_id",
    type="str",
    description="要取消钉住的 pinned message_id，例如 pin_1710000000000_0。",
    required=True,
)

SKILL_META = {
    "name": "unpin_message",
    "description": "取消一条已经钉住的消息。当钉住内容过期、已完成或不再重要时使用。",
    "version": "1.0.0",
    "tags": ["memory", "pinned_message"],
    "silent": True,
    "dependencies": [],
    "parameters": _config.build(),
}


def run(
    message_id: str = "",
    engine_context: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if engine_context is None:
        return {
            "success": False,
            "error": "engine_context 未就绪，无法取消钉住",
        }

    result = engine_context.unpin_message(message_id)
    if result.get("success"):
        return {
            "success": True,
            "summary": result.get("summary", "已取消钉住"),
            "internal_metadata": result,
        }
    return {
        "success": False,
        "error": str(result.get("error", "取消钉住失败")),
        "internal_metadata": result,
    }
