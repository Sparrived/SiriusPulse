"""Missing people: when someone she is close to has not shown up for a while.

This is a rule, not a model call.  Two numbers decide it -- how close she is to
someone and how long it has been since they last said anything where she could
hear it -- and the rule only produces a *thought* ("想找某人聊聊"), never the
words.  What she actually says, and where, she decides herself on the turn that
picks an audience for a tell-intention.

Why a rule: deciding "do I miss anyone" on a model turn would cost a call on
every heartbeat and, like the old free time, mostly come back with "no".  The
interesting part (what to say to them) is still hers.

The rhythm is persona-level and kept in a small ledger: one person at most once
per ``gap_days``, and at most ``max_per_day`` people a day.
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


@dataclass(slots=True)
class MissLedger:
    """When she last thought of each person, and how many she thought of today."""

    last_missed: dict[str, str] = field(default_factory=dict)
    day: str = ""
    count: int = 0

    def today_count(self, today: str) -> int:
        return self.count if self.day == today else 0

    def missed_recently(self, user_id: str, now: datetime, gap_days: float) -> bool:
        last = _parse_time(self.last_missed.get(user_id, ""))
        return last is not None and (now - last).total_seconds() < gap_days * 86400

    def record(self, user_id: str, *, today: str, now: datetime) -> None:
        self.last_missed[user_id] = now.isoformat()
        self.count = self.today_count(today) + 1
        self.day = today

    def to_dict(self) -> dict[str, Any]:
        return {"last_missed": dict(self.last_missed), "day": self.day, "count": self.count}

    @classmethod
    def from_dict(cls, data: Any) -> "MissLedger":
        if not isinstance(data, Mapping):
            return cls()
        raw = data.get("last_missed")
        return cls(
            last_missed=(
                {str(k): str(v) for k, v in raw.items()} if isinstance(raw, Mapping) else {}
            ),
            day=str(data.get("day", "") or ""),
            count=_int(data.get("count")),
        )


@dataclass(slots=True)
class Missed:
    """One person she has started to wonder about, and where she could find them."""

    person: Acquaintance
    chats: list[str]
    days: int


def people_to_miss(
    people: Iterable[Acquaintance],
    *,
    now: datetime,
    today: str,
    reachable: Iterable[str],
    ledger: MissLedger,
    miss_after_days: float,
    min_closeness: float,
    gap_days: float,
    max_per_day: int,
) -> list[Missed]:
    """Who she starts missing now, closest first, within today's allowance.

    Someone qualifies only if she is close to them, they have been away for at
    least *miss_after_days*, she has not thought of them in the last *gap_days*,
    and at least one chat they talk in is one she can actually reach.
    """
    allowance = max_per_day - ledger.today_count(today)
    if allowance <= 0 or miss_after_days <= 0:
        return []
    open_chats = set(reachable)
    found: list[Missed] = []
    for person in people:
        if not person.name or person.closeness < min_closeness:
            continue
        days = person.days_away(now)
        if not math.isfinite(days) or days < miss_after_days:
            continue
        if ledger.missed_recently(person.user_id, now, gap_days):
            continue
        chats = [chat for chat in person.chats if chat in open_chats]
        if not chats:
            continue
        found.append(Missed(person=person, chats=chats, days=int(days)))
    found.sort(key=lambda item: (item.person.closeness, item.days), reverse=True)
    return found[:allowance]


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


__all__ = ["Acquaintance", "MissLedger", "Missed", "gather", "people_to_miss"]
