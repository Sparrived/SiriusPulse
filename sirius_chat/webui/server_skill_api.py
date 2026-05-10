"""WebUI Skill 管理 API — 每人格独立的 Skill 配置与启停。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_chat.skills.registry import SkillRegistry

LOG = logging.getLogger("sirius.webui")


def _json_response(data: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, indent=2))


def _get_name(request: web.Request) -> str:
    return str(request.match_info.get("name", "")).strip()


def _skill_config_path(persona_dir: Path, skill_name: str) -> Path:
    return persona_dir / "skill_data" / f"{skill_name}.config.json"


def _persona_skill_config_path(persona_dir: Path) -> Path:
    return persona_dir / "skill_data" / ".persona_skills.json"


def _load_skill_registry(persona_dir: Path) -> SkillRegistry:
    """从人格目录加载所有 skill（内置 + 人格级）。"""
    registry = SkillRegistry()
    registry.load_from_directory(
        persona_dir / "skills",
        auto_install_deps=False,
        include_builtin=True,
    )
    return registry


def _load_persona_skill_config(persona_dir: Path) -> dict[str, Any]:
    """加载人格级 skill 配置（启停状态、默认参数等）。"""
    path = _persona_skill_config_path(persona_dir)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_persona_skill_config(persona_dir: Path, config: dict[str, Any]) -> None:
    """保存人格级 skill 配置。"""
    path = _persona_skill_config_path(persona_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


async def api_persona_skills_get(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/skills — 列出所有人格级 skill。"""
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    try:
        registry = _load_skill_registry(paths.dir)
        persona_config = _load_persona_skill_config(paths.dir)

        skills: list[dict[str, Any]] = []
        for skill in registry.all_skills():
            skill_cfg = persona_config.get(skill.name, {})
            skills.append({
                "name": skill.name,
                "description": skill.description,
                "version": skill.version,
                "enabled": skill_cfg.get("enabled", True),
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
                "config": skill_cfg.get("config", {}),
            })

        return _json_response({"skills": skills})
    except Exception as exc:
        LOG.warning("读取 Skill 列表失败 %s: %s", name, exc)
        return _json_response({"error": str(exc)}, 500)


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

    try:
        persona_config = _load_persona_skill_config(paths.dir)
        if skill_name not in persona_config:
            persona_config[skill_name] = {}
        persona_config[skill_name]["enabled"] = enabled
        _save_persona_skill_config(paths.dir, persona_config)

        LOG.info("Skill %s/%s enabled=%s", name, skill_name, enabled)
        return _json_response({"success": True, "skill": skill_name, "enabled": enabled})
    except Exception as exc:
        LOG.warning("切换 Skill 状态失败 %s/%s: %s", name, skill_name, exc)
        return _json_response({"error": str(exc)}, 500)


async def api_persona_skill_config_get(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/skills/{skill_name}/config — 获取 skill 配置。"""
    name = _get_name(request)
    skill_name = str(request.match_info.get("skill_name", "")).strip()
    if not skill_name:
        return _json_response({"error": "缺少 skill_name"}, 400)

    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    try:
        persona_config = _load_persona_skill_config(paths.dir)
        skill_cfg = persona_config.get(skill_name, {})

        # 同时返回 skill 的元数据（参数定义等）
        registry = _load_skill_registry(paths.dir)
        skill = registry.get(skill_name)
        meta: dict[str, Any] = {}
        if skill is not None:
            meta = {
                "name": skill.name,
                "description": skill.description,
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
            }

        return _json_response({
            "skill": skill_name,
            "config": skill_cfg.get("config", {}),
            "enabled": skill_cfg.get("enabled", True),
            "meta": meta,
        })
    except Exception as exc:
        LOG.warning("读取 Skill 配置失败 %s/%s: %s", name, skill_name, exc)
        return _json_response({"error": str(exc)}, 500)


async def api_persona_skill_history_get(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/skill-history — 返回 SKILL 执行历史详情。"""
    from sirius_chat.skills.telemetry import SkillTelemetry

    name = _get_name(request)
    skill_name = request.query.get("skill_name", "").strip() or None
    limit = min(int(request.query.get("limit", "50")), 200)

    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    telemetry_path = paths.dir / "skill_data" / ".telemetry.jsonl"
    if not telemetry_path.exists():
        return _json_response({"history": []})

    try:
        telemetry = SkillTelemetry(telemetry_path)
        records = telemetry.query(skill_name=skill_name, limit=limit)
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

        return _json_response({"history": items})
    except Exception as exc:
        LOG.warning("读取 Skill 执行历史失败 %s: %s", name, exc)
        return _json_response({"error": str(exc)}, 500)


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

    try:
        persona_config = _load_persona_skill_config(paths.dir)
        if skill_name not in persona_config:
            persona_config[skill_name] = {}
        persona_config[skill_name]["config"] = body.get("config", {})
        if "enabled" in body:
            persona_config[skill_name]["enabled"] = bool(body["enabled"])
        _save_persona_skill_config(paths.dir, persona_config)

        LOG.info("Skill 配置已保存 %s/%s", name, skill_name)
        return _json_response({"success": True, "skill": skill_name})
    except Exception as exc:
        LOG.warning("保存 Skill 配置失败 %s/%s: %s", name, skill_name, exc)
        return _json_response({"error": str(exc)}, 500)
