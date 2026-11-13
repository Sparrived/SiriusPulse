"""What she did on her own: the :class:`Episode` timeline entry.

Her days are driven by her own morning plan (see :mod:`sirius_pulse.life` and the
``autonomy`` tool); every block she works through, every share she sends, leaves
one episode behind.  The no-sending window that goes with it lives in
:func:`sirius_pulse.life.clock.is_quiet_hours`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
