"""WebUI API endpoints for persona autonomy (intentions and episodes).

Autonomy leaves its traces on disk rather than in a chat transcript, so without
an API the only way to see what a persona did on her own was to read the files
directly.  This exposes those two records:

- ``memory/intentions.json`` — what she is currently carrying, and the outcome
  of the ones she has already dealt with.
- ``memory/autonomy/episodes.json`` — the timeline of what she actually did.

Both are read-only views.  Acting on her behalf from the WebUI would make the
persona configurable rather than autonomous, so no mutating endpoint exists.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.core.intent import IntentFileStore
from sirius_pulse.utils.layout import WorkspaceLayout
from sirius_pulse.webui.server_utils import _json_response, handle_api_errors

LOG = logging.getLogger("sirius.webui")

_MAX_ITEMS = 200


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _autonomy_dir(work_path: Path) -> Path:
    return WorkspaceLayout(work_path).memory_dir() / "autonomy"


def _episodes_path(work_path: Path) -> Path:
    return _autonomy_dir(work_path) / "episodes.json"


def _load_episodes(work_path: Path) -> list[dict[str, Any]]:
    raw = _read_json(_episodes_path(work_path))
    items = raw.get("episodes") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _sort_key(item: dict[str, Any]) -> str:
    return str(item.get("ended_at") or item.get("started_at") or "")


@handle_api_errors
async def api_persona_autonomy_get(request: web.Request, data_dir: Path) -> web.Response:
    """GET /api/persona/autonomy — 她惦记着什么、以及她自主做过什么。"""
    limit = min(max(int(request.query.get("limit", "100")), 1), _MAX_ITEMS)
    work_path = Path(data_dir)

    # 用领域对象而不是重新实现"什么算未了"的规则：意图是否还开着、是否已淡去
    # 由 IntentStore 判定，WebUI 只看结果，避免两处规则各自漂移。
    store = IntentFileStore(work_path)
    intentions = store.load()
    now = datetime.now(timezone.utc).isoformat()
    all_intentions = intentions.all()
    open_intentions = intentions.open_items(now=now)
    episodes = _load_episodes(work_path)

    intentions_sorted = sorted(all_intentions, key=lambda item: item.created_at, reverse=True)
    episodes.sort(key=_sort_key, reverse=True)

    return _json_response(
        {
            "summary": {
                "intentions_total": len(all_intentions),
                "intentions_open": len(open_intentions),
                "episodes_total": len(episodes),
                "last_episode_at": max((_sort_key(item) for item in episodes), default=""),
                "last_intention_at": max((item.created_at for item in all_intentions), default=""),
            },
            "intentions": [item.to_dict() for item in intentions_sorted[:limit]],
            "episodes": episodes[:limit],
            "paths": {
                "intentions": str(store.path),
                "episodes": str(_episodes_path(work_path)),
            },
        }
    )
