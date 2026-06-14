"""WebUI API endpoints for persona management."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

from aiohttp import web

from sirius_pulse.core.orchestration_store import OrchestrationStore
from sirius_pulse.core.persona_store import PersonaStore
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.persona_config import (
    AdapterConfig,
    PersonaAdaptersConfig,
    PersonaExperienceConfig,
)
from sirius_pulse.platforms.persona_utils import generate_persona_from_interview
from sirius_pulse.providers.routing import WorkspaceProviderManager
from sirius_pulse.webui.model_catalog import build_model_catalog
from sirius_pulse.webui.server_utils import _get_name, _json_response, handle_api_errors

LOG = logging.getLogger("sirius.webui")


def _request_config_reload(persona_name: str, reload_type: str, persona_manager: Any) -> None:
    """写入配置重载标志文件，触发 PersonaWorker 热重载。

    Args:
        persona_name: 人格名称
        reload_type: 重载类型 (persona / orchestration / experience / provider / all)
        persona_manager: PersonaManager 实例
    """
    paths = persona_manager.get_persona_paths(persona_name)
    if paths is None:
        return
    try:
        reload_flag = paths.engine_state / "reload_requested"
        reload_flag.parent.mkdir(parents=True, exist_ok=True)
        reload_flag.write_text(reload_type, encoding="utf-8")
        LOG.debug("已写入配置重载标志: %s -> %s", persona_name, reload_type)
    except Exception as exc:
        LOG.warning("写入配置重载标志失败: %s", exc)


async def api_personas_get(request: web.Request, persona_manager: Any) -> web.Response:
    personas = persona_manager.list_personas()
    result = []
    for p in personas:
        paths = persona_manager.get_persona_paths(p["name"])
        status = {"running": False, "pid": None}
        if paths is not None:
            status_path = paths.engine_state / "worker_status.json"
            if status_path.exists():
                try:
                    st = json.loads(status_path.read_text(encoding="utf-8"))
                    status = {
                        "running": st.get("running", False),
                        "pid": st.get("pid"),
                        "started_at": st.get("started_at"),
                    }
                except Exception:
                    LOG.warning("读取人格状态失败", exc_info=True)
                    pass
        result.append({**p, "status": status})
    return _json_response({"personas": result})


@handle_api_errors
async def api_personas_post(request: web.Request, persona_manager: Any) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    name = str(body.get("name", "")).strip()
    if not name:
        return _json_response({"error": "缺少 name"}, 400)

    # 禁止特殊字符
    if not name.replace("_", "").replace("-", "").isalnum():
        return _json_response({"error": "name 只能包含字母、数字、下划线和连字符"}, 400)

    persona_name = str(body.get("persona_name", "")).strip() or None
    persona_manager.create_persona(name, persona_name=persona_name)
    return _json_response({"success": True, "name": name})


@handle_api_errors
async def api_personas_delete(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    persona_manager.delete_persona(name)
    return _json_response({"success": True})


async def api_persona_get_single(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    profile = PersonaStore.load(paths.dir)
    if profile is None:
        profile = PersonaProfile(name=name)

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
            "name": name,
            "persona_name": profile.name,
            "status": status,
        }
    )


async def api_persona_status_get(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

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
    return _json_response({"name": name, "status": status})


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


async def api_system_logs_get(request: web.Request, persona_manager: Any) -> web.Response:
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
    log_file = persona_manager.data_path / "logs" / "webui.log"
    payload = _read_log_delta(log_file, offset, lines)
    payload.update({"target": "webui", "name": "WebUI", "path": str(log_file)})
    return _json_response(payload)


async def api_persona_logs_get(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)
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
    log_file = persona_manager.get_log_file(name)
    payload = _read_log_delta(log_file, offset, lines)
    payload.update({"target": "persona", "name": name, "path": str(log_file)})
    return _json_response(payload)


@handle_api_errors
async def api_persona_start(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    persona_manager.start_persona(name)
    return _json_response({"success": True, "message": f"{name} 已启动"})


@handle_api_errors
async def api_persona_stop(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    persona_manager.stop_persona(name)
    return _json_response({"success": True, "message": f"{name} 已停止"})


@handle_api_errors
async def api_persona_restart(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    persona_manager.stop_persona(name)
    await asyncio.sleep(1)
    persona_manager.start_persona(name)
    return _json_response({"success": True, "message": f"{name} 已重启"})


async def api_persona_get(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    profile = PersonaStore.load(paths.dir)
    if profile is None:
        profile = PersonaProfile(name=name)
    return _json_response(
        {
            "name": profile.name,
            "aliases": profile.aliases,
            "persona_summary": profile.persona_summary,
            "full_system_prompt": profile.full_system_prompt,
            "personality_traits": profile.personality_traits,
            "backstory": profile.backstory,
            "core_values": profile.core_values,
            "flaws": profile.flaws,
            "motivations": profile.motivations,
            "communication_style": profile.communication_style,
            "speech_rhythm": profile.speech_rhythm,
            "emoji_preference": profile.emoji_preference,
            "humor_style": profile.humor_style,
            "typical_greetings": profile.typical_greetings,
            "typical_signoffs": profile.typical_signoffs,
            "emotional_baseline": profile.emotional_baseline,
            "emotional_range": profile.emotional_range,
            "empathy_style": profile.empathy_style,
            "stress_response": profile.stress_response,
            "boundaries": profile.boundaries,
            "taboo_topics": profile.taboo_topics,
            "preferred_topics": profile.preferred_topics,
            "social_role": profile.social_role,
            "max_tokens_preference": profile.max_tokens_preference,
            "temperature_preference": profile.temperature_preference,
            "reply_frequency": profile.reply_frequency,
            "version": profile.version,
            "created_at": profile.created_at,
            "source": profile.source,
        }
    )


async def api_persona_post(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    profile = PersonaStore.load(paths.dir)
    if profile is None:
        profile = PersonaProfile(name=name)

    persona_data = body.get("persona", body)
    for key in (
        "name",
        "aliases",
        "persona_summary",
        "full_system_prompt",
        "personality_traits",
        "backstory",
        "core_values",
        "flaws",
        "motivations",
        "communication_style",
        "speech_rhythm",
        "emoji_preference",
        "humor_style",
        "typical_greetings",
        "typical_signoffs",
        "emotional_baseline",
        "emotional_range",
        "empathy_style",
        "stress_response",
        "boundaries",
        "taboo_topics",
        "preferred_topics",
        "social_role",
        "max_tokens_preference",
        "temperature_preference",
        "reply_frequency",
        "version",
        "created_at",
        "source",
    ):
        if key in persona_data:
            setattr(profile, key, persona_data[key])

    PersonaStore.save(paths.dir, profile)
    _request_config_reload(name, "persona", persona_manager)
    return _json_response({"success": True})


async def api_persona_interview_get(request: web.Request, persona_manager: Any) -> web.Response:
    """读取已保存的 interview 问卷答案。"""
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)
    record_path = paths.dir / "engine_state" / "persona_interview_record.json"
    pending_path = paths.dir / "engine_state" / "pending_persona_interview.json"
    try:
        if record_path.exists():
            data = json.loads(record_path.read_text(encoding="utf-8"))
            return _json_response(
                {
                    "answers": data.get("answers", {}),
                    "name": data.get("name", ""),
                    "aliases": data.get("aliases", []),
                }
            )
        if pending_path.exists():
            data = json.loads(pending_path.read_text(encoding="utf-8"))
            return _json_response(
                {
                    "answers": data.get("answers", {}),
                    "name": data.get("name", ""),
                    "aliases": data.get("aliases", []),
                }
            )
        return _json_response({"answers": {}, "name": "", "aliases": []})
    except Exception as exc:
        LOG.warning("读取 interview 记录失败: %s", exc)
        return _json_response({"answers": {}, "name": "", "aliases": []})


@handle_api_errors
async def api_persona_interview(request: web.Request, persona_manager: Any) -> web.Response:
    """根据问卷答案生成人格。"""
    name = _get_name(request)
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)
    p_name = str(body.get("name", "小星")).strip()
    answers = body.get("answers", {})
    aliases = [a.strip() for a in body.get("aliases", []) if isinstance(a, str) and a.strip()]
    model = str(body.get("model", "gpt-4o-mini")).strip()
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    from sirius_pulse.providers.routing import AutoRoutingProvider

    provider_mgr = WorkspaceProviderManager(persona_manager.data_path)
    providers = provider_mgr.load()
    provider = None
    if providers:
        provider = AutoRoutingProvider(providers)
    persona = await generate_persona_from_interview(
        work_path=paths.dir,
        provider=provider,
        name=p_name,
        answers=answers,
        aliases=aliases,
        model=model,
    )
    PersonaStore.save(paths.dir, persona)
    persona_manager.reload_persona(name)
    return _json_response({"success": True, "persona": persona.to_dict()})


async def api_orchestration_get(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    data = OrchestrationStore.load(paths.dir)
    data["model_choices"] = build_model_catalog(persona_manager.data_path)["model_choices"]
    return _json_response(data)


async def api_orchestration_post(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    cfg = OrchestrationStore.load(paths.dir)

    for key in ("analysis_model", "chat_model", "memory_model", "plugin_model", "summary_model"):
        if key in body:
            cfg[key] = body[key]

    for key in ("task_models", "task_temperatures", "task_max_tokens", "task_enabled"):
        if key in body and isinstance(body[key], dict):
            cfg[key] = body[key]

    OrchestrationStore.save(paths.dir, cfg)
    _request_config_reload(name, "orchestration", persona_manager)
    return _json_response({"success": True})


async def api_experience_get(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    exp = PersonaExperienceConfig.load(paths.experience)
    return _json_response(
        {
            "reply_mode": exp.reply_mode,
            "engagement_sensitivity": exp.engagement_sensitivity,
            "expressiveness": exp.expressiveness,
            "heat_window_seconds": exp.heat_window_seconds,
            "proactive_enabled": exp.proactive_enabled,
            "proactive_interval_seconds": exp.proactive_interval_seconds,
            "proactive_active_start_hour": exp.proactive_active_start_hour,
            "proactive_active_end_hour": exp.proactive_active_end_hour,
            "delay_reply_enabled": exp.delay_reply_enabled,
            "pending_message_threshold": exp.pending_message_threshold,
            "min_reply_interval_seconds": exp.min_reply_interval_seconds,
            "reply_frequency_window_seconds": exp.reply_frequency_window_seconds,
            "reply_frequency_max_replies": exp.reply_frequency_max_replies,
            "reply_frequency_exempt_on_mention": exp.reply_frequency_exempt_on_mention,
            "max_concurrent_llm_calls": exp.max_concurrent_llm_calls,
            "memory_depth": exp.memory_depth,
            "diary_top_k": exp.diary_top_k,
            "diary_token_budget": exp.diary_token_budget,
            "enable_skills": exp.enable_skills,
            "max_skill_rounds": exp.max_skill_rounds,
            "skill_execution_timeout": exp.skill_execution_timeout,
            "auto_install_skill_deps": exp.auto_install_skill_deps,
            "other_ai_names": exp.other_ai_names,
            "message_prefixes": exp.message_prefixes,
            "pinned_message_max_carry_count": exp.pinned_message_max_carry_count,
        }
    )


async def api_experience_post(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    exp = PersonaExperienceConfig.load(paths.experience)
    experience_data = body.get("experience", body)

    for key in (
        "reply_mode",
        "engagement_sensitivity",
        "expressiveness",
        "heat_window_seconds",
        "proactive_enabled",
        "proactive_interval_seconds",
        "proactive_active_start_hour",
        "proactive_active_end_hour",
        "delay_reply_enabled",
        "pending_message_threshold",
        "min_reply_interval_seconds",
        "reply_frequency_window_seconds",
        "reply_frequency_max_replies",
        "reply_frequency_exempt_on_mention",
        "max_concurrent_llm_calls",
        "memory_depth",
        "diary_top_k",
        "diary_token_budget",
        "enable_skills",
        "max_skill_rounds",
        "skill_execution_timeout",
        "auto_install_skill_deps",
        "other_ai_names",
        "message_prefixes",
        "pinned_message_max_carry_count",
    ):
        if key in experience_data:
            setattr(exp, key, experience_data[key])

    exp.save(paths.experience)
    _request_config_reload(name, "experience", persona_manager)
    return _json_response({"success": True})


async def api_adapters_get(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    adapters = PersonaAdaptersConfig.load(paths.adapters)
    return _json_response({"adapters": [a.to_dict() for a in adapters.adapters]})


async def api_adapters_post(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    adapters = PersonaAdaptersConfig.load(paths.adapters)
    if "adapters" in body and isinstance(body["adapters"], list):
        adapters.adapters = [AdapterConfig(**a) for a in body["adapters"]]

    adapters.save(paths.adapters)
    return _json_response({"success": True})


async def api_engine_reload(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    # 向 worker 发送重载信号（通过 engine_state/reload.flag）
    flag_path = paths.engine_state / "reload.flag"
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text("1", encoding="utf-8")
    return _json_response({"success": True, "message": "重载信号已发送"})


async def api_config_post(request: web.Request, persona_manager: Any) -> web.Response:
    """更新 adapter 配置（群白名单等），直接写入 adapters.json。"""
    name = _get_name(request)
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

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
    LOG.info("配置已更新 %s: %s", name, {k: body.get(k) for k in body})
    return _json_response({"success": True, "message": "配置已保存"})


@handle_api_errors
async def api_persona_clone(request: web.Request, persona_manager: Any) -> web.Response:
    """克隆人格：复制源人格目录到新人格，分配新端口。"""
    source_name = _get_name(request)
    try:
        body = await request.json()
    except Exception:
        body = {}

    target_name = str(body.get("target_name", "")).strip()
    if not target_name:
        target_name = f"{source_name}_copy"

    # 校验目标名称合法性
    if not target_name.replace("_", "").replace("-", "").isalnum():
        return _json_response({"error": "target_name 只能包含字母、数字、下划线和连字符"}, 400)

    # 检查源人格存在
    source_paths = persona_manager.get_persona_paths(source_name)
    if source_paths is None:
        return _json_response({"error": f"源人格 '{source_name}' 不存在"}, 404)

    # 检查目标人格不存在
    existing = persona_manager.get_persona_paths(target_name)
    if existing is not None:
        return _json_response({"error": f"目标人格 '{target_name}' 已存在"}, 409)

    # 复制目录
    target_dir = persona_manager.personas_dir / target_name
    shutil.copytree(str(source_paths.dir), str(target_dir))

    # 删除引擎运行状态（不应继承源人格的 PID 等）
    engine_state_dir = target_dir / "engine_state"
    if engine_state_dir.exists():
        for f in engine_state_dir.iterdir():
            if f.name.startswith("worker_status"):
                f.unlink(missing_ok=True)

    LOG.info("人格已克隆: %s → %s", source_name, target_name)
    return _json_response({"success": True, "source": source_name, "name": target_name})
