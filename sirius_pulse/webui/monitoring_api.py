"""WebUI 监控 API — 跨人格聚合监控数据。

提供全局概览、单人格详细指标、健康检查三类端点。
所有数据均以只读方式从磁盘文件/数据库中采集，不修改任何运行时状态。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.webui.server_utils import _get_name, _json_response, handle_api_errors

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


def _read_token_usage(persona_dir: Path) -> dict[str, int]:
    """从 SQLite token_usage.db 读取聚合的 token 使用统计。"""
    db_path = persona_dir / "token" / "token_usage.db"
    if not db_path.exists():
        return {"total_input": 0, "total_output": 0, "call_count": 0}
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL;")
        row = conn.execute(
            """SELECT
                COALESCE(SUM(prompt_tokens), 0)  AS total_input,
                COALESCE(SUM(completion_tokens), 0) AS total_output,
                COUNT(*) AS call_count
            FROM token_usage"""
        ).fetchone()
        conn.close()
        if row:
            return {
                "total_input": int(row[0]),
                "total_output": int(row[1]),
                "call_count": int(row[2]),
            }
    except Exception:
        LOG.debug("读取 token 使用数据失败: %s", db_path, exc_info=True)
    return {"total_input": 0, "total_output": 0, "call_count": 0}


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
    db_path = persona_dir / "cognition_events.db"
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
    has_memory = any(
        (persona_dir / d).exists()
        for d in ("memory", "diary", "token")
    )
    if not has_memory:
        return "empty"
    return "ok"


# ======================================================================
# API 端点
# ======================================================================


@handle_api_errors
async def api_monitoring_overview(
    request: web.Request, persona_manager: Any,
) -> web.Response:
    """GET /api/monitoring/overview — 全局概览。

    返回所有人格的运行状态汇总，用于监控面板首页。
    """
    personas_info: list[dict[str, Any]] = []
    running_count = 0

    for info in persona_manager.list_personas():
        name = info["name"]
        running = persona_manager.is_running(name)
        status_data = _read_worker_status(persona_manager.get_persona_dir(name))

        pid = status_data.get("pid") if status_data else None
        if not running:
            pid = None

        if running:
            running_count += 1

        personas_info.append({
            "name": name,
            "running": running,
            "pid": pid,
            "uptime_seconds": _calc_uptime_seconds(status_data) if running else 0.0,
        })

    return _json_response({
        "total_personas": len(personas_info),
        "running_personas": running_count,
        "personas": personas_info,
        "total_connections": 0,  # 占位，未来由 WS manager 填充
    })


@handle_api_errors
async def api_monitoring_persona_metrics(
    request: web.Request, persona_manager: Any,
) -> web.Response:
    """GET /api/monitoring/{name}/metrics — 单人格详细指标。

    从磁盘采集 token 使用、记忆系统、认知事件等数据，
    供监控面板的单人格详情页展示。
    """
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    persona_dir = paths.dir
    running = persona_manager.is_running(name)
    status_data = _read_worker_status(persona_dir)
    pid = status_data.get("pid") if status_data else None
    if not running:
        pid = None

    token_usage = _read_token_usage(persona_dir)
    diary_count = _count_diary_entries(persona_dir)
    glossary_count = _count_glossary_terms(persona_dir)
    user_count = _count_user_profiles(persona_dir)
    event_count = _count_cognition_events(persona_dir)

    return _json_response({
        "persona": name,
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
    })


@handle_api_errors
async def api_monitoring_health(
    request: web.Request, persona_manager: Any,
) -> web.Response:
    """GET /api/monitoring/{name}/health — 健康检查。

    检查人格进程存活、配置文件完整性、记忆系统可访问性。
    用于运维监控和自动化告警。
    """
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    persona_dir = paths.dir
    running = persona_manager.is_running(name)
    status_data = _read_worker_status(persona_dir)
    pid = status_data.get("pid") if status_data else None
    if not running:
        pid = None

    # 进程检查
    process_status = "ok" if running else "down"

    # 配置文件检查
    config_status, missing_files = _check_config_files(persona_dir)

    # 记忆系统检查
    memory_status = _check_memory_system(persona_dir)

    healthy = running and config_status == "ok" and memory_status == "ok"

    return _json_response({
        "persona": name,
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
    })
