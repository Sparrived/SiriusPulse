"""WebUI API endpoints for her life: today's plan, her projects, her works.

Read-only, like the autonomy view: the plan and the projects are hers, so the
WebUI shows them rather than editing them.

- ``GET /api/persona/life`` -- what she is doing now, her energy, her self-note,
  the plan for a day (``?date=YYYY-MM-DD``, default today), the dream of that
  day's night, her projects and the list of works.
- ``GET /api/persona/life/works/{project_id}`` -- one work file as Markdown.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.life import life_dir, schedule_from_config
from sirius_pulse.life.agenda import AgendaStore
from sirius_pulse.life.dreams import DreamStore
from sirius_pulse.life.projects import PROJECT_ID_RE, PROJECT_KINDS, ProjectStore
from sirius_pulse.life.reflection import SelfNoteStore
from sirius_pulse.life.state import LifeStateStore, energy_at, energy_word
from sirius_pulse.utils.json_io import read_json
from sirius_pulse.utils.layout import WorkspaceLayout
from sirius_pulse.webui.server_utils import _json_response, handle_api_errors

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WOKEN_WITHIN_SECONDS = 12 * 60 * 60


def _schedule(work_path: Path):
    config = read_json(WorkspaceLayout(work_path).tool_data_dir() / "autonomy.json", default={})
    return schedule_from_config(config if isinstance(config, dict) else {})


def life_now(work_path: Path, now: datetime | None = None) -> dict[str, Any]:
    """Her state right now; shared with the home-page glance."""
    now = now or datetime.now(timezone.utc)
    life = life_dir(work_path)
    schedule = _schedule(work_path)
    state = LifeStateStore(life).load()
    agenda = AgendaStore(life).load(schedule.life_date(now))
    energy = energy_at(
        schedule,
        now,
        work_done_today=agenda.work_done() if agenda else 0,
        woken_last_night=state.woken_count(now, within_seconds=_WOKEN_WITHIN_SECONDS),
    )
    upcoming = agenda.remaining(schedule, now) if agenda else []
    return {
        "phase": schedule.phase(now),
        "asleep": schedule.is_asleep(now),
        "wake_time": schedule.wake,
        "sleep_time": schedule.sleep,
        "energy": energy,
        "energy_word": energy_word(energy),
        "activity": "" if schedule.is_asleep(now) else state.current_activity(now),
        "next_block": upcoming[0].to_dict() if upcoming else None,
        "life_date": schedule.life_date(now),
    }


def _project_row(project: Any) -> dict[str, Any]:
    row = project.to_dict()
    row["kind_label"] = PROJECT_KINDS.get(project.kind, project.kind)
    row["steps"] = row["steps"][-8:]
    return row


@handle_api_errors
async def api_persona_life_get(request: web.Request, data_dir: Path) -> web.Response:
    """GET /api/persona/life — 她此刻在做什么、某一天的日程、她的项目和作品。"""
    work_path = Path(data_dir)
    life = life_dir(work_path)
    current = life_now(work_path)
    day = str(request.query.get("date", "") or current["life_date"])
    if not _DATE_RE.match(day):
        return _json_response({"error": "date 需要是 YYYY-MM-DD"}, status=400)
    try:
        date.fromisoformat(day)
    except ValueError:
        return _json_response({"error": "date 不是有效日期"}, status=400)

    agenda = AgendaStore(life).load(day)
    dream = DreamStore(life).get(day)
    projects = ProjectStore(life)
    all_projects = projects.load()
    active_first = [p for p in all_projects if p.status == "active"] + sorted(
        (p for p in all_projects if p.status != "active"),
        key=lambda p: p.touched_at,
        reverse=True,
    )
    return _json_response(
        {
            "now": current,
            "self_note": SelfNoteStore(life).load().to_dict(),
            "date": day,
            "agenda": agenda.to_dict() if agenda else None,
            "dream": dream.to_dict() if dream else None,
            "projects": [_project_row(p) for p in active_first],
            "works": projects.works_summary(),
        }
    )


@handle_api_errors
async def api_persona_life_work_get(request: web.Request, data_dir: Path) -> web.Response:
    """GET /api/persona/life/works/{project_id} — 读她的一份作品。"""
    project_id = str(request.match_info.get("project_id", "") or "")
    if not PROJECT_ID_RE.match(project_id):
        return _json_response({"error": "无效的项目 id"}, status=400)
    projects = ProjectStore(life_dir(Path(data_dir)))
    project = projects.get(project_id)
    if project is None:
        return _json_response({"error": "没有这个项目"}, status=404)
    return _json_response(
        {
            "project": _project_row(project),
            "markdown": projects.read_work(project_id),
        }
    )
