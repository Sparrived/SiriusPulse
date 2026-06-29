"""Built-in NapCat skill for recalling a QQ message."""

from __future__ import annotations

from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.skills.builtin._qq_ops import bridge_error, failure_from_exception, get_adapter, success_result

_config = ConfigBuilder()
_config.group("QQ 操作").add(
    "message_id",
    type="int",
    description="要撤回的 QQ/OneBot message_id，只能填写最近消息里真实出现的 msg_id。",
    required=True,
)

SKILL_META = {
    "name": "recall_message",
    "description": "群聊里需要撤回刚发错、误发或不该继续展示的指定消息时使用；只能撤回最近真实存在且平台仍允许撤回的消息。",
    "version": "1.0.0",
    "tags": ["napcat", "qq", "message_admin"],
    "adapter_types": ["napcat"],
    "parameters": _config.build(),
}


async def run(message_id: int, bridge: Any = None, **kwargs: Any) -> dict[str, Any]:
    adapter = get_adapter(bridge)
    if adapter is None:
        return bridge_error("撤回消息")
    try:
        result = await adapter.delete_message(str(message_id))
        return success_result(
            f"已请求撤回消息 {message_id}",
            text=f"已撤回消息：{message_id}",
            message_id=message_id,
            raw=result,
        )
    except Exception as exc:
        return failure_from_exception("撤回消息", exc)
