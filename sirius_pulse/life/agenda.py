"""Her plan for the day.

The morning plan is what turns "do you feel like doing something?" -- a question a
model nearly always answers with "no" -- into "it is three o'clock, and this
morning you said you would read the next chapter".  She made the plan herself, so
following it is her own will, not a cron job; she can still skip a block, but
skipping is a decision with a reason, not the default.

Two kinds of block:

``work``
    Tied to one of her projects.  Costs one focused model turn when it comes due.
``routine``
    A nap, a walk, music.  No model call at all -- it only sets what she is doing
    for that stretch, so a chat during it can feel it ("I was out for a walk").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sirius_pulse.life.clock import DaySchedule, format_hhmm, parse_hhmm
from sirius_pulse.utils.json_io import atomic_write_json, read_json

BLOCK_PLANNED = "planned"
BLOCK_DOING = "doing"
BLOCK_DONE = "done"
BLOCK_SKIPPED = "skipped"
BLOCK_MISSED = "missed"
BLOCK_STATUSES = frozenset({BLOCK_PLANNED, BLOCK_DOING, BLOCK_DONE, BLOCK_SKIPPED, BLOCK_MISSED})

_AGENDA_DIR = "agenda"
_KEEP_DAYS = 14
_MAX_ACTIVITY = 80
_MAX_OUTCOME = 300
_MAX_NOTE = 120


@dataclass(slots=True)
class Block:
    start: str
    end: str
    activity: str
    project_id: str = ""
    status: str = BLOCK_PLANNED
    outcome: str = ""
    reason: str = ""

    @property
    def is_work(self) -> bool:
        return bool(self.project_id)

    @property
    def start_minutes(self) -> int:
        return parse_hhmm(self.start, "00:00")

    @property
    def end_minutes(self) -> int:
        return parse_hhmm(self.end, "00:00")

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "activity": self.activity,
            "project_id": self.project_id,
            "kind": "work" if self.is_work else "routine",
            "status": self.status,
            "outcome": self.outcome,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Block | None":
        if not isinstance(data, dict):
            return None
        activity = str(data.get("activity", "") or "").strip()[:_MAX_ACTIVITY]
        try:
            start = format_hhmm(parse_hhmm(data.get("start"), "x"))
            end = format_hhmm(parse_hhmm(data.get("end"), "x"))
        except ValueError:
            return None
        if not activity:
            return None
        status = str(data.get("status", "") or BLOCK_PLANNED)
        return cls(
            start=start,
            end=end,
            activity=activity,
            project_id=str(data.get("project_id", "") or ""),
            status=status if status in BLOCK_STATUSES else BLOCK_PLANNED,
            outcome=str(data.get("outcome", "") or "")[:_MAX_OUTCOME],
            reason=str(data.get("reason", "") or "")[:_MAX_OUTCOME],
        )


@dataclass(slots=True)
class DayAgenda:
    date: str
    blocks: list[Block] = field(default_factory=list)
    note: str = ""
    made_at: str = ""

    def sort(self, schedule: DaySchedule) -> None:
        self.blocks.sort(key=lambda block: schedule.offset_of(block.start_minutes))

    def settle(self, schedule: DaySchedule, now: datetime) -> list[Block]:
        """Close blocks whose time has fully passed: planned ones as missed,
        ones left mid-way as skipped.

        This happens when the process was down, or a heartbeat landed late.  A
        missed block is not run late: the plan was for *then*.
        """
        open_states = {BLOCK_PLANNED, BLOCK_DOING}
        if schedule.life_date(now) != self.date:
            # Asked about a different day: everything still open is in the past.
            passed = [b for b in self.blocks if b.status in open_states]
        else:
            current = schedule.minutes_since_wake(now)
            passed = [
                b
                for b in self.blocks
                if b.status in open_states and schedule.offset_of(b.end_minutes) <= current
            ]
        for block in passed:
            if block.status == BLOCK_DOING:
                # Started but never finished (the turn failed or the process died).
                block.status = BLOCK_SKIPPED
                block.reason = block.reason or "没做成"
            else:
                block.status = BLOCK_MISSED
                block.reason = block.reason or "那会儿没顾上"
        return passed

    def due(self, schedule: DaySchedule, now: datetime) -> Block | None:
        """The planned block that is happening right now, if any."""
        if schedule.life_date(now) != self.date or schedule.is_asleep(now):
            return None
        current = schedule.minutes_since_wake(now)
        for block in self.blocks:
            if block.status != BLOCK_PLANNED:
                continue
            start = schedule.offset_of(block.start_minutes)
            end = schedule.offset_of(block.end_minutes)
            if start <= current < end:
                return block
        return None

    def remaining(self, schedule: DaySchedule, now: datetime) -> list[Block]:
        current = schedule.minutes_since_wake(now)
        return [
            block
            for block in self.blocks
            if block.status == BLOCK_PLANNED and schedule.offset_of(block.start_minutes) > current
        ]

    def work_done(self) -> int:
        return sum(1 for block in self.blocks if block.is_work and block.status == BLOCK_DONE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "note": self.note,
            "made_at": self.made_at,
            "blocks": [block.to_dict() for block in self.blocks],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "DayAgenda | None":
        if not isinstance(data, dict) or not data.get("date"):
            return None
        blocks = [Block.from_dict(item) for item in data.get("blocks") or []]
        return cls(
            date=str(data["date"]),
            blocks=[block for block in blocks if block is not None],
            note=str(data.get("note", "") or "")[:_MAX_NOTE],
            made_at=str(data.get("made_at", "") or ""),
        )


class AgendaStore:
    """``memory/life/agenda/<YYYY-MM-DD>.json``, the last two weeks kept."""

    def __init__(self, life_dir: Path | str) -> None:
        self.dir = Path(life_dir) / _AGENDA_DIR

    def _path(self, life_date: str) -> Path:
        # Dates come from DaySchedule.life_date or a validated API parameter.
        return self.dir / f"{life_date}.json"

    def load(self, life_date: str) -> DayAgenda | None:
        return DayAgenda.from_dict(read_json(self._path(life_date), default=None))

    def save(self, agenda: DayAgenda) -> None:
        atomic_write_json(self._path(agenda.date), agenda.to_dict())
        self._prune()

    def _prune(self) -> None:
        try:
            files = sorted(self.dir.glob("*.json"))
        except OSError:
            return
        for path in files[:-_KEEP_DAYS]:
            try:
                path.unlink()
            except OSError:
                pass
