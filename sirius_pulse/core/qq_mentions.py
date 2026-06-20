"""QQ mention prompt and output parsing helpers."""

from __future__ import annotations

import re
from typing import Any, Iterable

from sirius_pulse.adapters.models import AtSegment, MessageGroup, TextSegment

QQ_AT_PATTERN = re.compile(r"@\{(\d+)\}|@(qq_)?(\d{5,12})(?!\d)")


def normalize_qq_member(member: dict[str, Any]) -> dict[str, str]:
    """Return a small, prompt-safe member mapping."""
    user_id = str(member.get("user_id", "") or member.get("qq", "") or "").strip()
    card = str(member.get("card", "") or "").strip()
    nickname = str(member.get("nickname", "") or "").strip()
    role = str(member.get("role", "") or "").strip()
    title = str(member.get("title", "") or "").strip()
    display_name = card or nickname or f"qq_{user_id}"
    return {
        "user_id": user_id,
        "display_name": display_name,
        "nickname": nickname,
        "card": card,
        "role": role,
        "title": title,
    }


def build_qq_mention_section(
    members: Iterable[dict[str, Any]],
    *,
    max_members: int = 80,
) -> str:
    """Build a compact prompt section teaching inline QQ at syntax."""
    return (
        "【QQ @提及】"
        "\n在回复正文中插入 @{QQ号} 可以 @ 某个群成员，发送前会自动转换成真正的 @。"
        "\n从上下文消息中获取发送者的 QQ 号来使用，不要编造 QQ 号。"
    )


def _matched_qq_id(match: re.Match[str]) -> str:
    return match.group(1) or match.group(3)


def parse_qq_at_mentions(
    text: str,
    *,
    valid_user_ids: set[str] | None = None,
) -> MessageGroup | None:
    """Parse inline QQ mention markers into MessageGroup at segments.

    Supports both the preferred ``@{123}`` form and common model output like
    ``@123`` or ``@qq_123``. Unknown IDs are left as literal text when a valid
    ID set is provided.
    Returns None when the text contains no mention marker.
    """
    if not text or QQ_AT_PATTERN.search(text) is None:
        return None

    segments: list[Any] = []
    cursor = 0
    for match in QQ_AT_PATTERN.finditer(text):
        if match.start() > cursor:
            segments.append(TextSegment(text[cursor : match.start()]))

        user_id = _matched_qq_id(match)
        if valid_user_ids is None or user_id in valid_user_ids:
            segments.append(AtSegment(user_id))
        else:
            segments.append(TextSegment(match.group(0)))
        cursor = match.end()

    if cursor < len(text):
        segments.append(TextSegment(text[cursor:]))

    return MessageGroup(segments)
