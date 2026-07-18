"""Built-in NapCat skill for toggling whole-group mute."""

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
    "enable",
    type="bool",
    description="True 开启全员禁言；False 关闭全员禁言。",
    default=True,
)

SKILL_META = {
    "name": "mute_all",
    "description": "群聊里有人明确要求开启或解除全员禁言，且场景确实需要控场时使用；高风险管理操作，需要 Bot 是当前群管理员或群主。",
    "version": "1.0.0",
    "tags": ["napcat", "qq", "group_admin"],
    "model_visible": False,
    "admin_required": True,
    "adapter_types": ["napcat"],
    "parameters": _config.build(),
}


async def run(
    enable: bool = True,
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    **kwargs: Any,
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
