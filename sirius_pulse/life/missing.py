"""People she knows: one entry per person, summed up across every chat they share.

Every chat keeps its own semantic profile of the people in it; this module folds
those rows into one :class:`Acquaintance` per person -- when they last showed up,
how close she is to them, and which chats she could reach them in.  What to do
with that is the caller's decision, not this module's.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

#: Interactions after which someone counts as fully familiar (matches the
#: semantic profile's familiarity curve: 50 messages ≈ 0.96).
_FAMILIAR_AT = 50

# How much each signal weighs in closeness.  Familiarity is "we talk a lot";
# engagement is "they actually answer me"; warmth is how she feels about them.
_W_FAMILIARITY = 0.6
_W_ENGAGEMENT = 0.4
_W_WARMTH = 0.2


@dataclass(slots=True)
class Acquaintance:
    """Someone she has talked with, summed up across every chat they share."""

    user_id: str
    name: str = ""
    last_active_at: str = ""
    """The last message they sent anywhere she was (group or private chat)."""
    interaction_count: int = 0
    engagement_rate: float = 0.0
    warmth: float = 0.0
    """Her own leaning toward them (``user_bias`` valence), -1..1; 0 when unknown."""
    chats: list[str] = field(default_factory=list)
    """Chats they have talked in, most recent first."""

    @property
    def closeness(self) -> float:
        familiarity = min(
            1.0, math.log1p(max(0, self.interaction_count)) / math.log1p(_FAMILIAR_AT)
        )
        score = (
            _W_FAMILIARITY * familiarity
            + _W_ENGAGEMENT * _clamp(self.engagement_rate, 0.0, 1.0)
            + _W_WARMTH * _clamp(self.warmth, -1.0, 1.0)
        )
        return round(_clamp(score, 0.0, 1.0), 4)

    def days_away(self, now: datetime) -> float:
        """Days since they last showed up; infinity when never seen."""
        last = _parse_time(self.last_active_at)
        if last is None:
            return math.inf
        return max(0.0, (now - last).total_seconds() / 86400)


def gather(
    rows: Iterable[Mapping[str, Any]],
    *,
    bias: Mapping[str, Any] | None = None,
) -> list[Acquaintance]:
    """Merge per-chat profile rows into one entry per person.

    *rows* are semantic profile rows (``group_id``, ``user_id``, ``name``,
    ``interaction_count``, ``engagement_rate``, ``last_interaction_at``); *bias*
    is ``AssistantEmotionState.user_bias`` (``user_id -> (valence, arousal)``).
    """
    people: dict[str, Acquaintance] = {}
    seen: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        user_id = str(row.get("user_id", "") or "").strip()
        if not user_id:
            continue
        person = people.setdefault(user_id, Acquaintance(user_id=user_id))
        name = str(row.get("name", "") or "").strip()
        stamp = str(row.get("last_interaction_at", "") or "")
        if name and (not person.name or _later(stamp, person.last_active_at)):
            person.name = name
        if _later(stamp, person.last_active_at):
            person.last_active_at = stamp
        person.interaction_count += _int(row.get("interaction_count"))
        person.engagement_rate = max(person.engagement_rate, _float(row.get("engagement_rate")))
        chat = str(row.get("group_id", "") or "").strip()
        if chat:
            seen.setdefault(user_id, []).append((stamp, chat))
    for user_id, person in people.items():
        ordered = sorted(seen.get(user_id, []), key=lambda item: _sort_key(item[0]), reverse=True)
        person.chats = [chat for _, chat in ordered]
        leaning = (bias or {}).get(user_id)
        if isinstance(leaning, (list, tuple)) and leaning:
            person.warmth = _float(leaning[0])
    return list(people.values())


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _sort_key(value: str) -> float:
    parsed = _parse_time(value)
    return parsed.timestamp() if parsed else 0.0


def _later(candidate: str, current: str) -> bool:
    return _sort_key(candidate) > _sort_key(current)


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


__all__ = ["Acquaintance", "gather"]
