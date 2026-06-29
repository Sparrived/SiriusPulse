"""Built-in NapCat skill for QQ group member lookup."""

from __future__ import annotations

from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.skills.builtin._qq_ops import (
    bridge_error,
    current_group_id,
    failure_from_exception,
    format_member,
    get_adapter,
    group_error,
    member_display,
)

_config = ConfigBuilder()
_config.group("QQ 成员信息").add(
    "action",
    type="str",
    description="操作类型：list 获取群成员列表；get 获取指定成员详细信息。",
    required=True,
    choices=["list", "get"],
)
_config.group("QQ 成员信息").add(
    "user_id",
    type="int",
    description="要查询的成员 QQ 号；action=get 时必填。",
)
_config.group("QQ 成员信息").add(
    "group_id",
    type="int",
    description="群号；不填则使用当前群聊。",
)

SKILL_META = {
    "name": "qq_member_info",
    "description": (
        "群聊里需要确认谁是谁、查成员 QQ 号/群昵称/管理员身份，或准备 @、戳一戳、管理操作前核对对象时使用；"
        "可列出群成员或查询单个成员详情。"
    ),
    "version": "1.0.0",
    "tags": ["napcat", "qq", "group_info"],
    "adapter_types": ["napcat"],
    "parameters": _config.build(),
}


async def run(
    action: str,
    user_id: int | str | None = None,
    group_id: int | str | None = None,
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    adapter = get_adapter(bridge)
    if adapter is None:
        return bridge_error("获取群成员信息")
    gid = current_group_id(chat_context, group_id)
    if not gid:
        return group_error("获取群成员信息")

    action_key = str(action or "").strip().lower()
    if action_key == "list":
        return await _list_members(adapter, gid)
    if action_key == "get":
        if not user_id:
            return {"success": False, "error": "user_id 不能为空", "summary": "获取成员信息失败"}
        return await _get_member(adapter, gid, str(user_id))
    return {"success": False, "error": "action 必须是 list 或 get"}


async def _list_members(adapter: Any, group_id: str) -> dict[str, Any]:
    try:
        members = await adapter.get_group_member_list(group_id)
        shown = members[:80]
        lines = [f"群 {group_id} 成员共 {len(members)} 人，以下显示前 {len(shown)} 人："]
        lines.extend(format_member(member) for member in shown)
        if len(members) > len(shown):
            lines.append(f"...还有 {len(members) - len(shown)} 人未显示")
        return {
            "success": True,
            "summary": f"已获取群 {group_id} 成员 {len(members)} 人",
            "text_blocks": ["\n".join(lines)],
            "internal_metadata": {"group_id": group_id, "members": members},
        }
    except Exception as exc:
        return failure_from_exception("获取群成员列表", exc)


async def _get_member(adapter: Any, group_id: str, user_id: str) -> dict[str, Any]:
    try:
        info = await adapter.get_group_member_info(group_id, user_id)
        lines = [
            f"QQ {user_id}: {member_display(info)}",
            f"- role: {info.get('role', '')}",
            f"- title: {info.get('title', '')}",
            f"- level: {info.get('level', '')}",
            f"- sex: {info.get('sex', '')}",
            f"- age: {info.get('age', '')}",
        ]
        return {
            "success": True,
            "summary": f"已获取成员 {user_id} 信息",
            "text_blocks": ["\n".join(lines)],
            "internal_metadata": {"group_id": group_id, "user_id": user_id, "member": info},
        }
    except Exception as exc:
        return failure_from_exception("获取成员信息", exc)
