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
import shutil
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
        # 读取 worker 状态以判断是否运行中
        running = False
        worker_status_path = d / "engine_state" / "worker_status.json"
        if worker_status_path.exists():
            try:
                ws = json.loads(worker_status_path.read_text(encoding="utf-8"))
                running = ws.get("status") == "running"
            except Exception:
                pass

        result.append({
            "name": d.name,
            "persona_name": display_name,
            "running": running,
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
    _set_active_persona_name(root_dir, data_dir.name)
    LOG.info("人格已激活: %s", data_dir.name)
    return _json_response({"success": True, "active": data_dir.name})


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
    if active == data_dir.name:
        _set_active_persona_name(root_dir, "")
        LOG.info("人格已停用: %s", data_dir.name)
    return _json_response({"success": True})


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
    worker_status_path = data_dir / "engine_state" / "worker_status.json"
    worker_status: dict = {}
    if worker_status_path.exists():
        try:
            worker_status = json.loads(worker_status_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    return _json_response({
        "name": data_dir.name,
        "active": data_dir.name == active,
        "running": worker_status.get("status") == "running",
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
