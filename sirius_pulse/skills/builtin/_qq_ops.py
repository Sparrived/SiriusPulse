"""Shared helpers for built-in QQ/NapCat operation skills."""

from __future__ import annotations

from typing import Any


def get_adapter(bridge: Any) -> Any | None:
    if bridge is None:
        return None
    return getattr(bridge, "adapter", None) or bridge


def current_group_id(chat_context: dict[str, Any] | None, explicit_group_id: int | str | None = None) -> str:
    if explicit_group_id not in (None, ""):
        return str(explicit_group_id).strip()
    ctx = chat_context or {}
    chat_type = str(ctx.get("chat_type", "") or "")
    if chat_type and chat_type != "group":
        return ""
    return str(ctx.get("chat_id") or ctx.get("group_id") or "").strip()


def bridge_error(action: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": f"bridge 未就绪，无法{action}",
        "summary": "操作失败：NapCat 桥接未初始化",
    }


def group_error(action: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": f"当前不是群聊，无法{action}",
        "summary": "操作失败：缺少群聊上下文",
    }


def success_result(summary: str, *, text: str = "", **metadata: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": True,
        "summary": summary,
        "internal_metadata": metadata,
    }
    if text:
        result["text_blocks"] = [text]
    return result


def member_display(member: dict[str, Any]) -> str:
    card = str(member.get("card", "") or "").strip()
    nickname = str(member.get("nickname", "") or "").strip()
    user_id = str(member.get("user_id", "") or "").strip()
    if nickname and card and nickname != card:
        return f"{nickname}(群昵称：{card})"
    return card or nickname or f"qq_{user_id}"


def format_member(member: dict[str, Any]) -> str:
    user_id = str(member.get("user_id", "") or "").strip()
    role = str(member.get("role", "") or "").strip()
    title = str(member.get("title", "") or "").strip()
    suffix = "，".join(part for part in (role, title) if part)
    suffix_text = f" ({suffix})" if suffix else ""
    return f"- {user_id}: {member_display(member)}{suffix_text}"


def failure_from_exception(action: str, exc: Exception) -> dict[str, Any]:
    return {
        "success": False,
        "error": str(exc),
        "summary": f"{action}失败：{exc}",
    }
