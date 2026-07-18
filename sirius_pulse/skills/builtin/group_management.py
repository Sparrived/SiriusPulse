"""Unified NapCat skill for QQ group administration."""

from __future__ import annotations

from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.skills.models import SkillInvocationContext, SkillResult

_config = ConfigBuilder()
_config.group("QQ群管理").add(
    "action",
    type="str",
    description="操作类型：kick 踢人；mute_member 禁言成员；mute_all 全员禁言；set_group_card 修改群名片。",
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
    "description": "统一处理 QQ 群管理员操作：踢人、禁言、全员禁言和修改群名片。需要 Bot 是当前群管理员或群主。",
    "version": "1.0.0",
    "tags": ["napcat", "qq", "group_admin"],
    "admin_required": True,
    "adapter_types": ["napcat"],
    "parameters": _config.build(),
}

_ACTION_SKILLS = {
    "kick": "kick_member",
    "mute_member": "mute_member",
    "mute_all": "mute_all",
    "set_group_card": "set_group_card",
}


async def run(
    action: str,
    user_id: int | None = None,
    reason: str = "",
    reject_add_request: bool = False,
    duration: int = 1800,
    enable: bool = True,
    card: str = "",
    engine_context: Any = None,
    invocation_context: SkillInvocationContext | None = None,
    **kwargs: Any,
) -> SkillResult | dict[str, Any]:
    action_key = str(action or "").strip().lower()
    target_name = _ACTION_SKILLS.get(action_key)
    if target_name is None:
        return {"success": False, "error": f"不支持的管理 action: {action}"}

    executor = getattr(engine_context, "skill_executor", None)
    registry = getattr(engine_context, "skill_registry", None)
    target = registry.get(target_name) if registry is not None else None
    if executor is None or target is None:
        return {"success": False, "error": f"内部管理 Skill 未就绪: {target_name}"}

    params: dict[str, Any] = {}
    if action_key in {"kick", "mute_member", "set_group_card"}:
        if user_id is not None:
            params["user_id"] = user_id
    if action_key == "kick":
        params.update(reason=reason, reject_add_request=reject_add_request)
    elif action_key == "mute_member":
        params["duration"] = duration
    elif action_key == "mute_all":
        params["enable"] = enable
    else:
        params["card"] = card

    result = await executor.execute_async(target, params, invocation_context=invocation_context)
    result.internal_metadata = {
        **result.internal_metadata,
        "management_action": action_key,
        "management_skill": target_name,
    }
    return result
