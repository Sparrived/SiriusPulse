"""WebUI API endpoint for work-mode trajectories.

工作模式是模型自己进出的任务态，整个过程不留聊天记录，所以不暴露这条只读视图
的话，外界只能去翻 ``memory/work_mode/sessions.json``。这里把每次工作模式的目标、
每一轮的正文与工具结果、以及退出时的工作结果摆出来。

只读：工作模式由模型自己决定进出，从 WebUI 替她进入/退出就不是"她自己做"了。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.core.work_mode import WorkModeStore
from sirius_pulse.webui.server_utils import _json_response, handle_api_errors

_MAX_ITEMS = 100


def _sort_key(item: dict[str, Any]) -> str:
    return str(item.get("ended_at") or item.get("started_at") or "")


@handle_api_errors
async def api_persona_work_mode_get(request: web.Request, data_dir: Path) -> web.Response:
    """GET /api/persona/work-mode — 她进过哪些工作模式、做成了什么。"""
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
            "paths": {"sessions": str(store.path)},
        }
    )
