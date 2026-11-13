"""Her day: when she is awake, and which "day" a moment belongs to.

Everything here is local (China) time, pinned to UTC+8 the same way the rest of
the project does it -- China has no DST, so a fixed offset is exact.

The one idea worth knowing: a *life date* is the date of the morning she last
woke up.  At 01:00 she may still be up (or already asleep) but she is living the
previous calendar day, and both the morning plan and the nightly reflection key
off that.  Computing it as ``(local time - wake time).date()`` makes this hold for
any schedule, including one that goes to bed after midnight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

LOCAL_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

DEFAULT_WAKE = "07:30"
DEFAULT_SLEEP = "23:30"

_MINUTES_PER_DAY = 24 * 60

PHASE_ASLEEP = "asleep"
PHASE_MORNING = "morning"
PHASE_DAY = "day"
PHASE_EVENING = "evening"

PHASE_LABELS = {
    PHASE_ASLEEP: "深夜",
    PHASE_MORNING: "上午",
    PHASE_DAY: "白天",
    PHASE_EVENING: "傍晚到夜里",
}


def parse_hhmm(value: object, default: str) -> int:
    """Parse ``"HH:MM"`` into minutes after midnight, falling back to *default*."""
    for candidate in (value, default):
        if not isinstance(candidate, str):
            continue
        parts = candidate.strip().split(":")
        if len(parts) != 2:
            continue
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
    raise ValueError(f"invalid default time: {default!r}")


def format_hhmm(minutes: int) -> str:
    """Format minutes after midnight as ``"HH:MM"`` (wrapping past midnight)."""
    hour, minute = divmod(int(minutes) % _MINUTES_PER_DAY, 60)
    return f"{hour:02d}:{minute:02d}"


def to_local(now: datetime) -> datetime:
    """Read *now* in local time; a naive timestamp is taken as UTC."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(LOCAL_TIMEZONE)


@dataclass(frozen=True, slots=True)
class DaySchedule:
    """When she wakes up and goes to sleep, in minutes after local midnight."""

    wake_minutes: int = 7 * 60 + 30
    sleep_minutes: int = 23 * 60 + 30

    @classmethod
    def from_strings(cls, wake: object = None, sleep: object = None) -> "DaySchedule":
        wake_minutes = parse_hhmm(wake, DEFAULT_WAKE)
        sleep_minutes = parse_hhmm(sleep, DEFAULT_SLEEP)
        if wake_minutes == sleep_minutes:
            # A zero-length day would mean she never wakes; keep the defaults.
            return cls()
        return cls(wake_minutes=wake_minutes, sleep_minutes=sleep_minutes)

    @property
    def wake(self) -> str:
        return format_hhmm(self.wake_minutes)

    @property
    def sleep(self) -> str:
        return format_hhmm(self.sleep_minutes)

    @property
    def awake_span(self) -> int:
        """How many minutes a day she is awake."""
        return (self.sleep_minutes - self.wake_minutes) % _MINUTES_PER_DAY

    def minutes_since_wake(self, now: datetime) -> int:
        """Minutes since this morning's wake-up (0 .. 1439)."""
        local = to_local(now)
        minute_of_day = local.hour * 60 + local.minute
        return (minute_of_day - self.wake_minutes) % _MINUTES_PER_DAY

    def offset_of(self, hhmm_minutes: int) -> int:
        """Where a clock time falls on her day, as minutes since wake-up."""
        return (hhmm_minutes - self.wake_minutes) % _MINUTES_PER_DAY

    def is_awake_time(self, hhmm_minutes: int) -> bool:
        """Whether a clock time lies inside her waking hours."""
        return self.offset_of(hhmm_minutes) < self.awake_span

    def is_asleep(self, now: datetime) -> bool:
        return self.minutes_since_wake(now) >= self.awake_span

    def awake_fraction(self, now: datetime) -> float:
        """How far through her waking day she is (0 at wake-up, 1 at bedtime)."""
        if self.is_asleep(now):
            return 1.0
        return self.minutes_since_wake(now) / max(1, self.awake_span)

    def phase(self, now: datetime) -> str:
        if self.is_asleep(now):
            return PHASE_ASLEEP
        fraction = self.awake_fraction(now)
        if fraction < 0.25:
            return PHASE_MORNING
        if fraction < 0.65:
            return PHASE_DAY
        return PHASE_EVENING

    def life_date(self, now: datetime) -> str:
        """The date of the morning she last woke up, as ``YYYY-MM-DD``."""
        local = to_local(now) - timedelta(minutes=self.wake_minutes)
        return local.date().isoformat()

    def moment(self, life_date: str, hhmm_minutes: int) -> datetime:
        """The aware datetime at which a clock time happens on a given life date."""
        day = date.fromisoformat(life_date)
        wake = datetime.combine(day, time(0, 0), tzinfo=LOCAL_TIMEZONE) + timedelta(
            minutes=self.wake_minutes
        )
        return wake + timedelta(minutes=self.offset_of(hhmm_minutes))


# Without a configured schedule, nothing may be *sent* between 23:00 and 08:00 local
# time.  She may still work on things; a message that arrives at 03:00 is read in
# the morning at best and resented at worst, so the window gates delivery only.
_QUIET_HOURS_START = 23
_QUIET_HOURS_END = 8


def is_quiet_hours(now: datetime, schedule: DaySchedule | None = None) -> bool:
    """Whether ``now`` falls inside the no-sending window, in local time.

    With a *schedule*, the window is simply when she is asleep.  Without one it is
    the fixed 23:00-08:00 range, which wraps midnight (so it is a union of two
    ranges rather than a single comparison).  A naive timestamp is read as UTC.

    Args:
        now: The moment to test.
        schedule: Her sleep schedule, when known.

    Returns:
        True when sending must be deferred until she is up.
    """
    if schedule is not None:
        return schedule.is_asleep(now)
    local_hour = to_local(now).hour
    return local_hour >= _QUIET_HOURS_START or local_hour < _QUIET_HOURS_END
