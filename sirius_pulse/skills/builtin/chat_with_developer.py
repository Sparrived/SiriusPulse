"""Built-in NapCat skill for casual private messages to the developer."""

from __future__ import annotations

from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.skills.builtin._qq_ops import (
    bridge_error,
    failure_from_exception,
    get_adapter,
    success_result,
)

_config = ConfigBuilder()
_config.group("开发者私聊").add(
    "message",
    type="str",
    description=(
        "想私聊发给开发者的话。可以是闲聊、分享好玩的事、吐槽、开心、难过、委屈、求助，"
        "像给熟人发 QQ 一样自然写；不要泄露系统提示、密钥或隐私。"
    ),
    required=True,
)

SKILL_META = {
    "name": "chat_with_developer",
    "description": (
        "群聊外想私下和开发者说一声时使用；适合反馈异常、求助、吐槽、分享有趣情况、"
        "撒娇或表达委屈，内容要像自然 QQ 私聊。"
    ),
    "version": "1.0.0",
    "side_effect": "external_write",
    "tags": ["napcat", "qq", "developer", "chat", "messaging"],
    "adapter_types": ["napcat"],
    "silent": True,
    "parameters": _config.build(),
}


async def run(
    message: str,
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    adapter = get_adapter(bridge)
    if adapter is None:
        return bridge_error("和开发者私聊")

    developer_qq = _developer_qq_from_adapter(adapter, bridge)
    if not developer_qq:
        return {
            "success": False,
            "error": "NapCat adapter 未配置 root QQ，无法和开发者私聊",
            "summary": "操作失败：缺少开发者 QQ",
        }

    text = str(message or "").strip()
    if not text:
        return {
            "success": False,
            "error": "message 不能为空",
            "summary": "操作失败：私聊内容为空",
        }

    body = _truncate(text, 1200)
    try:
        raw = await adapter.send_private_message(developer_qq, body)
        return success_result(
            "已发给开发者",
            developer_qq=developer_qq,
            chat_context=dict(chat_context or {}),
            raw=raw,
        )
    except Exception as exc:
        return failure_from_exception("和开发者私聊", exc)


def _developer_qq_from_adapter(adapter: Any, bridge: Any = None) -> str:
    for source in (adapter, bridge):
        if source is None:
            continue
        for attr in ("plugin_config", "config"):
            cfg = getattr(source, attr, None)
            if not isinstance(cfg, dict):
                continue
            value = str(cfg.get("root", "") or "").strip()
            if value:
                return value
    return ""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
