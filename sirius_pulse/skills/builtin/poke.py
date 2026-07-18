"""Built-in NapCat skill for poking a QQ group member."""

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
_config.group("QQ 操作").add(
    "user_id",
    type="int",
    description="要戳一戳的 QQ 号。",
    required=True,
)

SKILL_META = {
    "name": "poke",
    "description": "群聊里适合轻轻提醒、打招呼、撒娇、催一下或回应“戳他/戳我”时使用；要配合文字一起用，别无缘无故单独戳。",
    "version": "1.0.0",
    "tags": ["napcat", "qq", "messaging"],
    "model_visible": False,
    "adapter_types": ["napcat"],
    "parameters": _config.build(),
}


async def run(
    user_id: int,
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    adapter = get_adapter(bridge)
    if adapter is None:
        return bridge_error("戳一戳")
    group_id = current_group_id(chat_context)
    if not group_id:
        return group_error("戳一戳")
    try:
        result = await adapter.send_poke(str(user_id), group_id)
        return success_result("已发送戳一戳", user_id=user_id, group_id=group_id, raw=result)
    except Exception as exc:
        return failure_from_exception("戳一戳", exc)
