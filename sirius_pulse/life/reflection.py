"""Parsing what she decides in the morning and concludes at night.

The old diary subsystem failed because a weak model was asked to distil long text
into facts, and the errors piled up in an ever-growing chain.  The guardrails here
are the opposite on every axis:

- The input is only her *own* first-hand record of the day (her plan, what she did
  in each block, whether people answered).  No chat transcripts, no facts about
  other people -- that stays with the memory system.
- The output is a handful of small typed fields, each clamped and truncated.
- Fields *overwrite* the previous value; nothing is appended forever, so one bad
  night cannot compound.
- Anything that does not validate is dropped as a whole and yesterday's state
  stays.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sirius_pulse.life.agenda import Block
from sirius_pulse.life.clock import DaySchedule, format_hhmm, parse_hhmm
from sirius_pulse.life.projects import PROJECT_KINDS, PROJECT_STATUSES, STATUS_ACTIVE
from sirius_pulse.utils.json_io import atomic_write_json, read_json

_SELF_FILE = "self.json"

MAX_RECENT_CHARS = 300
MAX_INTERESTS = 8
MAX_INTEREST_CHARS = 16
MAX_TOMORROW_CHARS = 100
MAX_MOOD_SHIFT = 0.05

_MAX_BLOCKS = 6
_MIN_BLOCK_MINUTES = 20
_MAX_BLOCK_MINUTES = 180
_MAX_NEW_PROJECTS = 2

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply (fenced or bare)."""
    raw = str(text or "")
    candidates = [match.strip() for match in _FENCE_RE.findall(raw)]
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


# ---------------------------------------------------------------------------
# Self note
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SelfNote:
    """How she would describe herself lately.  Overwritten nightly, never appended."""

    recent: str = ""
    interests: list[str] = field(default_factory=list)
    tomorrow: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "recent": self.recent,
            "interests": list(self.interests),
            "tomorrow": self.tomorrow,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SelfNote":
        if not isinstance(data, dict):
            return cls()
        return cls(
            recent=_clip(data.get("recent"), MAX_RECENT_CHARS),
            interests=_interests(data.get("interests")),
            tomorrow=_clip(data.get("tomorrow"), MAX_TOMORROW_CHARS),
            updated_at=str(data.get("updated_at", "") or ""),
        )


def _interests(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: list[str] = []
    for item in value:
        tag = _clip(item, MAX_INTEREST_CHARS)
        if tag and tag not in seen:
            seen.append(tag)
    return seen[:MAX_INTERESTS]


class SelfNoteStore:
    """``memory/life/self.json``."""

    def __init__(self, life_dir: Path | str) -> None:
        self.path = Path(life_dir) / _SELF_FILE

    def load(self) -> SelfNote:
        return SelfNote.from_dict(read_json(self.path, default=None))

    def save(self, note: SelfNote) -> None:
        atomic_write_json(self.path, note.to_dict())


# ---------------------------------------------------------------------------
# Morning plan
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class NewProject:
    key: str
    title: str
    kind: str
    why: str
    next_step: str


@dataclass(slots=True)
class PlanResult:
    note: str
    blocks: list[Block]
    new_projects: list[NewProject]
    used_intentions: list[str]


def parse_plan(
    text: str,
    *,
    schedule: DaySchedule,
    project_ids: set[str],
    intention_ids: set[str],
    max_work_blocks: int,
    free_project_slots: int,
    not_before: int = 0,
) -> PlanResult | None:
    """Validate a morning plan.  Blocks that do not fit are dropped one by one;
    a reply with no usable block at all is rejected as a whole.

    *not_before* is in minutes since wake-up: when she plans late (the process
    started in the afternoon), blocks that would already be over are dropped.
    """
    data = extract_json_object(text)
    if data is None:
        return None

    new_projects: list[NewProject] = []
    for item in data.get("new_projects") or []:
        if not isinstance(item, dict) or len(new_projects) >= min(
            _MAX_NEW_PROJECTS, max(0, free_project_slots)
        ):
            continue
        title = _clip(item.get("title"), 60)
        key = _clip(item.get("key"), 16)
        if not title or not key or key in project_ids:
            continue
        kind = str(item.get("kind", "") or "")
        new_projects.append(
            NewProject(
                key=key,
                title=title,
                kind=kind if kind in PROJECT_KINDS else "explore",
                why=_clip(item.get("why"), 200),
                next_step=_clip(item.get("next_step"), 300),
            )
        )
    new_keys = {project.key for project in new_projects}

    blocks: list[Block] = []
    taken: list[tuple[int, int]] = []
    work_blocks = 0
    raw_blocks = data.get("blocks") or []
    for item in raw_blocks if isinstance(raw_blocks, list) else []:
        if len(blocks) >= _MAX_BLOCKS or not isinstance(item, dict):
            continue
        try:
            start = parse_hhmm(item.get("start"), "x")
            end = parse_hhmm(item.get("end"), "x")
        except ValueError:
            continue
        activity = _clip(item.get("activity"), 80)
        if not activity or not schedule.is_awake_time(start):
            continue
        begin, finish = schedule.offset_of(start), schedule.offset_of(end)
        length = finish - begin
        if not (_MIN_BLOCK_MINUTES <= length <= _MAX_BLOCK_MINUTES):
            continue
        if finish > schedule.awake_span or begin < not_before:
            continue
        if any(begin < other_end and other_begin < finish for other_begin, other_end in taken):
            continue
        project = _clip(item.get("project"), 16)
        if project and project not in project_ids and project not in new_keys:
            project = ""
        if project:
            if work_blocks >= max_work_blocks:
                continue
            work_blocks += 1
        taken.append((begin, finish))
        blocks.append(
            Block(
                start=format_hhmm(start),
                end=format_hhmm(end),
                activity=activity,
                project_id=project,
            )
        )

    if not blocks:
        return None
    blocks.sort(key=lambda block: schedule.offset_of(block.start_minutes))
    used = [
        str(item)
        for item in (data.get("use_intentions") or [])
        if isinstance(item, str) and item in intention_ids
    ]
    referenced = {block.project_id for block in blocks}
    return PlanResult(
        note=_clip(data.get("note"), 120),
        blocks=blocks,
        # A project that never made it onto the schedule is not started.
        new_projects=[project for project in new_projects if project.key in referenced],
        used_intentions=used,
    )


# ---------------------------------------------------------------------------
# Night reflection
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProjectUpdate:
    project_id: str
    status: str = ""
    next_step: str = ""


@dataclass(slots=True)
class ReflectionResult:
    note: SelfNote
    mood_shift: float
    project_updates: list[ProjectUpdate]


def parse_reflection(text: str, *, project_ids: set[str]) -> ReflectionResult | None:
    data = extract_json_object(text)
    if data is None:
        return None
    recent = _clip(data.get("recent"), MAX_RECENT_CHARS)
    if not recent:
        return None
    try:
        shift = float(data.get("mood_shift", 0.0) or 0.0)
    except (TypeError, ValueError):
        shift = 0.0
    updates: list[ProjectUpdate] = []
    for item in data.get("projects") or []:
        if not isinstance(item, dict):
            continue
        project_id = str(item.get("id", "") or "")
        if project_id not in project_ids:
            continue
        status = str(item.get("status", "") or "")
        updates.append(
            ProjectUpdate(
                project_id=project_id,
                status=status if status in PROJECT_STATUSES and status != STATUS_ACTIVE else "",
                next_step=_clip(item.get("next_step"), 300),
            )
        )
    return ReflectionResult(
        note=SelfNote(
            recent=recent,
            interests=_interests(data.get("interests")),
            tomorrow=_clip(data.get("tomorrow"), MAX_TOMORROW_CHARS),
        ),
        mood_shift=max(-MAX_MOOD_SHIFT, min(MAX_MOOD_SHIFT, shift)),
        project_updates=updates,
    )
