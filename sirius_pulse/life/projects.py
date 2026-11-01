"""Her projects: the long-running things she is doing for herself.

A project is what keeps her days connected.  Free time used to start from
nothing every time; now there is always "the book I am halfway through" or "the
list I am putting together", with a progress log and a next step she wrote down
herself.

Each project owns one *work* file -- a Markdown document she actually writes in.
That is the artifact: reading notes, a story draft, a collection.  It is not
memory and is never fed back into chat wholesale; the chat side only ever sees a
one-line "what I am doing" and short memory units.
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sirius_pulse.life.clock import to_local
from sirius_pulse.utils.json_io import atomic_write_json, read_json

PROJECT_KINDS: dict[str, str] = {
    "read": "阅读",
    "learn": "学习",
    "write": "写作",
    "collect": "收集",
    "make": "动手做",
    "explore": "探索",
}
DEFAULT_KIND = "explore"

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_DROPPED = "dropped"
PROJECT_STATUSES = frozenset({STATUS_ACTIVE, STATUS_PAUSED, STATUS_DONE, STATUS_DROPPED})

MAX_ACTIVE_PROJECTS = 3

# The progress log only needs to remind her where she is, not replay everything.
_MAX_STEPS = 30
_MAX_TITLE = 60
_MAX_WHY = 200
_MAX_STEP_TEXT = 300

# A work file keeps its newest part in place; older sections move to ``.old.md``
# so the file she reads back at the start of each session stays small.
WORK_MAX_CHARS = 40_000
_WORK_KEEP_CHARS = 30_000
_MAX_SECTION_CHARS = 6_000

_PROJECTS_FILE = "projects.json"
_WORKS_DIR = "works"

PROJECT_ID_RE = re.compile(r"^p-[0-9a-f]{8}$")

_LOCK = threading.RLock()


class ProjectLimitError(RuntimeError):
    """Raised when starting a project would exceed the active-project cap."""


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


@dataclass(slots=True)
class Project:
    project_id: str
    title: str
    kind: str = DEFAULT_KIND
    why: str = ""
    next_step: str = ""
    steps: list[dict[str, str]] = field(default_factory=list)
    status: str = STATUS_ACTIVE
    started_at: str = ""
    touched_at: str = ""
    finished_at: str = ""
    outcome: str = ""

    @property
    def kind_label(self) -> str:
        return PROJECT_KINDS.get(self.kind, PROJECT_KINDS[DEFAULT_KIND])

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "title": self.title,
            "kind": self.kind,
            "why": self.why,
            "next_step": self.next_step,
            "steps": [dict(step) for step in self.steps],
            "status": self.status,
            "started_at": self.started_at,
            "touched_at": self.touched_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Project | None":
        if not isinstance(data, dict):
            return None
        project_id = str(data.get("project_id", "") or "")
        title = _clip(data.get("title"), _MAX_TITLE)
        if not PROJECT_ID_RE.match(project_id) or not title:
            return None
        kind = str(data.get("kind", "") or DEFAULT_KIND)
        status = str(data.get("status", "") or STATUS_ACTIVE)
        steps = data.get("steps")
        return cls(
            project_id=project_id,
            title=title,
            kind=kind if kind in PROJECT_KINDS else DEFAULT_KIND,
            why=_clip(data.get("why"), _MAX_WHY),
            next_step=_clip(data.get("next_step"), _MAX_STEP_TEXT),
            steps=[
                {"at": str(step.get("at", "")), "did": _clip(step.get("did"), _MAX_STEP_TEXT)}
                for step in (steps if isinstance(steps, list) else [])
                if isinstance(step, dict) and step.get("did")
            ][-_MAX_STEPS:],
            status=status if status in PROJECT_STATUSES else STATUS_ACTIVE,
            started_at=str(data.get("started_at", "") or ""),
            touched_at=str(data.get("touched_at", "") or ""),
            finished_at=str(data.get("finished_at", "") or ""),
            outcome=_clip(data.get("outcome"), _MAX_STEP_TEXT),
        )


class ProjectStore:
    """``memory/life/projects.json`` plus one ``works/<id>.md`` per project."""

    def __init__(self, life_dir: Path | str) -> None:
        self.life_dir = Path(life_dir)
        self.path = self.life_dir / _PROJECTS_FILE
        self.works_dir = self.life_dir / _WORKS_DIR

    # -- projects -----------------------------------------------------------

    def load(self) -> list[Project]:
        raw = read_json(self.path, default=None)
        items = raw.get("projects") if isinstance(raw, dict) else None
        projects = [Project.from_dict(item) for item in (items or [])]
        return [project for project in projects if project is not None]

    def _save(self, projects: list[Project]) -> None:
        atomic_write_json(self.path, {"projects": [p.to_dict() for p in projects]})

    def active(self) -> list[Project]:
        return [project for project in self.load() if project.is_active]

    def get(self, project_id: str) -> Project | None:
        return next((p for p in self.load() if p.project_id == project_id), None)

    def start(
        self,
        *,
        title: str,
        now: datetime,
        kind: str = DEFAULT_KIND,
        why: str = "",
        next_step: str = "",
    ) -> Project:
        title = _clip(title, _MAX_TITLE)
        if not title:
            raise ValueError("项目需要一个名字")
        with _LOCK:
            projects = self.load()
            if sum(1 for p in projects if p.is_active) >= MAX_ACTIVE_PROJECTS:
                raise ProjectLimitError(f"手上已经有 {MAX_ACTIVE_PROJECTS} 件事在做了")
            stamp = now.isoformat()
            project = Project(
                project_id=f"p-{uuid.uuid4().hex[:8]}",
                title=title,
                kind=kind if kind in PROJECT_KINDS else DEFAULT_KIND,
                why=_clip(why, _MAX_WHY),
                next_step=_clip(next_step, _MAX_STEP_TEXT),
                started_at=stamp,
                touched_at=stamp,
            )
            projects.append(project)
            self._save(projects)
            return project

    def _mutate(self, project_id: str, mutate: Any) -> Project | None:
        with _LOCK:
            projects = self.load()
            target = next((p for p in projects if p.project_id == project_id), None)
            if target is None:
                return None
            mutate(target)
            self._save(projects)
            return target

    def record_step(
        self, project_id: str, *, did: str, now: datetime, next_step: str = ""
    ) -> Project | None:
        did = _clip(did, _MAX_STEP_TEXT)

        def _apply(project: Project) -> None:
            if did:
                project.steps = [*project.steps, {"at": now.isoformat(), "did": did}][-_MAX_STEPS:]
            if next_step.strip():
                project.next_step = _clip(next_step, _MAX_STEP_TEXT)
            project.touched_at = now.isoformat()

        return self._mutate(project_id, _apply)

    def set_status(
        self, project_id: str, status: str, *, now: datetime, outcome: str = ""
    ) -> Project | None:
        if status not in PROJECT_STATUSES:
            raise ValueError(f"unknown project status: {status}")

        def _apply(project: Project) -> None:
            if status == STATUS_ACTIVE and not project.is_active:
                active = sum(1 for p in self.load() if p.is_active)
                if active >= MAX_ACTIVE_PROJECTS:
                    raise ProjectLimitError(f"手上已经有 {MAX_ACTIVE_PROJECTS} 件事在做了")
            project.status = status
            project.touched_at = now.isoformat()
            if status in {STATUS_DONE, STATUS_DROPPED}:
                project.finished_at = now.isoformat()
            if outcome.strip():
                project.outcome = _clip(outcome, _MAX_STEP_TEXT)

        return self._mutate(project_id, _apply)

    def set_next_step(self, project_id: str, next_step: str, *, now: datetime) -> Project | None:
        def _apply(project: Project) -> None:
            project.next_step = _clip(next_step, _MAX_STEP_TEXT)
            project.touched_at = now.isoformat()

        return self._mutate(project_id, _apply)

    # -- works --------------------------------------------------------------

    def work_path(self, project_id: str) -> Path:
        if not PROJECT_ID_RE.match(project_id):
            raise ValueError(f"invalid project id: {project_id!r}")
        return self.works_dir / f"{project_id}.md"

    def read_work(self, project_id: str) -> str:
        path = self.work_path(project_id)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def work_tail(self, project_id: str, chars: int = 1500) -> str:
        text = self.read_work(project_id)
        if len(text) <= chars:
            return text
        tail = text[-chars:]
        # Start at a line boundary so she does not read back half a sentence.
        newline = tail.find("\n")
        return tail[newline + 1 :] if 0 <= newline < chars // 3 else tail

    def append_work(self, project_id: str, text: str, *, now: datetime, heading: str = "") -> int:
        """Append one dated section to the work file; returns the file's length."""
        body = str(text or "").strip()[:_MAX_SECTION_CHARS]
        if not body:
            raise ValueError("没有要写下的内容")
        project = self.get(project_id)
        if project is None:
            raise KeyError(project_id)
        stamp = to_local(now).strftime("%Y-%m-%d %H:%M")
        title = _clip(heading, _MAX_TITLE)
        section = f"## {stamp}" + (f" · {title}" if title else "") + f"\n\n{body}\n"
        with _LOCK:
            path = self.work_path(project_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            current = self.read_work(project_id)
            if not current:
                current = f"# {project.title}\n\n"
            combined = f"{current.rstrip()}\n\n{section}"
            if len(combined) > WORK_MAX_CHARS:
                combined = self._archive_head(project_id, combined)
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(combined, encoding="utf-8")
            tmp.replace(path)
        self._mutate(project_id, lambda p: setattr(p, "touched_at", now.isoformat()))
        return len(combined)

    def _archive_head(self, project_id: str, text: str) -> str:
        cut = len(text) - _WORK_KEEP_CHARS
        boundary = text.find("\n## ", cut)
        if boundary < 0:
            boundary = cut
        head, keep = text[:boundary], text[boundary:]
        archive = self.works_dir / f"{project_id}.old.md"
        with archive.open("a", encoding="utf-8") as handle:
            handle.write(head.rstrip() + "\n\n")
        title = head.splitlines()[0] if head.startswith("# ") else ""
        return f"{title}\n\n（更早的部分收在旧稿里）\n{keep}".lstrip()

    def works_summary(self) -> list[dict[str, Any]]:
        """One row per project that has a work file, for the WebUI."""
        rows = []
        for project in self.load():
            text = self.read_work(project.project_id)
            if not text:
                continue
            rows.append(
                {
                    "project_id": project.project_id,
                    "title": project.title,
                    "kind": project.kind,
                    "status": project.status,
                    "chars": len(text),
                    "updated_at": project.touched_at,
                }
            )
        return rows
