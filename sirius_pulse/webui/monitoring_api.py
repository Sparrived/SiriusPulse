"""WebUI monitoring API -- single-persona monitoring data.

Provides overview, detailed metrics, and health check endpoints.
All data is collected from disk files/databases in read-only mode.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.persona_config import PersonaConfigPaths
from sirius_pulse.webui.server_utils import _json_response, handle_api_errors

LOG = logging.getLogger("sirius.webui.monitoring")


# ======================================================================
# 内部工具函数
# ======================================================================


def _read_worker_status(persona_dir: Path) -> dict[str, Any] | None:
    """读取子进程心跳状态文件，返回原始字典或 None。"""
    path = persona_dir / "engine_state" / "worker_status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _calc_uptime_seconds(status: dict[str, Any] | None) -> float:
    """根据 worker_status 中的 started_at 计算运行时长（秒）。"""
    if not status:
        return 0.0
    started_at = status.get("started_at")
    if not started_at:
        return 0.0
    try:
        started = datetime.fromisoformat(started_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        return max(0.0, elapsed)
    except (ValueError, TypeError):
        return 0.0


def _read_token_usage(persona_dir: Path) -> dict[str, Any]:
    """从 persona.db 读取聚合的 token 使用统计。"""
    empty_cache_stats = {
        "total_calls": 0,
        "cache_info_calls": 0,
        "cache_info_coverage_pct": 0.0,
        "cached_prompt_tokens": 0,
        "uncached_prompt_tokens": 0,
        "cache_creation_prompt_tokens": 0,
        "cache_hit_rate_pct": 0.0,
    }
    empty = {"total_input": 0, "total_output": 0, "call_count": 0, "cache_stats": empty_cache_stats}
    db_path = persona_dir / "persona.db"
    if not db_path.exists():
        return empty
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL;")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(token_usage)")}
        has_cache_columns = {
            "cached_prompt_tokens",
            "uncached_prompt_tokens",
            "cache_creation_prompt_tokens",
            "cache_info_available",
        }.issubset(columns)
        cache_select = (
            "SUM(CASE WHEN cache_info_available != 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN cache_info_available != 0 THEN cached_prompt_tokens ELSE 0 END), "
            "SUM(CASE WHEN cache_info_available != 0 THEN uncached_prompt_tokens ELSE 0 END), "
            "SUM(CASE WHEN cache_info_available != 0 THEN cache_creation_prompt_tokens ELSE 0 END)"
            if has_cache_columns
            else "0, 0, 0, 0"
        )
        row = conn.execute(f"""SELECT
                COALESCE(SUM(prompt_tokens), 0)  AS total_input,
                COALESCE(SUM(completion_tokens), 0) AS total_output,
                COUNT(*) AS call_count,
                {cache_select}
            FROM token_usage""").fetchone()
        conn.close()
        if row:
            total_calls = int(row[2])
            cache_info_calls = int(row[3] or 0)
            cached = int(row[4] or 0)
            uncached = int(row[5] or 0)
            observed = cached + uncached
            return {
                "total_input": int(row[0]),
                "total_output": int(row[1]),
                "call_count": total_calls,
                "cache_stats": {
                    "total_calls": total_calls,
                    "cache_info_calls": cache_info_calls,
                    "cache_info_coverage_pct": round(cache_info_calls * 100.0 / total_calls, 1)
                    if total_calls
                    else 0.0,
                    "cached_prompt_tokens": cached,
                    "uncached_prompt_tokens": uncached,
                    "cache_creation_prompt_tokens": int(row[6] or 0),
                    "cache_hit_rate_pct": round(cached * 100.0 / observed, 1)
                    if observed
                    else 0.0,
                },
            }
    except Exception:
        LOG.debug("读取 token 使用数据失败: %s", db_path, exc_info=True)
    return empty


def _count_diary_entries(persona_dir: Path) -> int:
    """统计日记目录下所有 JSON 文件中的条目总数。"""
    diary_dir = persona_dir / "diary"
    if not diary_dir.exists():
        return 0
    total = 0
    try:
        for path in diary_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                total += len(data.get("entries", []))
            except (OSError, json.JSONDecodeError):
                continue
    except OSError:
        return 0
    return total


def _count_glossary_terms(persona_dir: Path) -> int:
    """统计名词解释条目数。"""
    terms_path = persona_dir / "glossary" / "terms.json"
    if not terms_path.exists():
        return 0
    try:
        data = json.loads(terms_path.read_text(encoding="utf-8"))
        return len(data) if isinstance(data, (dict, list)) else 0
    except (OSError, json.JSONDecodeError):
        return 0


def _count_user_profiles(persona_dir: Path) -> int:
    """统计语义记忆中的用户画像数量（遍历 memory/semantic/users/ 下所有 JSON）。"""
    users_dir = persona_dir / "memory" / "semantic" / "users"
    if not users_dir.exists():
        return 0
    count = 0
    try:
        for group_dir in users_dir.iterdir():
            if not group_dir.is_dir():
                continue
            count += len(list(group_dir.glob("*.json")))
    except OSError:
        return 0
    return count


def _count_cognition_events(persona_dir: Path) -> int:
    """统计认知事件总数。"""
    db_path = persona_dir / "persona.db"
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL;")
        row = conn.execute("SELECT COUNT(*) FROM cognition_events").fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception:
        LOG.debug("读取认知事件数据失败: %s", db_path, exc_info=True)
        return 0


def _check_config_files(persona_dir: Path) -> tuple[str, list[str]]:
    """检查关键配置文件是否存在，返回 (status, missing_list)。"""
    required = ["persona.json", "orchestration.json", "adapters.json", "experience.json"]
    missing = [f for f in required if not (persona_dir / f).exists()]
    return ("ok" if not missing else "missing"), missing


def _check_memory_system(persona_dir: Path) -> str:
    """检查记忆系统是否可访问（至少有一个记忆子目录存在）。"""
    has_memory = any((persona_dir / d).exists() for d in ("memory", "diary", "token"))
    if not has_memory:
        return "empty"
    return "ok"


# ======================================================================
# API 端点
# ======================================================================


@handle_api_errors
async def api_monitoring_overview(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """GET /api/monitoring/overview -- 全局概览。

    返回当前人格的运行状态，用于监控面板首页。
    """
    persona_dir = data_dir
    status_data = _read_worker_status(persona_dir)
    running = status_data.get("running", False) if status_data else False
    pid = status_data.get("pid") if status_data else None

    return _json_response(
        {
            "total_personas": 1,
            "running_personas": 1 if running else 0,
            "personas": [
                {
                    "name": data_dir.name,
                    "running": running,
                    "pid": pid,
                    "uptime_seconds": _calc_uptime_seconds(status_data) if running else 0.0,
                }
            ],
            "total_connections": 0,
        }
    )


@handle_api_errors
async def api_monitoring_persona_metrics(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """GET /api/monitoring/metrics -- 当前人格详细指标。"""
    paths = PersonaConfigPaths(data_dir)
    persona_dir = paths.dir

    status_data = _read_worker_status(persona_dir)
    running = status_data.get("running", False) if status_data else False
    pid = status_data.get("pid") if status_data else None

    token_usage = _read_token_usage(persona_dir)
    diary_count = _count_diary_entries(persona_dir)
    glossary_count = _count_glossary_terms(persona_dir)
    user_count = _count_user_profiles(persona_dir)
    event_count = _count_cognition_events(persona_dir)

    return _json_response(
        {
            "persona": data_dir.name,
            "running": running,
            "pid": pid,
            "uptime_seconds": _calc_uptime_seconds(status_data) if running else 0.0,
            "token_usage": token_usage,
            "memory": {
                "diary_count": diary_count,
                "glossary_count": glossary_count,
                "user_count": user_count,
            },
            "cognition": {
                "event_count": event_count,
            },
        }
    )


@handle_api_errors
async def api_monitoring_health(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """GET /api/monitoring/health -- 健康检查。"""
    paths = PersonaConfigPaths(data_dir)
    persona_dir = paths.dir

    status_data = _read_worker_status(persona_dir)
    running = status_data.get("running", False) if status_data else False
    pid = status_data.get("pid") if status_data else None

    # 进程检查
    process_status = "ok" if running else "down"

    # 配置文件检查
    config_status, missing_files = _check_config_files(persona_dir)

    # 记忆系统检查
    memory_status = _check_memory_system(persona_dir)

    healthy = running and config_status == "ok" and memory_status == "ok"

    return _json_response(
        {
            "persona": data_dir.name,
            "healthy": healthy,
            "checks": {
                "process": {
                    "status": process_status,
                    "pid": pid,
                },
                "config": {
                    "status": config_status,
                    "files": missing_files,
                },
                "memory": {
                    "status": memory_status,
                },
            },
        }
    )
