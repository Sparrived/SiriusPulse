"""WebUI endpoints for work-mode trajectories and settings.

工作模式是模型自己进出的任务态，整个过程不留聊天记录，所以不暴露这条视图的话，
外界只能去翻 ``memory/work_mode/sessions.json``。这里把每次工作模式的目标、每一轮
的正文与工具结果、以及退出时的工作结果摆出来。

轨迹只读——工作模式由模型自己决定进出，从 WebUI 替她进入/退出就不是"她自己做"了。
但"工作期间用哪个模型"是本框架的配置：任务名就是 AMKR 的模型入口，所以这里提供一个
读写都有的设置项，写到 ``memory/work_mode/settings.json``，每次开始工作时重新读取。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.core.work_mode import WorkModeStore
from sirius_pulse.webui.model_catalog import build_model_catalog
from sirius_pulse.webui.server_utils import _json_response, handle_api_errors

_MAX_ITEMS = 100
_TASK_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{0,64}$")


def _sort_key(item: dict[str, Any]) -> str:
    return str(item.get("ended_at") or item.get("started_at") or "")


@handle_api_errors
async def api_persona_work_mode_get(request: web.Request, data_dir: Path) -> web.Response:
    """GET /api/persona/work-mode — 她进过哪些工作模式、做成了什么、用的哪个模型。"""
    limit = min(max(int(request.query.get("limit", "50")), 1), _MAX_ITEMS)
    store = WorkModeStore(Path(data_dir))
    sessions = store.load()
    sessions.sort(key=_sort_key, reverse=True)

    completed = [item for item in sessions if item.get("status") == "completed"]
    running = [item for item in sessions if item.get("status") == "running"]
    steps_total = sum(len(item.get("steps") or []) for item in sessions)

    return _json_response(
        {
            "summary": {
                "sessions_total": len(sessions),
                "sessions_completed": len(completed),
                "sessions_running": len(running),
                "steps_total": steps_total,
                "last_session_at": max((_sort_key(item) for item in sessions), default=""),
                "last_goal": (sessions[0].get("goal", "") if sessions else ""),
            },
            "sessions": sessions[:limit],
            "settings": {"task_name": store.work_task_name()},
            "task_options": list(build_model_catalog()["model_choices"]),
            "paths": {"sessions": str(store.path), "settings": str(store.settings_path)},
        }
    )


@handle_api_errors
async def api_persona_work_mode_post(request: web.Request, data_dir: Path) -> web.Response:
    """POST /api/persona/work-mode — 设置工作模式使用的任务名，也就是 AMKR 里的模型。

    空字符串表示沿用本回合原本的任务名（普通聊天即 ``response_generate``），
    所以没配置过时行为与从前一致。
    """
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)
    if not isinstance(body, dict):
        return _json_response({"error": "Invalid JSON"}, 400)

    task_name = str(body.get("task_name", "") or "").strip()
    if not _TASK_NAME_RE.fullmatch(task_name):
        return _json_response({"error": "task_name 只能是字母、数字、下划线、点或横线"}, 400)

    store = WorkModeStore(Path(data_dir))
    store.save_settings(task_name=task_name)
    return _json_response({"success": True, "task_name": store.work_task_name()})
