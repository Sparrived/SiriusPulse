"""人格管理 API — 多人格 CRUD 与切换。

端点：
    GET  /api/personas                  — 列出所有人格
    POST /api/personas                  — 创建新人格
    GET  /api/personas/active           — 获取当前活跃人格
    POST /api/personas/{name}/activate  — 切换活跃人格
    DELETE /api/personas/{name}         — 删除人格
"""

from __future__ import annotations

import json
import logging
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.webui.server_utils import _json_response, handle_api_errors

LOG = logging.getLogger("sirius.persona_manager")


@handle_api_errors
async def api_personas_list(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """列出所有人格。"""
    personas_dir = data_dir / "personas"
    if not personas_dir.exists():
        return _json_response({"personas": []})

    active = _get_active_persona_name(data_dir)
    result = []
    for d in sorted(personas_dir.iterdir()):
        if not d.is_dir():
            continue
        persona_file = d / "persona.json"
        display_name = d.name
        has_config = persona_file.exists()
        if has_config:
            try:
                data = json.loads(persona_file.read_text(encoding="utf-8"))
                display_name = data.get("name", d.name)
            except Exception:
                pass
        worker_status = _read_worker_status(d)
        running = _is_persona_running(d, worker_status)

        result.append({
            "name": d.name,
            "persona_name": display_name,
            "running": running,
            "pid": worker_status.get("pid"),
            "heartbeat_at": worker_status.get("heartbeat_at"),
            "started_at": worker_status.get("started_at"),
            "active": d.name == active,
            "has_config": has_config,
        })

    return _json_response({"personas": result, "active": active})


@handle_api_errors
async def api_persona_create(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """创建新人格。"""
    body = await request.json()
    name = str(body.get("name", "")).strip()

    if not name:
        return _json_response({"error": "人格名称不能为空"}, 400)

    # 验证名称合法（只允许字母数字中文下划线连字符）
    import re

    if not re.match(r"^[a-zA-Z0-9_\-一-鿿]+$", name):
        return _json_response({"error": "人格名称只能包含字母、数字、中文、下划线和连字符"}, 400)

    persona_dir = data_dir / "personas" / name
    if persona_dir.exists():
        return _json_response({"error": f"人格「{name}」已存在"}, 409)

    # 创建目录结构
    persona_dir.mkdir(parents=True)
    for subdir in ("engine_state", "archive", "plugins", "skills", "logs", "image_cache"):
        (persona_dir / subdir).mkdir(exist_ok=True)

    # 创建默认配置文件
    display_name = body.get("display_name", name)
    (persona_dir / "persona.json").write_text(
        json.dumps({"name": display_name, "aliases": []}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (persona_dir / "experience.json").write_text(
        json.dumps({}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (persona_dir / "adapters.json").write_text(
        json.dumps({"adapters": [{"type": "napcat", "enabled": False}]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    LOG.info("已创建人格: %s", name)
    return _json_response({"success": True, "name": name})


@handle_api_errors
async def api_persona_active_get(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """获取当前活跃人格。"""
    active = _get_active_persona_name(data_dir)
    return _json_response({"active": active})


@handle_api_errors
async def api_persona_activate(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """切换活跃人格。"""
    name = request.match_info.get("name", "").strip()
    if not name:
        return _json_response({"error": "人格名称不能为空"}, 400)

    persona_dir = data_dir / "personas" / name
    if not persona_dir.exists():
        return _json_response({"error": f"人格「{name}」不存在"}, 404)

    _set_active_persona_name(data_dir, name)
    LOG.info("已切换活跃人格: %s", name)
    return _json_response({"success": True, "active": name})


@handle_api_errors
async def api_persona_start(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """激活当前人格。

    data_dir 是 persona_dir (data/personas/{name}/)。
    需要找到根目录来写 global_config.json。
    前端调用 POST /api/persona/start。
    """
    root_dir = _find_root_dir(data_dir)
    if not (data_dir / "persona.json").exists():
        return _json_response({"error": f"无效的人格目录: {data_dir.name}"}, 400)
    _set_active_persona_name(root_dir, data_dir.name)
    start_result = _start_persona_process(root_dir, data_dir)
    LOG.info("人格已启动: %s pid=%s", data_dir.name, start_result.get("pid"))
    return _json_response({"success": True, "active": data_dir.name, **start_result})


@handle_api_errors
async def api_persona_stop(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """停用当前人格。

    前端调用 POST /api/persona/stop。
    """
    root_dir = _find_root_dir(data_dir)
    active = _get_active_persona_name(root_dir)
    stop_result = _stop_persona_process(data_dir)
    if active == data_dir.name:
        _set_active_persona_name(root_dir, "")
        LOG.info("人格已停用: %s", data_dir.name)
    return _json_response({"success": True, **stop_result})


@handle_api_errors
async def api_persona_status(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """获取当前人格的运行状态。

    前端调用 GET /api/persona/status。
    """
    root_dir = _find_root_dir(data_dir)
    active = _get_active_persona_name(root_dir)
    worker_status = _read_worker_status(data_dir)
    running = _is_persona_running(data_dir, worker_status)

    return _json_response({
        "name": data_dir.name,
        "active": data_dir.name == active,
        "running": running,
        "pid": worker_status.get("pid"),
        "heartbeat_at": worker_status.get("heartbeat_at"),
        "started_at": worker_status.get("started_at"),
    })


@handle_api_errors
async def api_persona_delete(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """删除人格。"""
    name = request.match_info.get("name", "").strip()
    if not name:
        return _json_response({"error": "人格名称不能为空"}, 400)

    active = _get_active_persona_name(data_dir)
    if name == active:
        return _json_response({"error": "不能删除当前活跃的人格"}, 400)

    persona_dir = data_dir / "personas" / name
    if not persona_dir.exists():
        return _json_response({"error": f"人格「{name}」不存在"}, 404)

    shutil.rmtree(persona_dir)
    LOG.info("已删除人格: %s", name)
    return _json_response({"success": True})


# ------------------------------------------------------------------
# 辅助函数
# ------------------------------------------------------------------


def _find_root_dir(persona_dir: Path) -> Path:
    """从人格目录推导根数据目录。

    persona_dir = data/personas/{name}/
    root = data/
    """
    # personas/{name} → 上两级就是 root
    if persona_dir.parent.name == "personas":
        return persona_dir.parent.parent
    # 兼容旧格式（persona_dir == root）
    return persona_dir


def _read_worker_status(persona_dir: Path) -> dict[str, Any]:
    status_path = persona_dir / "engine_state" / "worker_status.json"
    if not status_path.exists():
        return {}
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_worker_status(persona_dir: Path, status: dict[str, Any]) -> None:
    status_path = persona_dir / "engine_state" / "worker_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = status_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    tmp.replace(status_path)


def _pid_exists(pid: Any) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
    except OSError:
        return False
    return True


def _is_persona_running(persona_dir: Path, worker_status: dict[str, Any] | None = None) -> bool:
    status = worker_status if worker_status is not None else _read_worker_status(persona_dir)
    return status.get("status") in {"starting", "running"} and _pid_exists(status.get("pid"))


def _start_persona_process(root_dir: Path, persona_dir: Path) -> dict[str, Any]:
    worker_status = _read_worker_status(persona_dir)
    if _is_persona_running(persona_dir, worker_status):
        return {"started": False, "already_running": True, "pid": worker_status.get("pid")}

    log_level = "INFO"
    try:
        config = json.loads((root_dir / "global_config.json").read_text(encoding="utf-8"))
        log_level = str(config.get("log_level") or log_level).upper()
    except Exception:
        pass

    log_path = persona_dir / "logs" / "persona_stdout.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "sirius_pulse.persona_worker",
        "--config",
        str(persona_dir),
        "--log-level",
        log_level,
    ]
    kwargs: dict[str, Any] = {
        "cwd": str(root_dir.parent),
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    with log_path.open("ab") as log_file:
        process = subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT, **kwargs)
    _write_worker_status(
        persona_dir,
        {"status": "starting", "pid": process.pid, "started_at": _now_iso()},
    )
    return {"started": True, "already_running": False, "pid": process.pid}


def _stop_persona_process(persona_dir: Path) -> dict[str, Any]:
    worker_status = _read_worker_status(persona_dir)
    pid = worker_status.get("pid")
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        pid_int = 0

    if pid_int <= 0 or not _pid_exists(pid_int):
        _write_worker_status(persona_dir, {"status": "stopped", "pid": pid, "stopped_at": _now_iso()})
        return {"stopped": False, "pid": pid, "reason": "not_running"}

    if pid_int == os.getpid():
        return {"stopped": False, "pid": pid_int, "reason": "in_process"}

    os.kill(pid_int, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _pid_exists(pid_int):
            return {"stopped": True, "pid": pid_int}
        time.sleep(0.1)

    if hasattr(signal, "SIGKILL"):
        os.kill(pid_int, signal.SIGKILL)
        return {"stopped": True, "pid": pid_int, "forced": True}
    return {"stopped": False, "pid": pid_int, "reason": "still_running"}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _get_active_persona_name(data_dir: Path) -> str:
    """从 global_config.json 读取活跃人格名。"""
    config_path = data_dir / "global_config.json"
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            return data.get("active_persona", "")
        except Exception:
            pass
    return ""


def _set_active_persona_name(data_dir: Path, name: str) -> None:
    """写入活跃人格名到 global_config.json。"""
    config_path = data_dir / "global_config.json"
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["active_persona"] = name
    config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
