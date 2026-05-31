"""WebUI API endpoints for the biography system — user persona cards and alias management."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from sirius_pulse.webui.server_utils import _get_name, _json_response

LOG = logging.getLogger("sirius.webui")


def _create_managers(paths: Any, persona_name: str) -> tuple:
    """创建 UnifiedUserManager 和 EvolutionChain 实例（共享 DB 连接）。"""
    from sirius_pulse.memory.evolution.chain import EvolutionChain
    from sirius_pulse.memory.user.unified_manager import UnifiedUserManager

    db_path = paths.dir / "persona.db"
    chain = EvolutionChain(db_path=db_path)
    mgr = UnifiedUserManager(
        work_path=paths.dir,
        persona_name=persona_name,
        evolution_chain=chain,
    )
    return mgr, chain


async def api_persona_biography_list(
    request: web.Request, persona_manager: Any
) -> web.Response:
    """获取人格的所有用户传记卡列表（分页）。"""
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    limit = min(int(request.query.get("limit", "50")), 200)
    offset = max(int(request.query.get("offset", "0")), 0)

    mgr, chain = _create_managers(paths, name)
    users = mgr.list_global_users()

    # 从演化链别称缓存构建别称索引
    alias_index_data: dict[str, list[dict]] = {}
    for alias_key, records in chain._alias_cache.items():
        alias_index_data[alias_key] = [r.to_dict() for r in records if r.is_active]

    mgr.close()
    chain.close()

    total = len(users)
    users_sorted = sorted(users, key=lambda u: getattr(u, "last_updated_at", "") or "", reverse=True)
    end = total - offset
    start = max(0, end - limit)
    page = users_sorted[start:end] if end > 0 else []

    return _json_response({
        "cards": [u.to_dict() for u in page],
        "total": total,
        "alias_index": alias_index_data,
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

    mgr, chain = _create_managers(paths, name)
    user = mgr.get_global_user(user_id)
    mgr.close()
    chain.close()

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

    mgr, chain = _create_managers(paths, name)
    alias_index_data: dict[str, list[dict]] = {}
    for alias_key, records in chain._alias_cache.items():
        alias_index_data[alias_key] = [r.to_dict() for r in records if r.is_active]
    mgr.close()
    chain.close()

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

    mgr, chain = _create_managers(paths, name)

    if action == "delete":
        chain.reject_alias(alias, user_id)
        mgr.close()
        chain.close()
        return _json_response({"success": True})

    # action == "add" (default)
    if not user_id:
        mgr.close()
        chain.close()
        return _json_response({"error": "缺少 user_id 参数"}, 400)

    mgr.register_alias(alias, user_id, user_name, source="manual")
    mgr.close()
    chain.close()
    return _json_response({"success": True})
