"""WebUI API endpoints for the biography system — user persona cards and alias management."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from sirius_pulse.webui.server_utils import _get_name, _json_response

LOG = logging.getLogger("sirius.webui")


async def api_persona_biography_list(
    request: web.Request, persona_manager: Any
) -> web.Response:
    """获取人格的所有用户传记卡列表。"""
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    from sirius_pulse.memory.user.unified_manager import UnifiedUserManager

    mgr = UnifiedUserManager(work_path=paths.dir, persona_name=name)
    users = mgr.list_global_users()
    alias_index = mgr._alias_index
    mgr.close()

    return _json_response({
        "cards": [u.to_dict() for u in users],
        "alias_index": {
            alias: [e.to_dict() for e in entries]
            for alias, entries in alias_index.items()
        },
    })


async def api_persona_biography_get(
    request: web.Request, persona_manager: Any
) -> web.Response:
    """获取单个用户的传记卡详情。"""
    name = _get_name(request)
    user_id = str(request.match_info.get("user_id", "")).strip()
    if not user_id:
        return _json_response({"error": "缺少用户ID"}, 400)

    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    from sirius_pulse.memory.user.unified_manager import UnifiedUserManager

    mgr = UnifiedUserManager(work_path=paths.dir, persona_name=name)
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

    from sirius_pulse.memory.user.unified_manager import UnifiedUserManager

    mgr = UnifiedUserManager(work_path=paths.dir, persona_name=name)
    alias_index = mgr._alias_index
    mgr.close()

    return _json_response({
        alias: [e.to_dict() for e in entries]
        for alias, entries in alias_index.items()
    })


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

    from sirius_pulse.memory.user.unified_manager import UnifiedUserManager

    mgr = UnifiedUserManager(work_path=paths.dir, persona_name=name)

    if action == "delete":
        if alias in mgr._alias_index:
            mgr._alias_index[alias] = [
                e for e in mgr._alias_index[alias] if e.user_id != user_id
            ]
            if not mgr._alias_index[alias]:
                del mgr._alias_index[alias]
        mgr.save_to_disk()
        mgr.close()
        return _json_response({"success": True})

    # action == "add" (default)
    if not user_id:
        mgr.close()
        return _json_response({"error": "缺少 user_id 参数"}, 400)

    mgr.register_alias(alias, user_id, user_name, source="manual")
    mgr.save_to_disk()
    mgr.close()
    return _json_response({"success": True})
