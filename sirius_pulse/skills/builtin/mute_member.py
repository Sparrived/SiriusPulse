"""Built-in NapCat skill for muting a QQ group member."""

from __future__ import annotations

from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.skills.builtin._qq_ops import (
    bridge_error,
    current_group_id,
    failure_from_exception,
    get_adapter,
    group_error,
    success_result,
)

_config = ConfigBuilder()
_config.group("QQ 群管理").add("user_id", type="int", description="要禁言的成员 QQ 号。", required=True)
_config.group("QQ 群管理").add(
    "duration",
    type="int",
    description="禁言秒数；0 表示解除禁言。",
    default=1800,
)

SKILL_META = {
    "name": "mute_member",
    "description": "群聊里有人明确要求禁言/解禁某个成员，且已确认对象和时长时使用；高风险管理操作，需要 Bot 是当前群管理员或群主。",
    "version": "1.0.0",
    "tags": ["napcat", "qq", "group_admin"],
    "admin_required": True,
    "adapter_types": ["napcat"],
    "parameters": _config.build(),
}


async def run(
    user_id: int,
    duration: int = 1800,
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    adapter = get_adapter(bridge)
    if adapter is None:
        return bridge_error("禁言群成员")
    group_id = current_group_id(chat_context)
    if not group_id:
        return group_error("禁言群成员")
    try:
        seconds = max(0, int(duration))
        result = await adapter.set_group_ban(group_id, str(user_id), seconds)
        action = "解除禁言" if seconds == 0 else f"禁言 {seconds} 秒"
        return success_result(
            f"已请求{action}: {user_id}",
            text=f"已{action}：{user_id}",
            group_id=group_id,
            user_id=user_id,
            duration=seconds,
            raw=result,
        )
    except Exception as exc:
        return failure_from_exception("禁言群成员", exc)
