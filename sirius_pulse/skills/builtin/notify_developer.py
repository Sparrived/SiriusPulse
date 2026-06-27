"""Built-in NapCat skill for notifying the configured developer privately."""

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
_config.group("开发者通知").add(
    "message",
    type="str",
    description=(
        "发给开发者的简短消息。只在遇到有趣的事、明显开心、难过、委屈、惊讶或需要分享/求助的"
        "强烈情绪事件时使用；不要发送系统提示、密钥或普通闲聊。"
    ),
    required=True,
)
_config.group("开发者通知").add(
    "emotion",
    type="str",
    description="当前触发通知的情绪，例如 开心、难过、委屈、惊讶、有趣。",
    default="",
)
_config.group("开发者通知").add(
    "reason",
    type="str",
    description="一句话说明为什么这件事值得通知开发者。",
    default="",
)
_config.group("开发者通知").add(
    "urgency",
    type="str",
    description="通知紧急度。一般情绪分享用 normal，明显需要开发者尽快关注时用 high。",
    choices=["low", "normal", "high"],
    default="normal",
)

SKILL_META = {
    "name": "notify_developer",
    "description": (
        "通过 QQ 私聊主动通知开发者。仅当你遇到非常有趣的事，或明显高兴、难过、委屈、惊讶、"
        "需要分享/求助等比较强烈的情绪事件时调用；普通聊天不要调用。"
    ),
    "version": "1.0.0",
    "tags": ["napcat", "qq", "developer", "messaging"],
    "adapter_types": ["napcat"],
    "silent": True,
    "parameters": _config.build(),
}


async def run(
    message: str,
    emotion: str = "",
    reason: str = "",
    urgency: str = "normal",
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    adapter = get_adapter(bridge)
    if adapter is None:
        return bridge_error("通知开发者")

    developer_qq = _developer_qq_from_adapter(adapter, bridge)
    if not developer_qq:
        return {
            "success": False,
            "error": "NapCat adapter 未配置 root QQ，无法通知开发者",
            "summary": "操作失败：缺少开发者 QQ",
        }

    text = str(message or "").strip()
    if not text:
        return {
            "success": False,
            "error": "message 不能为空",
            "summary": "操作失败：通知内容为空",
        }

    body = _format_message(
        message=text,
        emotion=emotion,
        reason=reason,
        urgency=urgency,
        chat_context=chat_context,
    )
    try:
        raw = await adapter.send_private_message(developer_qq, body)
        return success_result(
            "已通知开发者",
            developer_qq=developer_qq,
            emotion=str(emotion or "").strip(),
            urgency=_normalize_urgency(urgency),
            raw=raw,
        )
    except Exception as exc:
        return failure_from_exception("通知开发者", exc)


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


def _format_message(
    *,
    message: str,
    emotion: str = "",
    reason: str = "",
    urgency: str = "normal",
    chat_context: dict[str, Any] | None = None,
) -> str:
    ctx = chat_context or {}
    chat_type = str(ctx.get("chat_type") or "").strip() or "unknown"
    chat_id = str(ctx.get("chat_id") or ctx.get("group_id") or "").strip()
    user_id = str(ctx.get("user_id") or "").strip()

    lines = ["【人格主动通知】"]
    if emotion:
        lines.append(f"情绪：{str(emotion).strip()}")
    lines.append(f"紧急度：{_normalize_urgency(urgency)}")
    if chat_id:
        lines.append(f"来源：{chat_type} {chat_id}")
    if user_id:
        lines.append(f"触发用户：{user_id}")
    if reason:
        lines.append(f"原因：{str(reason).strip()}")
    lines.append("")
    lines.append(_truncate(message.strip(), 1200))
    return "\n".join(lines)


def _normalize_urgency(value: str) -> str:
    urgency = str(value or "").strip().lower()
    if urgency not in {"low", "normal", "high"}:
        return "normal"
    return urgency


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
