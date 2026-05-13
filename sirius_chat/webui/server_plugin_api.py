"""WebUI Plugin 管理 API — 全局 Plugin 查阅、启停控制与源码编辑。

Plugin 目录位于项目根 plugins/（与 data/ 同级），
启用/禁用状态持久化到 plugins/_enabled.json。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_chat.plugins.loader import PluginLoader
from sirius_chat.plugins.models import PluginDefinition

LOG = logging.getLogger("sirius.webui")


def _json_response(data: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(
        data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, indent=2)
    )


def _plugins_dir(manager: Any) -> Path:
    """获取项目根 plugins/ 目录。"""
    return Path(manager.data_path).parent / "plugins"


def _enabled_config_path(manager: Any) -> Path:
    """获取 _enabled.json 路径。"""
    return _plugins_dir(manager) / "_enabled.json"


def _load_enabled_config(manager: Any) -> dict[str, bool]:
    """加载启用/禁用配置。"""
    path = _enabled_config_path(manager)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_enabled_config(manager: Any, config: dict[str, bool]) -> None:
    """保存启用/禁用配置。"""
    path = _enabled_config_path(manager)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ═══════════════════════════════════════════════════════════════════════
# API: 插件列表
# ═══════════════════════════════════════════════════════════════════════

async def api_plugins_get(request: web.Request, manager: Any) -> web.Response:
    """GET /api/plugins — 列出所有插件及其元数据。"""
    plugins_dir = _plugins_dir(manager)
    if not plugins_dir.exists():
        return _json_response({"plugins": []})

    try:
        loader = PluginLoader(plugins_dir)
        definitions = loader.load_all_definitions()
        enabled_config = _load_enabled_config(manager)

        plugins: list[dict[str, Any]] = []
        for d in definitions:
            is_enabled = enabled_config.get(d.name, True)
            source_file = _find_source_file(d.source_path) if d.source_path else None

            plugins.append({
                "name": d.name,
                "display_name": d.display_name or d.name,
                "description": d.description,
                "version": d.version,
                "author": d.author,
                "enabled": is_enabled,
                "commands": [
                    {
                        "name": c.name,
                        "patterns": c.patterns,
                        "pattern_type": c.pattern_type,
                        "description": c.description,
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
            })

        return _json_response({"plugins": plugins})
    except Exception as exc:
        LOG.warning("读取插件列表失败: %s", exc)
        return _json_response({"error": str(exc)}, 500)


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
    loader = PluginLoader(plugins_dir)

    definitions = loader.load_all_definitions()
    definition = next((d for d in definitions if d.name == plugin_name), None)
    if definition is None:
        return _json_response({"error": f"插件 {plugin_name} 不存在"}, 404)

    enabled_config = _load_enabled_config(manager)
    source_file = _find_source_file(definition.source_path)
    source_content = ""

    if source_file and definition.source_path:
        source_path = definition.source_path / source_file
        if source_path.exists():
            try:
                source_content = source_path.read_text(encoding="utf-8")
            except Exception as exc:
                LOG.warning("读取源码失败 %s: %s", source_path, exc)

    return _json_response({
        "name": definition.name,
        "display_name": definition.display_name or definition.name,
        "description": definition.description,
        "version": definition.version,
        "author": definition.author,
        "enabled": enabled_config.get(definition.name, True),
        "commands": [
            {
                "name": c.name,
                "patterns": c.patterns,
                "pattern_type": c.pattern_type,
                "description": c.description,
                "examples": c.examples,
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
            }
            for p in definition.parameters
        ],
        "nl_examples": definition.natural_language.examples if definition.natural_language else [],
        "nl_slots": definition.natural_language.slots if definition.natural_language else {},
        "source_file": source_file,
        "source_content": source_content,
    })


# ═══════════════════════════════════════════════════════════════════════
# API: 启用/禁用
# ═══════════════════════════════════════════════════════════════════════

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

    try:
        config = _load_enabled_config(manager)
        config[plugin_name] = enabled
        _save_enabled_config(manager, config)
        LOG.info("插件 %s enabled=%s", plugin_name, enabled)
        return _json_response({"success": True, "plugin": plugin_name, "enabled": enabled})
    except Exception as exc:
        LOG.warning("切换插件状态失败 %s: %s", plugin_name, exc)
        return _json_response({"error": str(exc)}, 500)


# ═══════════════════════════════════════════════════════════════════════
# API: 保存源码
# ═══════════════════════════════════════════════════════════════════════

async def api_plugin_source_save(request: web.Request, manager: Any) -> web.Response:
    """PUT /api/plugins/{plugin_name}/source — 保存插件源码。"""
    plugin_name = str(request.match_info.get("plugin_name", "")).strip()
    if not plugin_name:
        return _json_response({"error": "缺少 plugin_name"}, 400)

    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    source_content = body.get("source_content", "")
    if not source_content:
        return _json_response({"error": "source_content 不能为空"}, 400)

    plugins_dir = _plugins_dir(manager)
    plugin_dir = plugins_dir / plugin_name
    if not plugin_dir.exists():
        return _json_response({"error": f"插件目录 {plugin_name} 不存在"}, 404)

    source_file = _find_source_file(plugin_dir)
    if source_file is None:
        return _json_response({"error": "未找到可编辑的源文件"}, 404)

    source_path = plugin_dir / source_file
    try:
        source_path.write_text(source_content, encoding="utf-8")
        LOG.info("插件源码已保存: %s/%s", plugin_name, source_file)
        return _json_response({"success": True, "plugin": plugin_name, "source_file": source_file})
    except Exception as exc:
        LOG.error("保存源码失败 %s: %s", source_path, exc)
        return _json_response({"error": str(exc)}, 500)


# ═══════════════════════════════════════════════════════════════════════
# API: 刷新插件（重新加载）
# ═══════════════════════════════════════════════════════════════════════

async def api_plugins_reload(request: web.Request, manager: Any) -> web.Response:
    """POST /api/plugins/reload — 刷新插件列表（重新扫描目录）。"""
    plugins_dir = _plugins_dir(manager)
    if not plugins_dir.exists():
        return _json_response({"plugins": []})

    try:
        loader = PluginLoader(plugins_dir)
        definitions = loader.load_all_definitions()
        enabled_config = _load_enabled_config(manager)

        count = len(definitions)
        enabled_count = sum(1 for d in definitions if enabled_config.get(d.name, True))
        LOG.info("插件刷新完成: %d 个 (启用 %d)", count, enabled_count)

        return _json_response({
            "success": True,
            "total": count,
            "enabled": enabled_count,
        })
    except Exception as exc:
        LOG.warning("刷新插件失败: %s", exc)
        return _json_response({"error": str(exc)}, 500)
