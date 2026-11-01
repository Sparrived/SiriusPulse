"""What she does on her own: the episode record, and when she may not send.

The decision of *what* to do on her own no longer lives here.  It used to be a
scoring gate over intentions plus an open-ended "free time" offer -- and an
open-ended offer with no context is something a model nearly always declines.
Her days are now driven by her own morning plan (see :mod:`sirius_pulse.life`
and the ``autonomy`` tool); this module keeps the two shared pieces: the
:class:`Episode` timeline entry and the no-sending window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sirius_pulse.life.clock import DaySchedule, to_local

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


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


@dataclass(slots=True)
class Episode:
    """One thing the persona did on her own initiative.

    ``kind`` is a free-form label (a project kind, ``routine``, ``share`` ...),
    not an enum: autonomy is not restricted to producing "works".
    """

    episode_id: str
    started_at: str
    kind: str = "musing"
    seed: str = ""
    outcome: str = ""
    ended_at: str = ""
    intention_id: str = ""
    resolution: str = ""
    audience: str = ""
    refs: list[str] = field(default_factory=list)
    intensity: float = 0.5
    status: str = "done"
    project_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "kind": self.kind,
            "seed": self.seed,
            "outcome": self.outcome,
            "intention_id": self.intention_id,
            "resolution": self.resolution,
            "audience": self.audience,
            "refs": list(self.refs),
            "intensity": round(self.intensity, 4),
            "status": self.status,
            "project_id": self.project_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Episode":
        return cls(
            episode_id=str(data.get("episode_id", "")),
            started_at=str(data.get("started_at", "")),
            kind=str(data.get("kind", "musing")),
            seed=str(data.get("seed", "")),
            outcome=str(data.get("outcome", "")),
            ended_at=str(data.get("ended_at", "")),
            intention_id=str(data.get("intention_id", "")),
            resolution=str(data.get("resolution", "")),
            audience=str(data.get("audience", "")),
            refs=[str(item) for item in data.get("refs", []) or []],
            intensity=_clamp(data.get("intensity", 0.5)),
            status=str(data.get("status", "done")),
            project_id=str(data.get("project_id", "")),
        )
