"""WebUI MCP 配置 API。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.config.file_io import atomic_json_save
from sirius_pulse.persona_config import PersonaConfigPaths
from sirius_pulse.tools.mcp_client import load_mcp_config
from sirius_pulse.webui.persona_api import _request_config_reload
from sirius_pulse.webui.secret_settings import SECRET_MASK
from sirius_pulse.webui.server_utils import _json_response, handle_api_errors

MCP_SECRET_MASK = SECRET_MASK
_MCP_TRANSPORTS = {"stdio", "streamable_http", "sse"}
_SERVER_NAME_RE = re.compile(r"^[^/\\\x00\r\n]{1,80}$")


def _server_map(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    servers = raw.get("servers", raw)
    if isinstance(servers, dict):
        return {
            str(name): config
            for name, config in servers.items()
            if str(name).strip() and isinstance(config, dict)
        }
    if isinstance(servers, list):
        result: dict[str, dict[str, Any]] = {}
        for config in servers:
            if not isinstance(config, dict):
                continue
            name = str(config.get("name", "")).strip()
            if name:
                result[name] = {key: value for key, value in config.items() if key != "name"}
        return result
    return {}


def _masked_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): MCP_SECRET_MASK for key in value if str(key).strip()}


def _mask_server(config: dict[str, Any]) -> dict[str, Any]:
    result = dict(config)
    for field in ("headers", "env"):
        if field in result:
            result[field] = _masked_mapping(result[field])
    return result


def _masked_servers(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {name: _mask_server(config) for name, config in _server_map(raw).items()}


def _string_mapping(value: Any, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    result: dict[str, str] = {}
    for key, item in value.items():
        key_text = str(key).strip()
        if not key_text:
            raise ValueError(f"{field} 不能包含空键")
        if isinstance(item, (dict, list)):
            raise ValueError(f"{field}.{key_text} 必须是字符串")
        result[key_text] = str(item)
    return result


def _clean_server(
    name: str,
    raw: Any,
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"MCP server {name} 必须是对象")

    transport = str(raw.get("transport", "stdio")).strip().lower()
    if transport not in _MCP_TRANSPORTS:
        raise ValueError(f"MCP server {name} 的 transport 不支持: {transport}")

    enabled = bool(raw.get("enabled", True))
    command = str(raw.get("command", "")).strip()
    url = str(raw.get("url", "")).strip()
    if enabled and transport == "stdio" and not command:
        raise ValueError(f"MCP server {name} 启用 stdio 时必须填写 command")
    if enabled and transport != "stdio" and not url:
        raise ValueError(f"MCP server {name} 启用 {transport} 时必须填写 url")

    args = raw.get("args", [])
    if args is None:
        args = []
    if not isinstance(args, list) or any(isinstance(item, (dict, list)) for item in args):
        raise ValueError(f"MCP server {name} 的 args 必须是字符串数组")

    config: dict[str, Any] = {
        "enabled": enabled,
        "transport": transport,
    }
    if command:
        config["command"] = command
    if args:
        config["args"] = [str(item) for item in args]
    for field in ("cwd", "url"):
        value = str(raw.get(field, "")).strip()
        if value:
            config[field] = value

    for field in ("headers", "env"):
        incoming = _string_mapping(raw.get(field, {}), field)
        previous = _string_mapping((existing or {}).get(field, {}), field)
        values: dict[str, str] = {}
        for key, value in incoming.items():
            if value == MCP_SECRET_MASK and key in previous:
                values[key] = previous[key]
            elif value != MCP_SECRET_MASK:
                values[key] = value
        if values:
            config[field] = values

    return config


@handle_api_errors
async def api_persona_mcp_get(request: web.Request, data_dir: Path) -> web.Response:
    """GET /api/persona/mcp — 获取当前人格的 MCP 配置（敏感值遮罩）。"""
    paths = PersonaConfigPaths(data_dir)
    raw = load_mcp_config(paths.mcp)
    servers = _masked_servers(raw)
    return _json_response(
        {
            "servers": servers,
            "server_count": len(servers),
            "secret_mask": MCP_SECRET_MASK,
        }
    )


@handle_api_errors
async def api_persona_mcp_post(request: web.Request, data_dir: Path) -> web.Response:
    """POST /api/persona/mcp — 保存 MCP 配置并请求运行时重载。"""
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    raw_servers = body.get("servers") if isinstance(body, dict) else None
    if not isinstance(raw_servers, dict):
        return _json_response({"error": "servers 必须是对象"}, 400)

    paths = PersonaConfigPaths(data_dir)
    existing = _server_map(load_mcp_config(paths.mcp))
    servers: dict[str, dict[str, Any]] = {}
    try:
        for raw_name, raw_config in raw_servers.items():
            name = str(raw_name).strip()
            if not _SERVER_NAME_RE.fullmatch(name):
                raise ValueError(f"MCP server 名称无效: {name}")
            servers[name] = _clean_server(name, raw_config, existing.get(name))
    except ValueError as exc:
        return _json_response({"error": str(exc)}, 400)

    atomic_json_save(paths.mcp, {"servers": servers})
    _request_config_reload("mcp", data_dir)
    return _json_response(
        {
            "success": True,
            "servers": {name: _mask_server(config) for name, config in servers.items()},
            "reload_requested": True,
        }
    )
