"""WebUI Plugin 管理 API — 全局 Plugin 查阅、启停控制与配置管理。

Plugin 目录位于项目根 plugins/（与 data/ 同级），
配置持久化到 plugins/_config.json。

v1.2+: 支持插件自定义配置（如 chat_analyzer 的时间配置）
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.plugins.config import get_config_manager
from sirius_pulse.plugins.loader import PluginLoader
from sirius_pulse.plugins.models import PluginDefinition
from sirius_pulse.webui.server_utils import _json_response, handle_api_errors

LOG = logging.getLogger("sirius.webui")

# ── 模块级缓存，避免每次 API 请求都重新扫描磁盘和执行 importlib ──
_plugin_definitions_cache: dict[str, tuple[float, list[PluginDefinition]]] = {}
_CACHE_TTL = 60.0  # 秒


def _plugins_dir(manager: Any) -> Path:
    """获取项目根 plugins/ 目录。"""
    return Path(manager.data_path).parent / "plugins"


def _get_config_manager(manager: Any) -> Any:
    """获取配置管理器实例。"""
    return get_config_manager(_plugins_dir(manager))


def _invalidate_plugin_cache(plugins_dir: Path) -> None:
    """清除指定插件目录的定义缓存。"""
    _plugin_definitions_cache.pop(str(plugins_dir), None)


def _load_definitions_cached(plugins_dir: Path) -> list[PluginDefinition]:
    """加载插件定义，带模块级缓存。"""
    key = str(plugins_dir)
    now = time.monotonic()
    cached = _plugin_definitions_cache.get(key)
    if cached is not None:
        ts, definitions = cached
        if now - ts < _CACHE_TTL:
            return definitions
    loader = PluginLoader(plugins_dir)
    definitions = loader.load_all_definitions()
    _plugin_definitions_cache[key] = (now, definitions)
    return definitions


# ═══════════════════════════════════════════════════════════════════════
# API: 插件列表
# ═══════════════════════════════════════════════════════════════════════


@handle_api_errors
async def api_plugins_get(request: web.Request, manager: Any) -> web.Response:
    """GET /api/plugins — 列出所有插件及其元数据。"""
    plugins_dir = _plugins_dir(manager)
    if not plugins_dir.exists():
        return _json_response({"plugins": []})

    definitions = _load_definitions_cached(plugins_dir)
    config_manager = _get_config_manager(manager)

    plugins: list[dict[str, Any]] = []
    for d in definitions:
        plugin_config = config_manager.get_config(d.name)
        source_file = _find_source_file(d.source_path) if d.source_path else None

        plugins.append(
            {
                "name": d.name,
                "display_name": d.display_name or d.name,
                "description": d.description,
                "version": d.version,
                "author": d.author,
                "enabled": plugin_config["enabled"],
                "prompt_inject": d.prompt_inject or "",
                "permissions": {
                    "hidden_from_intent": d.permissions.hidden_from_intent,
                },
                "commands": [
                    {
                        "name": c.name,
                        "patterns": c.patterns,
                        "pattern_type": c.pattern_type,
                        "description": c.description,
                        "hidden_from_intent": c.hidden_from_intent,
                    }
                    for c in d.commands
                ],
                "events": [
                    {
                        "type": e.type,
                        "cron": e.cron,
                        "description": e.description,
                    }
                    for e in d.events
                ],
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "description": p.description,
                        "required": p.required,
                        "default": p.default,
                    }
                    for p in d.parameters
                ],
                "nl_examples": d.natural_language.examples if d.natural_language else [],
                "source_file": source_file,
                "has_source": source_file is not None,
                "settings": plugin_config["settings"],
            }
        )

    return _json_response({"plugins": plugins})


# ═══════════════════════════════════════════════════════════════════════
# API: github_monitor 仓库列表（供插件表单 active_repos 复用）
# ═══════════════════════════════════════════════════════════════════════


async def api_plugin_monitor_repos_get(request: web.Request, manager: Any) -> web.Response:
    """GET /api/plugins/monitor_repos — 获取 github_monitor 的仓库列表。"""
    import json as _json
    from pathlib import Path as _Path

    data_path = _Path(manager.data_path)
    repo_names: list[str] = []

    for persona_dir in sorted(data_path.glob("personas/*")):
        if not persona_dir.is_dir():
            continue
        monitor_path = persona_dir / "skill_data" / "github_monitor.json"
        if not monitor_path.exists():
            continue
        try:
            raw = _json.loads(monitor_path.read_text(encoding="utf-8"))
            repos_data = raw.get("repos", [])
            if isinstance(repos_data, list):
                for r in repos_data:
                    owner = str(r.get("owner", "")).strip()
                    repo = str(r.get("repo", "")).strip()
                    if owner and repo:
                        repo_names.append(f"{owner}/{repo}")
        except Exception:
            continue

    return _json_response({"repos": repo_names})


def _find_source_file(plugin_path: Path | None) -> str | None:
    """查找插件目录下的主 .py 文件，返回文件名。"""
    if plugin_path is None or not plugin_path.exists():
        return None
    py_files = sorted(plugin_path.glob("*.py"), key=lambda p: (p.name != "__init__.py", p.name))
    for pf in py_files:
        if not pf.name.startswith("_"):
            return pf.name
    return None


# ═══════════════════════════════════════════════════════════════════════
# API: 插件详情（含源码）
# ═══════════════════════════════════════════════════════════════════════


async def api_plugin_detail_get(request: web.Request, manager: Any) -> web.Response:
    """GET /api/plugins/{plugin_name} — 获取插件详情，含源码内容。"""
    plugin_name = str(request.match_info.get("plugin_name", "")).strip()
    if not plugin_name:
        return _json_response({"error": "缺少 plugin_name"}, 400)

    plugins_dir = _plugins_dir(manager)
    config_manager = _get_config_manager(manager)
    config_manager.reload()  # 热重载，确保读取最新的磁盘配置

    definitions = _load_definitions_cached(plugins_dir)
    definition = next((d for d in definitions if d.name == plugin_name), None)
    if definition is None:
        return _json_response({"error": f"插件 {plugin_name} 不存在"}, 404)

    plugin_config = config_manager.get_config(plugin_name)
    source_file = _find_source_file(definition.source_path)
    source_content = ""

    if source_file and definition.source_path:
        source_path = definition.source_path / source_file
        if source_path.exists():
            try:
                source_content = source_path.read_text(encoding="utf-8")
            except Exception as exc:
                LOG.warning("读取源码失败 %s: %s", source_path, exc)

    return _json_response(
        {
            "name": definition.name,
            "display_name": definition.display_name or definition.name,
            "description": definition.description,
            "version": definition.version,
            "author": definition.author,
            "prompt_inject": definition.prompt_inject or "",
            "hidden_from_intent": definition.permissions.hidden_from_intent,
            "enabled": plugin_config["enabled"],
            "commands": [
                {
                    "name": c.name,
                    "patterns": c.patterns,
                    "pattern_type": c.pattern_type,
                    "description": c.description,
                    "examples": c.examples,
                    "hidden_from_intent": c.hidden_from_intent,
                }
                for c in definition.commands
            ],
            "events": [
                {
                    "type": e.type,
                    "cron": e.cron,
                    "description": e.description,
                }
                for e in definition.events
            ],
            "parameters": [
                {
                    "name": p.name,
                    "type": p.type,
                    "description": p.description,
                    "required": p.required,
                    "default": p.default,
                    "choices": p.choices,
                    "group": p.group,
                }
                for p in definition.parameters
            ],
            "nl_examples": (
                definition.natural_language.examples if definition.natural_language else []
            ),
            "nl_slots": definition.natural_language.slots if definition.natural_language else {},
            "source_file": source_file,
            "source_content": source_content,
            "settings": plugin_config["settings"],
            "permissions": plugin_config["permissions"],
        }
    )


# ═══════════════════════════════════════════════════════════════════════
# API: 启用/禁用
# ═══════════════════════════════════════════════════════════════════════


@handle_api_errors
async def api_plugin_toggle(request: web.Request, manager: Any) -> web.Response:
    """POST /api/plugins/{plugin_name}/toggle — 启用/禁用插件。"""
    plugin_name = str(request.match_info.get("plugin_name", "")).strip()
    if not plugin_name:
        return _json_response({"error": "缺少 plugin_name"}, 400)

    try:
        body = await request.json()
    except Exception:
        body = {}

    enabled = bool(body.get("enabled", True))

    config_manager = _get_config_manager(manager)
    config_manager.set_enabled(plugin_name, enabled)
    LOG.info("插件 %s enabled=%s", plugin_name, enabled)
    return _json_response({"success": True, "plugin": plugin_name, "enabled": enabled})


# ═══════════════════════════════════════════════════════════════════════
# API: 插件权限配置
# ═══════════════════════════════════════════════════════════════════════


async def api_plugin_config_get(request: web.Request, manager: Any) -> web.Response:
    """GET /api/plugins/{plugin_name}/config — 获取插件权限配置。"""
    plugin_name = str(request.match_info.get("plugin_name", "")).strip()
    if not plugin_name:
        return _json_response({"error": "缺少 plugin_name"}, 400)

    config_manager = _get_config_manager(manager)
    permissions = config_manager.get_permissions(plugin_name)

    return _json_response(
        {
            "plugin": plugin_name,
            "group_blacklist": permissions.get("group_blacklist", []),
            "developer_only": permissions.get("developer_only", False),
            "hidden_from_intent": permissions.get("hidden_from_intent", False),
            "rate_limit_calls_per_minute": permissions.get("rate_limit_calls_per_minute", 60),
        }
    )


async def api_plugin_config_post(request: web.Request, manager: Any) -> web.Response:
    """PUT /api/plugins/{plugin_name}/config — 保存插件权限配置。"""
    plugin_name = str(request.match_info.get("plugin_name", "")).strip()
    if not plugin_name:
        return _json_response({"error": "缺少 plugin_name"}, 400)

    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    config_manager = _get_config_manager(manager)
    permissions: dict[str, Any] = {}

    if "group_blacklist" in body and isinstance(body["group_blacklist"], list):
        permissions["group_blacklist"] = [
            str(v).strip() for v in body["group_blacklist"] if str(v).strip()
        ]

    if "developer_only" in body:
        permissions["developer_only"] = bool(body["developer_only"])

    if "hidden_from_intent" in body:
        permissions["hidden_from_intent"] = bool(body["hidden_from_intent"])

    if "rate_limit_calls_per_minute" in body:
        try:
            permissions["rate_limit_calls_per_minute"] = int(body["rate_limit_calls_per_minute"])
        except (ValueError, TypeError):
            LOG.warning("解析 rate_limit 失败", exc_info=True)
            pass

    config_manager.update_permissions(plugin_name, permissions)

    LOG.info("插件权限配置已保存: %s", plugin_name)
    return _json_response({"success": True, "plugin": plugin_name})


# ═══════════════════════════════════════════════════════════════════════
# API: 插件自定义配置（如 chat_analyzer 的时间配置）
# ═══════════════════════════════════════════════════════════════════════


async def api_plugin_settings_get(request: web.Request, manager: Any) -> web.Response:
    """GET /api/plugins/{plugin_name}/settings — 获取插件自定义配置。"""
    plugin_name = str(request.match_info.get("plugin_name", "")).strip()
    if not plugin_name:
        return _json_response({"error": "缺少 plugin_name"}, 400)

    config_manager = _get_config_manager(manager)
    settings = config_manager.get_settings(plugin_name)

    return _json_response(
        {
            "plugin": plugin_name,
            "settings": settings,
        }
    )


async def api_plugin_settings_post(request: web.Request, manager: Any) -> web.Response:
    """POST /api/plugins/{plugin_name}/settings — 更新插件自定义配置（完整覆盖）。"""
    plugin_name = str(request.match_info.get("plugin_name", "")).strip()
    if not plugin_name:
        return _json_response({"error": "缺少 plugin_name"}, 400)

    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    settings = body.get("settings", {})
    if not isinstance(settings, dict):
        return _json_response({"error": "settings 必须是对象"}, 400)

    config_manager = _get_config_manager(manager)
    config_manager.update_settings(plugin_name, settings)

    LOG.info("插件自定义配置已保存: %s", plugin_name)
    return _json_response({"success": True, "plugin": plugin_name, "settings": settings})


async def api_plugin_setting_post(request: web.Request, manager: Any) -> web.Response:
    """POST /api/plugins/{plugin_name}/settings/{key} — 设置单个配置项。"""
    plugin_name = str(request.match_info.get("plugin_name", "")).strip()
    key = str(request.match_info.get("key", "")).strip()

    if not plugin_name:
        return _json_response({"error": "缺少 plugin_name"}, 400)
    if not key:
        return _json_response({"error": "缺少 key"}, 400)

    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    value = body.get("value")

    config_manager = _get_config_manager(manager)
    config_manager.set_setting(plugin_name, key, value)

    LOG.info("插件配置项已保存: %s.%s", plugin_name, key)
    return _json_response({"success": True, "plugin": plugin_name, "key": key, "value": value})


async def api_plugin_setting_delete(request: web.Request, manager: Any) -> web.Response:
    """DELETE /api/plugins/{plugin_name}/settings/{key} — 删除单个配置项。"""
    plugin_name = str(request.match_info.get("plugin_name", "")).strip()
    key = str(request.match_info.get("key", "")).strip()

    if not plugin_name:
        return _json_response({"error": "缺少 plugin_name"}, 400)
    if not key:
        return _json_response({"error": "缺少 key"}, 400)

    config_manager = _get_config_manager(manager)
    config_manager.delete_setting(plugin_name, key)

    LOG.info("插件配置项已删除: %s.%s", plugin_name, key)
    return _json_response({"success": True, "plugin": plugin_name, "key": key})


# ═══════════════════════════════════════════════════════════════════════
# API: 刷新插件（重新加载）
# ═══════════════════════════════════════════════════════════════════════


@handle_api_errors
async def api_plugins_reload(request: web.Request, manager: Any) -> web.Response:
    """POST /api/plugins/reload — 刷新插件列表和配置（热重载）。"""
    plugins_dir = _plugins_dir(manager)
    if not plugins_dir.exists():
        return _json_response({"plugins": []})

    # 清除缓存，强制重新加载
    _invalidate_plugin_cache(plugins_dir)

    loader = PluginLoader(plugins_dir)
    definitions = loader.load_all_definitions()

    # 更新缓存
    _plugin_definitions_cache[str(plugins_dir)] = (time.monotonic(), definitions)

    config_manager = _get_config_manager(manager)
    config_manager.reload()

    count = len(definitions)
    enabled_count = sum(1 for d in definitions if config_manager.get_enabled(d.name))
    LOG.info("插件刷新完成: %d 个 (启用 %d)", count, enabled_count)

    return _json_response(
        {
            "success": True,
            "total": count,
            "enabled": enabled_count,
        }
    )
