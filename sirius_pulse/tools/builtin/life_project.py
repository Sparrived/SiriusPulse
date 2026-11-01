"""Built-in TOOL for her own projects: start one, write in it, move it forward.

A project is a long-running thing she does for herself -- a book with reading
notes, a story draft, a collection.  Each one owns a work file she actually
writes in, which is what makes her time on her own leave something behind.

Usable in two places:

- in a block of her own day (the autonomy tick runs those), where ``write`` and
  ``step`` are how the block's result is kept;
- in an ordinary chat, where she can start a project because someone recommended
  a book, or jot down a step she just made.

It never sends anything, so it is allowed during self-initiated turns.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sirius_pulse.life import life_dir
from sirius_pulse.life.projects import (
    MAX_ACTIVE_PROJECTS,
    PROJECT_KINDS,
    STATUS_DONE,
    STATUS_DROPPED,
    STATUS_PAUSED,
    ProjectLimitError,
    ProjectStore,
)

_ACTIONS = ("write", "step", "start", "finish", "drop", "pause", "list")

TOOL_META = {
    "name": "life_project",
    "description": (
        "【适用场景】管理你自己的长期项目（读一本书、学一样东西、写一篇东西、收集整理、动手做点什么），"
        "并把真实的成果写进项目的作品里。\n"
        "【使用方法】action=write：把这次做出的具体内容写进作品（content，可选 heading）；"
        "action=step：记下这一步做了什么（did）和下一步（next_step）；"
        "action=start：开新项目（title、kind=read/learn/write/collect/make/explore、why、next_step）；"
        "action=finish / drop / pause：做完 / 放下 / 暂停（可选 outcome 说明）；action=list：查看手上的项目。\n"
        f"【注意事项】同时进行的项目最多 {MAX_ACTIVE_PROJECTS} 个；作品里写具体内容，不写流水账。"
        "project_id 形如 p-xxxxxxxx，用 list 查询。"
    ),
    "version": "1.0.0",
    "model_visible": True,
    "allowed_when_self_initiated": True,
    "side_effect": "external_write",
    "tags": ["autonomy", "life", "persona"],
    "parameters": {
        "action": {
            "type": "str",
            "description": "write / step / start / finish / drop / pause / list",
            "required": True,
            "default": "list",
        },
        "project_id": {
            "type": "str",
            "description": "要操作的项目 id（start、list 不需要）。",
            "required": False,
            "default": "",
        },
        "content": {
            "type": "str",
            "description": "write：写进作品的具体内容（Markdown）。",
            "required": False,
            "default": "",
        },
        "heading": {
            "type": "str",
            "description": "write：这一段的小标题，可选。",
            "required": False,
            "default": "",
        },
        "did": {
            "type": "str",
            "description": "step：这一步做了什么，一两句话。",
            "required": False,
            "default": "",
        },
        "next_step": {
            "type": "str",
            "description": "step / start：下一步打算做什么。",
            "required": False,
            "default": "",
        },
        "title": {
            "type": "str",
            "description": "start：项目名，要具体（如「读《雪国》并写读书笔记」）。",
            "required": False,
            "default": "",
        },
        "kind": {
            "type": "str",
            "description": "start：read / learn / write / collect / make / explore。",
            "required": False,
            "default": "explore",
        },
        "why": {
            "type": "str",
            "description": "start：为什么想做这件事。",
            "required": False,
            "default": "",
        },
        "outcome": {
            "type": "str",
            "description": "finish / drop / pause：一句话说明。",
            "required": False,
            "default": "",
        },
    },
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fail(error: str, summary: str = "") -> dict[str, Any]:
    return {"success": False, "error": error, "summary": summary or error}


def run(
    action: str = "list",
    project_id: str = "",
    content: str = "",
    heading: str = "",
    did: str = "",
    next_step: str = "",
    title: str = "",
    kind: str = "explore",
    why: str = "",
    outcome: str = "",
    engine_context: Any = None,
    **_: Any,
) -> dict[str, Any]:
    """Act on one of her projects."""
    if engine_context is None:
        return _fail("缺少 engine_context，无法访问项目")
    action = str(action or "").strip().lower()
    if action not in _ACTIONS:
        return _fail(f"未知 action：{action}，可用：{' / '.join(_ACTIONS)}")
    store = ProjectStore(life_dir(engine_context.get_work_path()))
    now = _now()

    if action == "list":
        projects = store.load()
        rows = [
            {
                "project_id": p.project_id,
                "title": p.title,
                "kind": PROJECT_KINDS.get(p.kind, p.kind),
                "status": p.status,
                "next_step": p.next_step,
            }
            for p in projects
            if p.status in {"active", STATUS_PAUSED}
        ]
        summary = "手上还没有项目。" if not rows else f"手上有 {len(rows)} 个项目。"
        return {"success": True, "projects": rows, "summary": summary}

    if action == "start":
        try:
            project = store.start(
                title=title,
                kind=str(kind or "").strip().lower(),
                why=why,
                next_step=next_step,
                now=now,
            )
        except ProjectLimitError as exc:
            return _fail(str(exc), "手上的事已经够多了，先做完或放下一件再开新的")
        except ValueError as exc:
            return _fail(str(exc))
        return {
            "success": True,
            "project_id": project.project_id,
            "summary": f"开始了新项目「{project.title}」。之后排日程时会给它留时间。",
        }

    project = store.get(str(project_id or "").strip())
    if project is None:
        return _fail(f"找不到项目：{project_id}", "没找到这个项目，可以先用 list 看看")

    if action == "write":
        try:
            length = store.append_work(project.project_id, content, heading=heading, now=now)
        except ValueError as exc:
            return _fail(str(exc))
        return {
            "success": True,
            "chars": length,
            "summary": f"写进了「{project.title}」的作品。",
        }

    if action == "step":
        if not str(did or "").strip() and not str(next_step or "").strip():
            return _fail("did 和 next_step 至少写一个")
        store.record_step(project.project_id, did=did, next_step=next_step, now=now)
        return {"success": True, "summary": f"记下了「{project.title}」这一步。"}

    status = {"finish": STATUS_DONE, "drop": STATUS_DROPPED, "pause": STATUS_PAUSED}[action]
    try:
        store.set_status(project.project_id, status, outcome=outcome, now=now)
    except ProjectLimitError as exc:
        return _fail(str(exc))
    verb = {"finish": "做完了", "drop": "放下了", "pause": "先暂停了"}[action]
    return {"success": True, "status": status, "summary": f"「{project.title}」{verb}。"}
