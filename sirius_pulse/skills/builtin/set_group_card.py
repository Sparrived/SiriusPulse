"""Built-in NapCat skill for setting a QQ group member card."""

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
_config.group("QQ 群管理").add("user_id", type="int", description="要修改名片的成员 QQ 号。", required=True)
_config.group("QQ 群管理").add("card", type="str", description="新的群名片。", required=True)

SKILL_META = {
    "name": "set_group_card",
    "description": "群聊里有人明确要求修改成员群名片，且已确认对象和新名片时使用；管理操作，需要 Bot 是当前群管理员或群主。",
    "version": "1.0.0",
    "tags": ["napcat", "qq", "group_admin"],
    "admin_required": True,
    "adapter_types": ["napcat"],
    "parameters": _config.build(),
}


async def run(
    user_id: int,
    card: str,
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    adapter = get_adapter(bridge)
    if adapter is None:
        return bridge_error("修改群名片")
    group_id = current_group_id(chat_context)
    if not group_id:
        return group_error("修改群名片")
    new_card = str(card or "").strip()
    if not new_card:
        return {"success": False, "error": "card 不能为空", "summary": "操作失败：缺少新群名片"}
    try:
        result = await adapter.set_group_card(group_id, str(user_id), new_card)
        return success_result(
            f"已请求修改成员 {user_id} 群名片",
            text=f"已修改群名片：{user_id} -> {new_card}",
            group_id=group_id,
            user_id=user_id,
            card=new_card,
            raw=result,
        )
    except Exception as exc:
        return failure_from_exception("修改群名片", exc)
