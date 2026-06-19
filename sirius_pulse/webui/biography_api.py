"""WebUI API endpoints for the biography system — user persona cards and alias management."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from sirius_pulse.webui.server_utils import _get_name, _json_response

LOG = logging.getLogger("sirius.webui")


def _create_manager(paths: Any, persona_name: str):
    """创建 UnifiedUserManager 实例。"""
    from sirius_pulse.memory.user.unified_manager import UnifiedUserManager

    return UnifiedUserManager(work_path=paths.dir, persona_name=persona_name)


def _get_storage(paths: Any):
    """获取 MemoryStorage 实例。"""
    from sirius_pulse.memory.storage import MemoryStorage

    db_path = paths.dir / "memory.db"
    return MemoryStorage(db_path)


async def api_persona_biography_list(request: web.Request, persona_manager: Any) -> web.Response:
    """获取人格的所有用户传记卡列表（分页）。"""
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    limit = min(int(request.query.get("limit", "50")), 200)
    offset = max(int(request.query.get("offset", "0")), 0)

    mgr = _create_manager(paths, name)
    storage = _get_storage(paths)
    users = mgr.list_global_users()
    alias_index_data = storage.get_all_aliases()
    mgr.close()
    storage.close()

    total = len(users)
    users_sorted = sorted(
        users, key=lambda u: getattr(u, "last_updated_at", "") or "", reverse=True
    )
    end = total - offset
    start = max(0, end - limit)
    page = users_sorted[start:end] if end > 0 else []

    return _json_response(
        {
            "cards": [u.to_dict() for u in page],
            "total": total,
            "alias_index": alias_index_data,
        }
    )


async def api_persona_biography_get(request: web.Request, persona_manager: Any) -> web.Response:
    """获取单个用户的传记卡详情。"""
    name = _get_name(request)
    user_id = str(request.match_info.get("user_id", "")).strip()
    if not user_id:
        return _json_response({"error": "缺少用户ID"}, 400)

    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    mgr = _create_manager(paths, name)
    user = mgr.get_global_user(user_id)
    mgr.close()

    if user is None:
        return _json_response({"error": "用户传记不存在"}, 404)

    return _json_response(user.to_dict())


async def api_persona_biography_alias_index(
    request: web.Request, persona_manager: Any
) -> web.Response:
    """获取别名索引。"""
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    storage = _get_storage(paths)
    alias_index_data = storage.get_all_aliases()
    storage.close()

    return _json_response(alias_index_data)


async def api_persona_biography_alias_index_update(
    request: web.Request, persona_manager: Any
) -> web.Response:
    """更新别名索引（新增或删除别名映射）。"""
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "无效的 JSON 请求体"}, 400)

    action = body.get("action", "add")
    alias = str(body.get("alias", "")).strip().lower()
    user_id = str(body.get("user_id", "")).strip()
    user_name = str(body.get("user_name", "")).strip()

    if not alias:
        return _json_response({"error": "缺少 alias 参数"}, 400)

    storage = _get_storage(paths)

    if action == "delete":
        storage.delete_alias_entry(alias, user_id)
        storage.close()
        return _json_response({"success": True})

    if action == "shadow":
        storage.shadow_alias_entry(alias, user_id)
        storage.close()
        return _json_response({"success": True})

    # action == "add" (default)
    if not user_id:
        storage.close()
        return _json_response({"error": "缺少 user_id 参数"}, 400)

    from sirius_pulse.memory.alias_policy import validate_person_alias

    valid_alias, alias, reason = validate_person_alias(alias)
    if not valid_alias:
        storage.close()
        return _json_response({"error": reason}, 400)

    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    storage.save_alias_entry(
        {
            "alias": alias,
            "user_id": user_id,
            "user_name": user_name,
            "source": "manual",
            "confidence": 0.95,
            "first_seen_at": now_iso,
            "last_seen_at": now_iso,
        }
    )
    storage.close()
    return _json_response({"success": True})
