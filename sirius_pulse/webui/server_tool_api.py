"""WebUI Tool 管理 API — Tool 配置与启停。

所有 tool 的配置和启停状态统一存储在各自的 data_store 文件中：
  {data_dir}/tool_data/{tool_name}.json
其中 _enabled 字段表示启停状态，其余为 tool 配置参数。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.persona_config import PersonaConfigPaths
from sirius_pulse.tools.registry import ToolRegistry
from sirius_pulse.webui.server_utils import _json_response, handle_api_errors

LOG = logging.getLogger("sirius.webui")

# ── 模块级缓存，避免每次 API 请求都重新扫描磁盘和执行 importlib ──
_tool_registry_cache: dict[str, tuple[float, ToolRegistry]] = {}
_CACHE_TTL = 60.0  # 秒


def _load_tool_registry(persona_dir: Path) -> ToolRegistry:
    """从人格目录加载所有 tool（内置 + 人格级），带模块级缓存。"""
    key = str(persona_dir)
    now = time.monotonic()
    cached = _tool_registry_cache.get(key)
    if cached is not None:
        ts, registry = cached
        if now - ts < _CACHE_TTL:
            return registry
    registry = ToolRegistry()
    registry.load_from_directory(
        persona_dir / "tools",
        auto_install_deps=False,
        include_builtin=True,
    )
    _tool_registry_cache[key] = (now, registry)
    return registry


def _load_tool_data_store(persona_dir: Path, tool_name: str) -> dict[str, Any]:
    """从 data_store 文件读取 tool 的完整数据（配置 + 运行时状态）。"""
    store_path = persona_dir / "tool_data" / f"{tool_name}.json"
    if not store_path.exists():
        return {}
    try:
        raw = json.loads(store_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        LOG.warning("读取 tool data_store 失败: %s", store_path, exc_info=True)
        return {}


def _save_tool_data_store(persona_dir: Path, tool_name: str, data: dict[str, Any]) -> None:
    """原子写入 tool 的 data_store 文件。"""
    from sirius_pulse.config.file_io import atomic_json_save

    store_path = persona_dir / "tool_data" / f"{tool_name}.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_save(store_path, data)


def _extract_config(data: dict[str, Any], tool: Any) -> dict[str, Any]:
    """从 data_store 数据中提取配置字段（过滤掉 _ 前缀的元数据和运行时字段）。"""
    config_keys: set[str] = {
        p.name for p in [*tool.parameters, *getattr(tool, "config_parameters", [])]
    }
    return {k: v for k, v in data.items() if k in config_keys}


@handle_api_errors
async def api_persona_tools_get(request: web.Request, data_dir: Path) -> web.Response:
    """GET /api/persona/tools — 列出所有人格级 tool。"""
    paths = PersonaConfigPaths(data_dir)
    registry = _load_tool_registry(paths.dir)

    tools: list[dict[str, Any]] = []
    for tool in registry.all_tools():
        data = _load_tool_data_store(paths.dir, tool.name)
        tools.append(
            {
                "name": tool.name,
                "description": tool.description,
                "version": tool.version,
                "enabled": data.get("_enabled", True),
                "developer_only": tool.developer_only,
                "silent": tool.silent,
                "tags": tool.tags,
                "adapter_types": tool.adapter_types,
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "description": p.description,
                        "required": p.required,
                        "default": p.default,
                    }
                    for p in tool.parameters
                ],
                "config_parameters": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "description": p.description,
                        "required": p.required,
                        "default": p.default,
                    }
                    for p in getattr(tool, "config_parameters", [])
                ],
                "config": _extract_config(data, tool),
            }
        )

    return _json_response({"tools": tools})


@handle_api_errors
async def api_persona_tool_toggle(request: web.Request, data_dir: Path) -> web.Response:
    """POST /api/persona/tools/{tool_name}/toggle — 启停 tool。"""
    tool_name = str(request.match_info.get("tool_name", "")).strip()
    if not tool_name:
        return _json_response({"error": "缺少 tool_name"}, 400)

    paths = PersonaConfigPaths(data_dir)

    try:
        body = await request.json()
    except Exception:
        body = {}

    enabled = bool(body.get("enabled", True))

    data = _load_tool_data_store(paths.dir, tool_name)
    data["_enabled"] = enabled
    _save_tool_data_store(paths.dir, tool_name, data)

    LOG.info("Tool %s enabled=%s", tool_name, enabled)
    return _json_response({"success": True, "tool": tool_name, "enabled": enabled})


@handle_api_errors
async def api_persona_tool_config_get(request: web.Request, data_dir: Path) -> web.Response:
    """GET /api/persona/tools/{tool_name}/config — 获取 tool 配置。"""
    tool_name = str(request.match_info.get("tool_name", "")).strip()
    if not tool_name:
        return _json_response({"error": "缺少 tool_name"}, 400)

    paths = PersonaConfigPaths(data_dir)

    registry = _load_tool_registry(paths.dir)
    tool = registry.get(tool_name)

    data = _load_tool_data_store(paths.dir, tool_name)
    config = _extract_config(data, tool) if tool else {}

    meta: dict[str, Any] = {}
    if tool is not None:
        meta = {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.get_parameter_schema(),
            "config_parameters": [
                {
                    "name": p.name,
                    "type": p.type,
                    "description": p.description,
                    "required": p.required,
                    "default": p.default,
                }
                for p in getattr(tool, "config_parameters", [])
            ],
        }

    return _json_response(
        {
            "tool": tool_name,
            "config": config,
            "enabled": data.get("_enabled", True),
            "meta": meta,
        }
    )


@handle_api_errors
async def api_persona_tool_history_get(request: web.Request, data_dir: Path) -> web.Response:
    """GET /api/persona/tool-history — 返回 TOOL 执行历史详情（分页，支持筛选）。"""
    from sirius_pulse.tools.telemetry import ToolTelemetry

    tool_name = request.query.get("tool_name", "").strip() or None
    success_str = request.query.get("success", "").strip().lower()
    caller = request.query.get("caller", "").strip()
    limit = min(int(request.query.get("limit", "50")), 200)
    offset = max(int(request.query.get("offset", "0")), 0)

    success_filter: bool | None = None
    if success_str == "true":
        success_filter = True
    elif success_str == "false":
        success_filter = False

    paths = PersonaConfigPaths(data_dir)

    telemetry_path = paths.dir / "tool_data" / ".telemetry.jsonl"
    if not telemetry_path.exists():
        return _json_response({"history": [], "total": 0, "stats": {}})

    telemetry = ToolTelemetry(telemetry_path)
    records, total = telemetry.query(
        tool_name=tool_name, success=success_filter, limit=limit, offset=offset
    )

    # caller 筛选（在 query 之后过滤）
    if caller:
        records = [r for r in records if caller in (r.caller_user_id or "")]
        total = len(records)
    items: list[dict[str, Any]] = []
    for rec in reversed(records):
        item: dict[str, Any] = {
            "tool_name": rec.tool_name,
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
async def api_persona_tool_config_post(request: web.Request, data_dir: Path) -> web.Response:
    """POST /api/persona/tools/{tool_name}/config — 保存 tool 配置。"""
    tool_name = str(request.match_info.get("tool_name", "")).strip()
    if not tool_name:
        return _json_response({"error": "缺少 tool_name"}, 400)

    paths = PersonaConfigPaths(data_dir)

    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    # 读取现有 data_store（保留运行时字段如 _last_poll_at）
    data = _load_tool_data_store(paths.dir, tool_name)

    # 合并配置：新配置覆盖同名键，运行时字段保留
    tool_cfg = body.get("config", {})
    if isinstance(tool_cfg, dict):
        data.update(tool_cfg)

    if "enabled" in body:
        data["_enabled"] = bool(body["enabled"])

    _save_tool_data_store(paths.dir, tool_name, data)

    LOG.info("Tool 配置已保存 %s", tool_name)
    return _json_response({"success": True, "tool": tool_name})
