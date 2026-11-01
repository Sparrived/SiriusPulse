"""Her running state: what she is doing right now, and how much energy she has.

Energy is a pure rule, never a model call: a daily curve (fresh after waking, a
dip after lunch, worn out by bedtime) minus what the day took out of her -- her
own work, how much she has talked, and being woken up at night.  It is computed
on demand rather than stored, so it can never drift away from the clock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.life.clock import DaySchedule
from sirius_pulse.utils.json_io import atomic_write_json, read_json

_STATE_FILE = "state.json"
_MAX_WOKEN = 10

# How long a night-time wake-up stays on her mind ("I was woken up earlier").
_WOKEN_MEMORY_SECONDS = 2 * 60 * 60

_LOCK = threading.Lock()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


@dataclass(slots=True)
class LifeState:
    """Small, persisted facts about her day that the rules need."""

    activity: str = ""
    activity_until: str = ""
    last_plan_date: str = ""
    last_reflect_date: str = ""
    plan_try_date: str = ""
    plan_tries: int = 0
    plan_tried_at: str = ""
    woken: list[str] = field(default_factory=list)

    def current_activity(self, now: datetime) -> str:
        until = _parse_time(self.activity_until)
        if not self.activity or until is None or until <= now:
            return ""
        return self.activity

    def set_activity(self, text: str, until: datetime) -> None:
        self.activity = str(text or "").strip()[:80]
        self.activity_until = until.isoformat()

    def can_try_plan(
        self, life_date: str, now: datetime, *, max_tries: int, gap_seconds: float
    ) -> bool:
        """Whether another attempt at today's plan is allowed."""
        if self.plan_try_date != life_date:
            return True
        if self.plan_tries >= max_tries:
            return False
        last = _parse_time(self.plan_tried_at)
        return last is None or (now - last).total_seconds() >= gap_seconds

    def record_plan_try(self, life_date: str, now: datetime) -> None:
        if self.plan_try_date != life_date:
            self.plan_try_date = life_date
            self.plan_tries = 0
        self.plan_tries += 1
        self.plan_tried_at = now.isoformat()

    def record_woken(self, now: datetime) -> None:
        self.woken = [*self.woken, now.isoformat()][-_MAX_WOKEN:]

    def woken_count(self, now: datetime, *, within_seconds: float) -> int:
        horizon = now - timedelta(seconds=within_seconds)
        return sum(1 for item in self.woken if (stamp := _parse_time(item)) and stamp >= horizon)

    def woken_recently(self, now: datetime) -> bool:
        return self.woken_count(now, within_seconds=_WOKEN_MEMORY_SECONDS) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "activity": self.activity,
            "activity_until": self.activity_until,
            "last_plan_date": self.last_plan_date,
            "last_reflect_date": self.last_reflect_date,
            "plan_try_date": self.plan_try_date,
            "plan_tries": self.plan_tries,
            "plan_tried_at": self.plan_tried_at,
            "woken": list(self.woken),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "LifeState":
        if not isinstance(data, dict):
            return cls()
        woken = data.get("woken")
        return cls(
            activity=str(data.get("activity", "") or ""),
            activity_until=str(data.get("activity_until", "") or ""),
            last_plan_date=str(data.get("last_plan_date", "") or ""),
            last_reflect_date=str(data.get("last_reflect_date", "") or ""),
            plan_try_date=str(data.get("plan_try_date", "") or ""),
            plan_tries=_int(data.get("plan_tries")),
            plan_tried_at=str(data.get("plan_tried_at", "") or ""),
            woken=[str(item) for item in woken] if isinstance(woken, list) else [],
        )


class LifeStateStore:
    """``memory/life/state.json``."""

    def __init__(self, life_dir: Path | str) -> None:
        self.path = Path(life_dir) / _STATE_FILE

    def load(self) -> LifeState:
        return LifeState.from_dict(read_json(self.path, default=None))

    def save(self, state: LifeState) -> None:
        with _LOCK:
            atomic_write_json(self.path, state.to_dict())

    def update(self, mutate: Any) -> LifeState:
        """Read, mutate and write back under one lock."""
        with _LOCK:
            state = LifeState.from_dict(read_json(self.path, default=None))
            mutate(state)
            atomic_write_json(self.path, state.to_dict())
            return state


def _daily_curve(fraction: float) -> float:
    """Energy over her waking day, before anything has tired her out."""
    if fraction < 0.12:
        return 0.65 + fraction / 0.12 * 0.3
    if fraction < 0.6:
        return 0.95 - (fraction - 0.12) / 0.48 * 0.2
    return 0.75 - (fraction - 0.6) / 0.4 * 0.35


def energy_at(
    schedule: DaySchedule,
    now: datetime,
    *,
    work_done_today: int = 0,
    replies_today: int = 0,
    woken_last_night: int = 0,
) -> float:
    """How much energy she has right now, 0..1."""
    if schedule.is_asleep(now):
        base = 0.2
    else:
        base = _daily_curve(schedule.awake_fraction(now))
    fatigue = (
        max(0, work_done_today) * 0.06
        + min(0.2, max(0, replies_today) * 0.004)
        + min(0.2, max(0, woken_last_night) * 0.1)
    )
    return round(max(0.05, min(1.0, base - fatigue)), 3)


def energy_word(energy: float) -> str:
    if energy >= 0.75:
        return "精神不错"
    if energy >= 0.5:
        return "还行"
    if energy >= 0.3:
        return "有点累"
    return "很困"
