"""QQ mention prompt and output parsing helpers."""

from __future__ import annotations

import re
from typing import Any, Iterable

from sirius_pulse.adapters.models import AtSegment, MessageGroup, TextSegment

QQ_AT_PATTERN = re.compile(r"@\{(\d+)\}")


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
    normalized = [
        item for item in (normalize_qq_member(member) for member in members) if item["user_id"]
    ]
    if not normalized:
        return ""

    shown = normalized[:max_members]
    lines = [
        "【QQ @提及】",
        "当前 QQ 群聊可以在回复正文中插入 @{QQ号} 来 @ 某个群成员；这个标记会在发送前自动转换成真正的 @。只能使用下列 QQ 号，不要编造。",
    ]
    for member in shown:
        suffix_parts = []
        if member["role"] in {"owner", "admin"}:
            suffix_parts.append(member["role"])
        if member["title"]:
            suffix_parts.append(member["title"])
        suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
        lines.append(f"- {member['user_id']}: {member['display_name']}{suffix}")
    remaining = len(normalized) - len(shown)
    if remaining > 0:
        lines.append(f"- 其余 {remaining} 名成员未列出；未列出的成员不要使用 @ 标记。")
    return "\n".join(lines)


def parse_qq_at_mentions(
    text: str,
    *,
    valid_user_ids: set[str] | None = None,
) -> MessageGroup | None:
    """Parse inline ``@{123}`` markers into MessageGroup at segments.

    Unknown IDs are left as literal text when a valid ID set is provided.
    Returns None when the text contains no mention marker.
    """
    if not text or QQ_AT_PATTERN.search(text) is None:
        return None

    segments: list[Any] = []
    cursor = 0
    for match in QQ_AT_PATTERN.finditer(text):
        if match.start() > cursor:
            segments.append(TextSegment(text[cursor : match.start()]))

        user_id = match.group(1)
        if valid_user_ids is None or user_id in valid_user_ids:
            segments.append(AtSegment(user_id))
        else:
            segments.append(TextSegment(match.group(0)))
        cursor = match.end()

    if cursor < len(text):
        segments.append(TextSegment(text[cursor:]))

    return MessageGroup(segments)
