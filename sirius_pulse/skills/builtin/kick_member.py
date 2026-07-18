"""Built-in NapCat skill for kicking a QQ group member."""

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
_config.group("QQ 群管理").add(
    "user_id", type="int", description="要踢出的成员 QQ 号。", required=True
)
_config.group("QQ 群管理").add("reason", type="str", description="操作原因，仅用于内部记录。")
_config.group("QQ 群管理").add(
    "reject_add_request",
    type="bool",
    description="是否拒绝该成员后续加群申请。",
    default=False,
)

SKILL_META = {
    "name": "kick_member",
    "description": "群聊里有人明确要求踢出成员，且你已确认对象和理由时使用；高风险管理操作，需要 Bot 是当前群管理员或群主。",
    "version": "1.0.0",
    "tags": ["napcat", "qq", "group_admin"],
    "model_visible": False,
    "admin_required": True,
    "adapter_types": ["napcat"],
    "parameters": _config.build(),
}


async def run(
    user_id: int,
    reason: str = "",
    reject_add_request: bool = False,
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    adapter = get_adapter(bridge)
    if adapter is None:
        return bridge_error("踢出群成员")
    group_id = current_group_id(chat_context)
    if not group_id:
        return group_error("踢出群成员")
    try:
        result = await adapter.set_group_kick(group_id, str(user_id), reject_add_request)
        return success_result(
            f"已请求踢出成员 {user_id}",
            text=f"已踢出成员：{user_id}",
            group_id=group_id,
            user_id=user_id,
            reason=reason,
            raw=result,
        )
    except Exception as exc:
        return failure_from_exception("踢出群成员", exc)
