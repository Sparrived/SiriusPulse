"""WebUI API endpoints for NapCat management."""

from __future__ import annotations

from typing import Any

from aiohttp import web

from sirius_pulse.webui.server_core import _json_response, LOG


async def api_napcat_status(request: web.Request, napcat_manager: Any | None) -> web.Response:
    if napcat_manager is None:
        return _json_response({"enabled": False, "message": "NapCat 管理未启用"})
    return _json_response({"enabled": True, **napcat_manager.get_status()})


async def api_napcat_install(request: web.Request, napcat_manager: Any | None) -> web.Response:
    if napcat_manager is None:
        return _json_response({"success": False, "message": "NapCat 管理未启用"}, 400)
    try:
        result = await napcat_manager.install()
        return _json_response(result)
    except Exception as exc:
        LOG.exception("NapCat 安装失败")
        return _json_response({"success": False, "message": str(exc)}, 500)


async def api_napcat_configure(request: web.Request, napcat_manager: Any | None) -> web.Response:
    if napcat_manager is None:
        return _json_response({"success": False, "message": "NapCat 管理未启用"}, 400)
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)
    try:
        result = await napcat_manager.configure(
            qq_number=str(body.get("qq_number", "")),
            ws_port=int(body.get("ws_port", 3001)),
        )
        return _json_response(result)
    except Exception as exc:
        LOG.exception("NapCat 配置失败")
        return _json_response({"success": False, "message": str(exc)}, 500)


async def api_napcat_logs(request: web.Request, napcat_manager: Any | None) -> web.Response:
    if napcat_manager is None:
        return _json_response({"enabled": False, "logs": []})
    lines = int(request.query.get("lines", "100"))
    try:
        return _json_response({
            "enabled": True,
            "logs": napcat_manager.get_logs(lines=lines),
        })
    except Exception as exc:
        LOG.warning("读取 NapCat 日志失败: %s", exc)
        return _json_response({"enabled": True, "logs": [], "error": str(exc)})


async def api_napcat_start(request: web.Request, napcat_manager: Any | None) -> web.Response:
    if napcat_manager is None:
        return _json_response({"success": False, "message": "NapCat 管理未启用"}, 400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    qq_number = str(body.get("qq_number", "")).strip()
    if not qq_number:
        return _json_response({"success": False, "message": "QQ 号码不能为空"}, 400)
    try:
        result = await napcat_manager.start(qq_number)
        return _json_response(result)
    except Exception as exc:
        LOG.exception("NapCat 启动失败")
        return _json_response({"success": False, "message": str(exc)}, 500)


async def api_napcat_stop(request: web.Request, napcat_manager: Any | None) -> web.Response:
    if napcat_manager is None:
        return _json_response({"success": False, "message": "NapCat 管理未启用"}, 400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    qq_number = str(body.get("qq_number", "")).strip()
    if not qq_number:
        return _json_response({"success": False, "message": "QQ 号码不能为空"}, 400)
    try:
        result = await napcat_manager.stop(qq_number)
        return _json_response(result)
    except Exception as exc:
        LOG.exception("NapCat 停止失败")
        return _json_response({"success": False, "message": str(exc)}, 500)
