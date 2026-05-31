"""Plugin 配置管理模块 —— 统一管理插件配置、支持热重载与 WebUI 配置。

核心功能：
    1. 统一的配置存储结构（plugins/_config.json）
    2. 配置变更监听与热重载
    3. WebUI 配置 API 支持
    4. 插件运行时配置访问接口

配置结构：
    {
        "plugin_name": {
            "enabled": true,              // 是否启用
            "permissions": {              // 权限配置
                "group_blacklist": [],
                "developer_only": false,
                ...
            },
            "settings": {                 // 插件自定义配置（如 chat_analyzer 的时间配置）
                "schedule": [...],
                ...
            }
        }
    }
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, TypedDict

logger = logging.getLogger(__name__)


class PluginConfig(TypedDict):
    """单个插件的配置结构。"""
    enabled: bool
    permissions: dict[str, Any]
    settings: dict[str, Any]


class ConfigChangeListener:
    """配置变更监听器。"""
    
    def __init__(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        self.callback = callback
        self._active = True
    
    def notify(self, plugin_name: str, config: dict[str, Any]) -> None:
        """通知配置变更。"""
        if self._active:
            try:
                self.callback(plugin_name, config)
            except Exception as exc:
                logger.warning("配置变更回调失败: %s", exc)
    
    def stop(self) -> None:
        """停止监听。"""
        self._active = False


class PluginConfigManager:
    """插件配置管理器。
    
    负责：
        1. 加载/保存配置到 JSON 文件
        2. 提供配置的增删改查接口
        3. 管理配置变更监听器
        4. 支持配置热重载
    """
    
    def __init__(self, plugins_dir: Path) -> None:
        self._plugins_dir = plugins_dir
        self._config_path = plugins_dir / "_config.json"
        self._config: dict[str, PluginConfig] = {}
        self._listeners: list[ConfigChangeListener] = []
        self._load()
    
    def _load(self) -> None:
        """从磁盘加载配置。"""
        if self._config_path.exists():
            try:
                raw = self._config_path.read_text(encoding="utf-8")
                data = json.loads(raw)
                # 迁移旧格式
                self._config = self._migrate_config(data)
                logger.info("加载插件配置: %d 个插件", len(self._config))
            except Exception as exc:
                logger.warning("加载配置失败，使用默认配置: %s", exc)
                self._config = {}
        else:
            self._config = {}
    
    def _migrate_config(self, data: dict[str, Any]) -> dict[str, PluginConfig]:
        """迁移旧格式配置到新格式。
        
        旧格式：{plugin_name: {group_blacklist: [], ...}}
        新格式：{plugin_name: {enabled: bool, permissions: {...}, settings: {...}}}
        """
        result: dict[str, PluginConfig] = {}
        
        for plugin_name, plugin_data in data.items():
            # 检查是否为旧格式（没有 enabled/permissions/settings 结构）
            if "enabled" not in plugin_data or "permissions" not in plugin_data:
                # 旧格式迁移
                permissions: dict[str, Any] = {}
                settings: dict[str, Any] = {}
                
                # 权限相关字段移到 permissions
                perm_keys = {"group_blacklist", "developer_only", "rate_limit_calls_per_minute"}
                for key in list(plugin_data.keys()):
                    if key in perm_keys:
                        permissions[key] = plugin_data.pop(key)
                    elif key == "enabled":
                        continue  # 单独处理
                    else:
                        settings[key] = plugin_data[key]
                
                result[plugin_name] = PluginConfig(
                    enabled=plugin_data.get("enabled", True),
                    permissions=permissions,
                    settings=settings
                )
            else:
                result[plugin_name] = PluginConfig(
                    enabled=plugin_data.get("enabled", True),
                    permissions=plugin_data.get("permissions", {}),
                    settings=plugin_data.get("settings", {})
                )
        
        return result
    
    def _save(self) -> None:
        """保存配置到磁盘。"""
        from sirius_pulse.config.file_io import atomic_json_save

        atomic_json_save(self._config_path, self._config)
        logger.debug("配置已保存")
    
    def _notify_change(self, plugin_name: str) -> None:
        """通知所有监听器配置变更。"""
        config: dict[str, Any] = self._config.get(plugin_name, {})  # type: ignore[assignment]
        for listener in self._listeners:
            listener.notify(plugin_name, config)  # type: ignore[arg-type]
    
    # ── 配置访问接口 ──
    
    def get_config(self, plugin_name: str) -> PluginConfig:
        """获取插件配置。"""
        return self._config.get(plugin_name, {
            "enabled": True,
            "permissions": {},
            "settings": {}
        })
    
    def get_enabled(self, plugin_name: str) -> bool:
        """获取插件启用状态。"""
        return self._config.get(plugin_name, {}).get("enabled", True)  # type: ignore[call-overload]
    
    def get_permissions(self, plugin_name: str) -> dict[str, Any]:
        """获取插件权限配置。"""
        return self._config.get(plugin_name, {}).get("permissions", {})  # type: ignore[call-overload]
    
    def get_settings(self, plugin_name: str) -> dict[str, Any]:
        """获取插件自定义配置。"""
        return self._config.get(plugin_name, {}).get("settings", {})  # type: ignore[call-overload]
    
    def get_setting(self, plugin_name: str, key: str, default: Any = None) -> Any:
        """获取插件单个配置项。"""
        return self.get_settings(plugin_name).get(key, default)
    
    # ── 配置修改接口 ──
    
    def set_enabled(self, plugin_name: str, enabled: bool) -> None:
        """设置插件启用状态。"""
        if plugin_name not in self._config:
            self._config[plugin_name] = PluginConfig(
                enabled=True,
                permissions={},
                settings={}
            )
        self._config[plugin_name]["enabled"] = enabled
        self._save()
        self._notify_change(plugin_name)
    
    def update_permissions(self, plugin_name: str, permissions: dict[str, Any]) -> None:
        """更新插件权限配置。"""
        if plugin_name not in self._config:
            self._config[plugin_name] = PluginConfig(
                enabled=True,
                permissions={},
                settings={}
            )
        self._config[plugin_name]["permissions"].update(permissions)
        self._save()
        self._notify_change(plugin_name)
    
    def update_settings(self, plugin_name: str, settings: dict[str, Any]) -> None:
        """更新插件自定义配置。"""
        if plugin_name not in self._config:
            self._config[plugin_name] = PluginConfig(
                enabled=True,
                permissions={},
                settings={}
            )
        self._config[plugin_name]["settings"].update(settings)
        self._save()
        self._notify_change(plugin_name)
    
    def set_setting(self, plugin_name: str, key: str, value: Any) -> None:
        """设置插件单个配置项。"""
        if plugin_name not in self._config:
            self._config[plugin_name] = PluginConfig(
                enabled=True,
                permissions={},
                settings={}
            )
        self._config[plugin_name]["settings"][key] = value
        self._save()
        self._notify_change(plugin_name)
    
    def delete_setting(self, plugin_name: str, key: str) -> None:
        """删除插件配置项。"""
        if plugin_name in self._config:
            self._config[plugin_name]["settings"].pop(key, None)
            self._save()
            self._notify_change(plugin_name)
    
    def remove_plugin(self, plugin_name: str) -> None:
        """移除插件配置。"""
        if plugin_name in self._config:
            del self._config[plugin_name]
            self._save()
    
    # ── 监听器管理 ──
    
    def add_listener(self, callback: Callable[[str, dict[str, Any]], None]) -> ConfigChangeListener:
        """添加配置变更监听器。
        
        Args:
            callback: 回调函数，接收 (plugin_name, config) 参数
        
        Returns:
            ConfigChangeListener 实例，可用于停止监听
        """
        listener = ConfigChangeListener(callback)
        self._listeners.append(listener)
        return listener
    
    def remove_listener(self, listener: ConfigChangeListener) -> None:
        """移除配置变更监听器。"""
        listener.stop()
        self._listeners.remove(listener)
    
    # ── 批量操作 ──
    
    def get_all_plugins(self) -> list[str]:
        """获取所有已配置插件名称。"""
        return list(self._config.keys())
    
    def get_all_configs(self) -> dict[str, PluginConfig]:
        """获取所有插件配置。"""
        return dict(self._config)
    
    def reset(self) -> None:
        """重置所有配置。"""
        self._config = {}
        self._save()
    
    # ── 热重载 ──
    
    def reload(self) -> None:
        """重新加载配置文件（热重载）。"""
        old_config = dict(self._config)
        self._load()
        
        # 检测变更并通知
        for plugin_name in set(old_config.keys()) | set(self._config.keys()):
            old = old_config.get(plugin_name)
            new = self._config.get(plugin_name)
            if old != new:
                self._notify_change(plugin_name)


# ═══════════════════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════════════════

_global_config_manager: PluginConfigManager | None = None


def get_config_manager(plugins_dir: Path | None = None) -> PluginConfigManager:
    """获取全局配置管理器单例。
    
    Args:
        plugins_dir: 插件目录路径，首次调用时如未提供则自动推断
    
    Returns:
        PluginConfigManager 实例
    """
    global _global_config_manager
    
    if _global_config_manager is None:
        if plugins_dir is None:
            # 自动推断：本文件位于 sirius_pulse/plugins/config.py
            # 项目根目录 / plugins 即为插件目录
            plugins_dir = Path(__file__).resolve().parent.parent.parent / "plugins"
        _global_config_manager = PluginConfigManager(plugins_dir)
    
    return _global_config_manager


def set_config_manager(manager: PluginConfigManager) -> None:
    """设置全局配置管理器（用于测试或依赖注入）。"""
    global _global_config_manager
    _global_config_manager = manager
