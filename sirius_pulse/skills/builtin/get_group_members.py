"""Built-in NapCat skill for listing QQ group members."""

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
)

_config = ConfigBuilder()
_config.group("QQ 信息").add(
    "group_id",
    type="int",
    description="群号；不填则使用当前群聊。",
)

SKILL_META = {
    "name": "get_group_members",
    "description": "获取 QQ 群成员列表，用于查找成员 QQ 号、群昵称或管理员身份。",
    "version": "1.0.0",
    "tags": ["napcat", "qq", "group_info"],
    "adapter_types": ["napcat"],
    "parameters": _config.build(),
}


async def run(
    group_id: int | str | None = None,
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    adapter = get_adapter(bridge)
    if adapter is None:
        return bridge_error("获取群成员列表")
    gid = current_group_id(chat_context, group_id)
    if not gid:
        return group_error("获取群成员列表")
    try:
        members = await adapter.get_group_member_list(gid)
        shown = members[:80]
        lines = [f"群 {gid} 成员共 {len(members)} 人，以下显示前 {len(shown)} 人："]
        lines.extend(format_member(member) for member in shown)
        if len(members) > len(shown):
            lines.append(f"...还有 {len(members) - len(shown)} 人未显示")
        return {
            "success": True,
            "summary": f"已获取群 {gid} 成员 {len(members)} 人",
            "text_blocks": ["\n".join(lines)],
            "internal_metadata": {"group_id": gid, "members": members},
        }
    except Exception as exc:
        return failure_from_exception("获取群成员列表", exc)
