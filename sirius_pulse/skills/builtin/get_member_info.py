"""Built-in NapCat skill for reading one QQ group member profile."""

from __future__ import annotations

from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.skills.builtin._qq_ops import (
    bridge_error,
    current_group_id,
    failure_from_exception,
    get_adapter,
    group_error,
    member_display,
)

_config = ConfigBuilder()
_config.group("QQ 信息").add(
    "user_id",
    type="int",
    description="要查询的成员 QQ 号。",
    required=True,
)
_config.group("QQ 信息").add(
    "group_id",
    type="int",
    description="群号；不填则使用当前群聊。",
)

SKILL_META = {
    "name": "get_member_info",
    "description": "获取指定 QQ 群成员的详细信息。",
    "version": "1.0.0",
    "tags": ["napcat", "qq", "group_info"],
    "adapter_types": ["napcat"],
    "parameters": _config.build(),
}


async def run(
    user_id: int,
    group_id: int | str | None = None,
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    adapter = get_adapter(bridge)
    if adapter is None:
        return bridge_error("获取成员信息")
    gid = current_group_id(chat_context, group_id)
    if not gid:
        return group_error("获取成员信息")
    try:
        info = await adapter.get_group_member_info(gid, str(user_id))
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
            "internal_metadata": {"group_id": gid, "user_id": user_id, "member": info},
        }
    except Exception as exc:
        return failure_from_exception("获取成员信息", exc)
