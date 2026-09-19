"""WebUI API endpoints for persona management (single-persona architecture)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.core.persona_store import PersonaStore
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.persona_config import (
    AdapterConfig,
    PersonaAdaptersConfig,
    PersonaConfigPaths,
    PersonaExperienceConfig,
)
from sirius_pulse.webui.server_utils import _json_response, handle_api_errors

LOG = logging.getLogger("sirius.webui")


def _request_config_reload(reload_type: str, data_dir: Path) -> None:
    """写入配置重载标志文件，触发 PersonaWorker 热重载。

    快速连续保存时合并重载类型，由 worker 等待短暂静默期后一次性执行。
    """
    try:
        reload_flag = data_dir / "engine_state" / "reload_requested"
        reload_flag.parent.mkdir(parents=True, exist_ok=True)
        types: set[str] = set()
        if reload_flag.exists():
            raw = reload_flag.read_text(encoding="utf-8").strip()
            try:
                existing = json.loads(raw)
                if isinstance(existing, dict):
                    types.update(str(item) for item in existing.get("types", []))
                elif isinstance(existing, list):
                    types.update(str(item) for item in existing)
                elif raw:
                    types.add(raw)
            except Exception:
                if raw:
                    types.add(raw)
        types.add(reload_type)
        payload = (
            next(iter(types))
            if len(types) == 1
            else json.dumps({"types": sorted(types)}, ensure_ascii=False)
        )
        reload_flag.write_text(payload, encoding="utf-8")
        LOG.debug("已写入配置重载标志: %s", sorted(types))
    except Exception as exc:
        LOG.warning("写入配置重载标志失败: %s", exc)


async def api_persona_get_single(request: web.Request, data_dir: Path) -> web.Response:
    paths = PersonaConfigPaths(data_dir)

    profile = PersonaStore.load(paths.dir)
    if profile is None:
        profile = PersonaProfile(name=data_dir.name)

    status = {"running": False, "pid": None}
    status_path = paths.engine_state / "worker_status.json"
    if status_path.exists():
        try:
            st = json.loads(status_path.read_text(encoding="utf-8"))
            status = {
                "running": st.get("running", False),
                "pid": st.get("pid"),
                "started_at": st.get("started_at"),
                "last_heartbeat": st.get("last_heartbeat"),
            }
        except Exception:
            LOG.warning("读取人格状态失败", exc_info=True)
            pass
    return _json_response(
        {
            "name": data_dir.name,
            "persona_name": profile.name,
            "status": status,
        }
    )


async def api_persona_status_get(request: web.Request, data_dir: Path) -> web.Response:
    paths = PersonaConfigPaths(data_dir)

    status = {"running": False, "pid": None}
    status_path = paths.engine_state / "worker_status.json"
    if status_path.exists():
        try:
            st = json.loads(status_path.read_text(encoding="utf-8"))
            status = {
                "running": st.get("running", False),
                "pid": st.get("pid"),
                "started_at": st.get("started_at"),
                "last_heartbeat": st.get("last_heartbeat"),
            }
        except Exception:
            LOG.warning("读取人格状态失败", exc_info=True)
            pass
    return _json_response({"name": data_dir.name, "status": status})


def _read_log_delta(log_file: Any, offset: int, lines: int) -> dict[str, Any]:
    if not log_file.exists():
        return {"lines": [], "offset": 0, "size": 0, "exists": False}
    size = log_file.stat().st_size
    if offset <= 0:
        all_lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        return {
            "lines": all_lines[-lines:] if len(all_lines) > lines else all_lines,
            "offset": size,
            "size": size,
            "exists": True,
        }
    if offset > size:
        offset = 0
    with log_file.open("rb") as f:
        f.seek(offset)
        chunk = f.read()
    text = chunk.decode("utf-8", errors="ignore")
    return {"lines": text.splitlines(), "offset": size, "size": size, "exists": True}


_PERSONA_LOG_SOURCES = {
    "persona": "persona.log",
    "assistant": "assistant.log",
}


def _resolve_persona_log_file(data_dir: Path, source: str = "persona") -> tuple[Path, str]:
    """Resolve a specific persona-scoped log source without guessing by mtime."""
    normalized = source if source in _PERSONA_LOG_SOURCES else "persona"
    return data_dir / "logs" / _PERSONA_LOG_SOURCES[normalized], normalized


async def api_system_logs_get(request: web.Request, data_dir: Path) -> web.Response:
    raw_lines = request.query.get("lines", "300")
    raw_offset = request.query.get("offset", "0")
    try:
        lines = min(2000, max(1, int(raw_lines)))
    except ValueError:
        lines = 300
    try:
        offset = max(0, int(raw_offset))
    except ValueError:
        offset = 0
    log_file = data_dir / "logs" / "webui.log"
    payload = _read_log_delta(log_file, offset, lines)
    payload.update({"target": "webui", "name": "WebUI", "path": str(log_file)})
    return _json_response(payload)


async def api_persona_logs_get(request: web.Request, data_dir: Path) -> web.Response:
    raw_lines = request.query.get("lines", "300")
    raw_offset = request.query.get("offset", "0")
    try:
        lines = min(2000, max(1, int(raw_lines)))
    except ValueError:
        lines = 300
    try:
        offset = max(0, int(raw_offset))
    except ValueError:
        offset = 0
    source_query = request.query.get("source", "persona")
    log_file, source = _resolve_persona_log_file(data_dir, source_query)
    payload = _read_log_delta(log_file, offset, lines)
    payload.update(
        {
            "target": "persona",
            "name": data_dir.name,
            "path": str(log_file),
            "source": source,
        }
    )
    return _json_response(payload)


async def api_persona_get(request: web.Request, data_dir: Path) -> web.Response:
    paths = PersonaConfigPaths(data_dir)

    profile = PersonaStore.load(paths.dir)
    if profile is None:
        profile = PersonaProfile(name=data_dir.name)
    return _json_response(
        {
            "name": profile.name,
            "aliases": profile.aliases,
            "full_system_prompt": profile.full_system_prompt,
        }
    )


async def api_persona_post(request: web.Request, data_dir: Path) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    paths = PersonaConfigPaths(data_dir)

    profile = PersonaStore.load(paths.dir)
    if profile is None:
        profile = PersonaProfile(name=data_dir.name)

    persona_data = body.get("persona", body)
    for key in (
        "name",
        "aliases",
        "full_system_prompt",
    ):
        if key in persona_data:
            setattr(profile, key, persona_data[key])

    PersonaStore.save(paths.dir, profile)
    _request_config_reload("persona", data_dir)
    return _json_response({"success": True})


async def api_experience_get(request: web.Request, data_dir: Path) -> web.Response:
    paths = PersonaConfigPaths(data_dir)

    exp = PersonaExperienceConfig.load(paths.experience)
    return _json_response(
        {
            "engagement_sensitivity": exp.engagement_sensitivity,
            "expressiveness": exp.expressiveness,
            "group_reply_strategies": exp.group_reply_strategies,
            "min_reply_interval_seconds": exp.min_reply_interval_seconds,
            "main_model_reply_cooldown_seconds": exp.main_model_reply_cooldown_seconds,
            "reply_time_curve_points": exp.reply_time_curve_points,
            "max_sentence_chars": exp.max_sentence_chars,
            "diary_top_k": exp.diary_top_k,
            "diary_token_budget": exp.diary_token_budget,
            "memory_unit_top_k": exp.memory_unit_top_k,
            "enable_tools": exp.enable_tools,
            "max_tool_rounds": exp.max_tool_rounds,
            "auto_install_tool_deps": exp.auto_install_tool_deps,
            "plan_mode_enabled": exp.plan_mode_enabled,
            "plan_mode_limit_normal_tools": exp.plan_mode_limit_normal_tools,
            "plan_mode_allow_light_chat": exp.plan_mode_allow_light_chat,
            "plan_mode_chat_awareness_enabled": exp.plan_mode_chat_awareness_enabled,
            "plan_mode_presence_enabled": exp.plan_mode_presence_enabled,
            "plan_mode_presence_min_interval_seconds": (
                exp.plan_mode_presence_min_interval_seconds
            ),
            "other_ai_names": exp.other_ai_names,
            "message_prefixes": exp.message_prefixes,
        }
    )


async def api_experience_post(request: web.Request, data_dir: Path) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    paths = PersonaConfigPaths(data_dir)

    exp = PersonaExperienceConfig.load(paths.experience)
    experience_data = body.get("experience", body)

    for key in (
        "engagement_sensitivity",
        "expressiveness",
        "group_reply_strategies",
        "min_reply_interval_seconds",
        "main_model_reply_cooldown_seconds",
        "reply_time_curve_points",
        "max_sentence_chars",
        "diary_top_k",
        "diary_token_budget",
        "memory_unit_top_k",
        "enable_tools",
        "max_tool_rounds",
        "auto_install_tool_deps",
        "plan_mode_enabled",
        "plan_mode_limit_normal_tools",
        "plan_mode_allow_light_chat",
        "plan_mode_chat_awareness_enabled",
        "plan_mode_presence_enabled",
        "plan_mode_presence_min_interval_seconds",
        "other_ai_names",
        "message_prefixes",
    ):
        if key in experience_data:
            setattr(exp, key, experience_data[key])

    exp = PersonaExperienceConfig.from_dict(exp.to_dict())
    exp.save(paths.experience)
    _request_config_reload("experience", data_dir)
    return _json_response({"success": True})


async def api_adapters_get(request: web.Request, data_dir: Path) -> web.Response:
    paths = PersonaConfigPaths(data_dir)

    adapters = PersonaAdaptersConfig.load(paths.adapters)
    return _json_response({"adapters": [a.to_dict() for a in adapters.adapters]})


async def api_adapters_post(request: web.Request, data_dir: Path) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    paths = PersonaConfigPaths(data_dir)

    adapters = PersonaAdaptersConfig.load(paths.adapters)
    if "adapters" in body and isinstance(body["adapters"], list):
        adapters.adapters = [AdapterConfig(**a) for a in body["adapters"]]

    adapters.save(paths.adapters)
    return _json_response({"success": True})


async def api_engine_reload(request: web.Request, data_dir: Path) -> web.Response:
    # 向 worker 发送重载信号（通过 engine_state/reload_requested）
    _request_config_reload("all", data_dir)
    return _json_response({"success": True, "message": "重载信号已发送"})


async def api_config_post(request: web.Request, data_dir: Path) -> web.Response:
    """更新 adapter 配置（群白名单等），直接写入 adapters.json。"""
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)
    paths = PersonaConfigPaths(data_dir)

    adapters = PersonaAdaptersConfig.load(paths.adapters)
    if not adapters.adapters:
        return _json_response({"error": "无 adapter 可配置"}, 400)

    # 只更新第一个 napcat adapter
    for key in (
        "allowed_group_ids",
        "allowed_private_user_ids",
        "enable_group_chat",
        "enable_private_chat",
        "root",
    ):
        if key in body and adapters.adapters:
            setattr(adapters.adapters[0], key, body[key])

    adapters.save(paths.adapters)
    LOG.info("配置已更新: %s", {k: body.get(k) for k in body})
    return _json_response({"success": True, "message": "配置已保存"})
