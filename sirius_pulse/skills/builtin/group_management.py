"""Unified NapCat skill for QQ group administration."""

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
from sirius_pulse.skills.models import SkillInvocationContext

_config = ConfigBuilder()
_config.group("QQ群管理").add(
    "action",
    type="str",
    description=(
        "操作类型：kick 踢人；mute_member 禁言成员；mute_all 全员禁言；"
        "set_group_card 修改群名片。只有当当前角色确实承担群管理职责、"
        "对方明确提出管理诉求且已确认对象和参数时才使用。"
    ),
    required=True,
    choices=["kick", "mute_member", "mute_all", "set_group_card"],
)
_config.group("QQ群管理").add("user_id", type="int", description="目标成员 QQ 号。")
_config.group("QQ群管理").add("reason", type="str", description="action=kick 时的内部记录原因。")
_config.group("QQ群管理").add(
    "reject_add_request", type="bool", description="action=kick 时是否拒绝再次加群。", default=False
)
_config.group("QQ群管理").add(
    "duration",
    type="int",
    description="action=mute_member 时的禁言秒数；0 表示解除。",
    default=1800,
)
_config.group("QQ群管理").add(
    "enable", type="bool", description="action=mute_all 时是否开启全员禁言。", default=True
)
_config.group("QQ群管理").add(
    "card", type="str", description="action=set_group_card 时的新群名片。"
)

SKILL_META = {
    "name": "group_management",
    "description": (
        "以当前人格参与群聊时的管理工具：当角色需要履行明确的群管理职责，且对方已清楚"
        "提出踢人、禁言、全员禁言或修改群名片的请求时调用；先确认对象、时长和动作，"
        "不要把管理动作只写成文字，也不要为了增强角色感而滥用。需要 Bot 是当前群管理员或群主。"
    ),
    "version": "1.0.0",
    "side_effect": "destructive",
    "tags": ["napcat", "qq", "group_admin"],
    "admin_required": True,
    "adapter_types": ["napcat"],
    "parameters": _config.build(),
}


async def run(
    action: str,
    user_id: int | None = None,
    reason: str = "",
    reject_add_request: bool = False,
    duration: int = 1800,
    enable: bool = True,
    card: str = "",
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    invocation_context: SkillInvocationContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    action_key = str(action or "").strip().lower()
    if action_key == "kick":
        result = await _kick_member(user_id, reason, reject_add_request, bridge, chat_context)
    elif action_key == "mute_member":
        result = await _mute_member(user_id, duration, bridge, chat_context)
    elif action_key == "mute_all":
        result = await _mute_all(enable, bridge, chat_context)
    elif action_key == "set_group_card":
        result = await _set_group_card(user_id, card, bridge, chat_context)
    else:
        return {"success": False, "error": f"不支持的管理 action: {action}"}

    metadata = result.get("internal_metadata")
    result["internal_metadata"] = {
        **(metadata if isinstance(metadata, dict) else {}),
        "management_action": action_key,
    }
    return result


async def _kick_member(
    user_id: int | None,
    reason: str,
    reject_add_request: bool,
    bridge: Any,
    chat_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if user_id is None:
        return {"success": False, "error": "缺少必填参数: user_id"}
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


async def _mute_member(
    user_id: int | None,
    duration: int,
    bridge: Any,
    chat_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if user_id is None:
        return {"success": False, "error": "缺少必填参数: user_id"}
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


async def _mute_all(
    enable: bool,
    bridge: Any,
    chat_context: dict[str, Any] | None,
) -> dict[str, Any]:
    adapter = get_adapter(bridge)
    if adapter is None:
        return bridge_error("设置全员禁言")
    group_id = current_group_id(chat_context)
    if not group_id:
        return group_error("设置全员禁言")
    try:
        result = await adapter.set_group_whole_ban(group_id, bool(enable))
        action = "开启全员禁言" if enable else "关闭全员禁言"
        return success_result(
            f"已请求{action}",
            text=f"已{action}",
            group_id=group_id,
            enable=bool(enable),
            raw=result,
        )
    except Exception as exc:
        return failure_from_exception("设置全员禁言", exc)


async def _set_group_card(
    user_id: int | None,
    card: str,
    bridge: Any,
    chat_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if user_id is None:
        return {"success": False, "error": "缺少必填参数: user_id"}
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
