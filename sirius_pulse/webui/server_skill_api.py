"""WebUI Skill 管理 API — 每人格独立的 Skill 配置与启停。

所有 skill 的配置和启停状态统一存储在各自的 data_store 文件中：
  {persona_dir}/skill_data/{skill_name}.json
其中 _enabled 字段表示启停状态，其余为 skill 配置参数。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.skills.registry import SkillRegistry
from sirius_pulse.webui.server_utils import _get_name, _json_response, handle_api_errors

LOG = logging.getLogger("sirius.webui")

# ── 模块级缓存，避免每次 API 请求都重新扫描磁盘和执行 importlib ──
_skill_registry_cache: dict[str, tuple[float, SkillRegistry]] = {}
_CACHE_TTL = 60.0  # 秒


def _invalidate_skill_cache(persona_dir: Path) -> None:
    """清除指定人格的 skill 缓存。"""
    key = str(persona_dir)
    _skill_registry_cache.pop(key, None)


def _load_skill_registry(persona_dir: Path) -> SkillRegistry:
    """从人格目录加载所有 skill（内置 + 人格级），带模块级缓存。"""
    key = str(persona_dir)
    now = time.monotonic()
    cached = _skill_registry_cache.get(key)
    if cached is not None:
        ts, registry = cached
        if now - ts < _CACHE_TTL:
            return registry
    registry = SkillRegistry()
    registry.load_from_directory(
        persona_dir / "skills",
        auto_install_deps=False,
        include_builtin=True,
    )
    _skill_registry_cache[key] = (now, registry)
    return registry


def _load_skill_data_store(persona_dir: Path, skill_name: str) -> dict[str, Any]:
    """从 data_store 文件读取 skill 的完整数据（配置 + 运行时状态）。"""
    store_path = persona_dir / "skill_data" / f"{skill_name}.json"
    if not store_path.exists():
        return {}
    try:
        raw = json.loads(store_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        LOG.warning("读取 skill data_store 失败: %s", store_path, exc_info=True)
        return {}


def _save_skill_data_store(persona_dir: Path, skill_name: str, data: dict[str, Any]) -> None:
    """原子写入 skill 的 data_store 文件。"""
    from sirius_pulse.config.file_io import atomic_json_save

    store_path = persona_dir / "skill_data" / f"{skill_name}.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_save(store_path, data)


def _extract_config(data: dict[str, Any], skill: Any) -> dict[str, Any]:
    """从 data_store 数据中提取配置字段（过滤掉 _ 前缀的元数据和运行时字段）。"""
    config_keys: set[str] = {p.name for p in skill.parameters}
    return {k: v for k, v in data.items() if k in config_keys}


@handle_api_errors
async def api_persona_skills_get(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/skills — 列出所有人格级 skill。"""
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    registry = _load_skill_registry(paths.dir)

    skills: list[dict[str, Any]] = []
    for skill in registry.all_skills():
        data = _load_skill_data_store(paths.dir, skill.name)
        skills.append(
            {
                "name": skill.name,
                "description": skill.description,
                "version": skill.version,
                "enabled": data.get("_enabled", True),
                "developer_only": skill.developer_only,
                "silent": skill.silent,
                "tags": skill.tags,
                "adapter_types": skill.adapter_types,
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "description": p.description,
                        "required": p.required,
                        "default": p.default,
                    }
                    for p in skill.parameters
                ],
                "config": _extract_config(data, skill),
            }
        )

    return _json_response({"skills": skills})


@handle_api_errors
async def api_persona_skill_toggle(request: web.Request, persona_manager: Any) -> web.Response:
    """POST /api/personas/{name}/skills/{skill_name}/toggle — 启停 skill。"""
    name = _get_name(request)
    skill_name = str(request.match_info.get("skill_name", "")).strip()
    if not skill_name:
        return _json_response({"error": "缺少 skill_name"}, 400)

    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    try:
        body = await request.json()
    except Exception:
        body = {}

    enabled = bool(body.get("enabled", True))

    data = _load_skill_data_store(paths.dir, skill_name)
    data["_enabled"] = enabled
    _save_skill_data_store(paths.dir, skill_name, data)

    LOG.info("Skill %s/%s enabled=%s", name, skill_name, enabled)
    return _json_response({"success": True, "skill": skill_name, "enabled": enabled})


@handle_api_errors
async def api_persona_skill_config_get(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/skills/{skill_name}/config — 获取 skill 配置。"""
    name = _get_name(request)
    skill_name = str(request.match_info.get("skill_name", "")).strip()
    if not skill_name:
        return _json_response({"error": "缺少 skill_name"}, 400)

    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    registry = _load_skill_registry(paths.dir)
    skill = registry.get(skill_name)

    data = _load_skill_data_store(paths.dir, skill_name)
    config = _extract_config(data, skill) if skill else {}

    meta: dict[str, Any] = {}
    if skill is not None:
        meta = {
            "name": skill.name,
            "description": skill.description,
            "parameters": skill.get_parameter_schema(),
        }

    return _json_response(
        {
            "skill": skill_name,
            "config": config,
            "enabled": data.get("_enabled", True),
            "meta": meta,
        }
    )


@handle_api_errors
async def api_persona_skill_history_get(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/skill-history — 返回 SKILL 执行历史详情（分页，支持筛选）。"""
    from sirius_pulse.skills.telemetry import SkillTelemetry

    name = _get_name(request)
    skill_name = request.query.get("skill_name", "").strip() or None
    success_str = request.query.get("success", "").strip().lower()
    caller = request.query.get("caller", "").strip()
    limit = min(int(request.query.get("limit", "50")), 200)
    offset = max(int(request.query.get("offset", "0")), 0)

    success_filter: bool | None = None
    if success_str == "true":
        success_filter = True
    elif success_str == "false":
        success_filter = False

    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    telemetry_path = paths.dir / "skill_data" / ".telemetry.jsonl"
    if not telemetry_path.exists():
        return _json_response({"history": [], "total": 0, "stats": {}})

    telemetry = SkillTelemetry(telemetry_path)
    records, total = telemetry.query(
        skill_name=skill_name, success=success_filter, limit=limit, offset=offset
    )

    # caller 筛选（在 query 之后过滤）
    if caller:
        records = [r for r in records if caller in (r.caller_user_id or "")]
        total = len(records)
    items: list[dict[str, Any]] = []
    for rec in reversed(records):
        item: dict[str, Any] = {
            "skill_name": rec.skill_name,
            "timestamp": rec.timestamp,
            "success": rec.success,
            "duration_ms": rec.duration_ms,
            "caller_user_id": rec.caller_user_id,
        }
        if rec.params:
            item["params"] = rec.params
        if rec.result_summary:
            item["result_summary"] = rec.result_summary
        if rec.error:
            item["error"] = rec.error
        items.append(item)

    # 计算统计摘要（基于全量数据，避免前端拉取全部明细）
    stats = telemetry.summary()

    return _json_response({"history": items, "total": total, "stats": stats})


@handle_api_errors
async def api_persona_skill_config_post(request: web.Request, persona_manager: Any) -> web.Response:
    """POST /api/personas/{name}/skills/{skill_name}/config — 保存 skill 配置。"""
    name = _get_name(request)
    skill_name = str(request.match_info.get("skill_name", "")).strip()
    if not skill_name:
        return _json_response({"error": "缺少 skill_name"}, 400)

    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    # 读取现有 data_store（保留运行时字段如 _last_poll_at）
    data = _load_skill_data_store(paths.dir, skill_name)

    # 合并配置：新配置覆盖同名键，运行时字段保留
    skill_cfg = body.get("config", {})
    if isinstance(skill_cfg, dict):
        data.update(skill_cfg)

    if "enabled" in body:
        data["_enabled"] = bool(body["enabled"])

    _save_skill_data_store(paths.dir, skill_name, data)

    LOG.info("Skill 配置已保存 %s/%s", name, skill_name)
    return _json_response({"success": True, "skill": skill_name})
