"""Plugin 执行器 —— 权限校验、参数校验、调用 Plugin.execute()。

职责：
    1. 校验调用者权限（群组白名单/黑名单、用户白名单、速率限制）
    2. 将 CommandAST 传递给 PluginBase.execute()
    3. 捕获异常并生成错误 PluginResult
    4. 管理 Plugin 生命周期（on_load / on_unload）
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sirius_chat.plugins.models import CommandAST, PluginDefinition, PluginResult
    from sirius_chat.plugins.registry import PluginRegistry
    from sirius_chat.plugins.context import PluginContext

logger = logging.getLogger(__name__)


class PluginExecutor:
    """Plugin 执行器。

    管理 Plugin 实例的生命周期，在调用 execute() 前后进行权限和参数校验。
    """

    def __init__(
        self,
        registry: "PluginRegistry",
        *,
        persona_data_path: Path | None = None,
        default_execution_timeout: float = 30.0,
    ) -> None:
        self._registry = registry
        self._persona_data_path = persona_data_path or Path(".")
        self._default_timeout = default_execution_timeout
        # 速率限制状态：{plugin_name: {minute_calls: [(timestamp, ...)], hour_calls: [(timestamp, ...)]}}
        self._rate_state: dict[str, dict[str, Any]] = {}

    # ── Plugin 生命周期 ──

    def instantiate(self, definition: "PluginDefinition") -> object | None:
        """从 PluginDefinition 创建 PluginBase 实例。

        Args:
            definition: Plugin 定义

        Returns:
            PluginBase 实例或 None
        """
        from sirius_chat.plugins.context import MessageContext, PluginContext
        from sirius_chat.plugins import PluginDataStore  # noqa: F811

        plugin_class = definition._plugin_class
        if plugin_class is None:
            logger.error("Plugin %s 未加载 Python 类", definition.name)
            return None

        try:
            instance = plugin_class()
        except Exception as exc:
            logger.error("实例化 Plugin %s 失败: %s", definition.name, exc)
            return None

        # 创建上下文
        data_store = PluginDataStore(self._persona_data_path, definition.name) if self._persona_data_path else None
        ctx = PluginContext.create(
            plugin_name=definition.name,
            data_store=data_store,
            config={
                "plugin_path": str(definition.source_path) if definition.source_path else "",
                "render_mode": definition.render.mode,
            },
        )
        instance._setup(definition.name, ctx)
        if definition.source_path:
            instance._set_source_path(definition.source_path)

        # 调用 on_load
        try:
            instance.on_load()
        except Exception as exc:
            logger.warning("Plugin %s on_load 失败: %s", definition.name, exc)

        logger.info("实例化 Plugin: %s", definition.name)
        return instance

    def instantiate_all(self) -> int:
        """实例化所有已注册的 Plugin。

        Returns:
            成功实例化的 Plugin 数量
        """
        count = 0
        for definition in self._registry.get_all_definitions():
            instance = self.instantiate(definition)
            if instance is not None:
                self._registry.set_instance(definition.name, instance)
                count += 1
        return count

    # ── 执行 ──

    async def execute(
        self,
        plugin_name: str,
        cmd: "CommandAST",
        *,
        group_id: str = "",
        user_id: str = "",
        caller_is_developer: bool = False,
        adapter: Any = None,
        engine: Any = None,
        message_context: Any = None,
    ) -> "PluginResult":
        """执行 Plugin 的核心逻辑。

        Args:
            plugin_name: Plugin 名称
            cmd: 命令 AST
            group_id: 群号（用于权限校验）
            user_id: 用户 ID（用于权限校验）
            caller_is_developer: 调用者是否为开发者
            adapter: 平台适配器实例（用于注入 PluginContext）
            engine: 引擎实例（用于注入 PluginContext）
            message_context: 消息上下文（MessageContext）

        Returns:
            PluginResult
        """
        from sirius_chat.plugins.models import PluginResult

        definition = self._registry.get(plugin_name)
        if definition is None:
            return PluginResult.fail(f"Plugin 未找到: {plugin_name}")

        # ── 权限校验 ──
        perm_error = self._check_permissions(
            definition, group_id=group_id, user_id=user_id,
            caller_is_developer=caller_is_developer,
        )
        if perm_error:
            logger.warning("Plugin %s 权限校验失败: %s", plugin_name, perm_error)
            return PluginResult.fail(perm_error)

        # ── 速率限制校验 ──
        rate_error = self._check_rate_limit(plugin_name, definition)
        if rate_error:
            logger.warning("Plugin %s 速率限制: %s", plugin_name, rate_error)
            return PluginResult.fail(rate_error)

        # ── 获取或创建实例 ──
        instance = self._registry.get_instance(plugin_name)
        if instance is None:
            instance = self.instantiate(definition)
            if instance is not None:
                self._registry.set_instance(plugin_name, instance)
        if instance is None:
            return PluginResult.fail(f"Plugin {plugin_name} 实例化失败")

        # ── 注入运行时上下文 ──
        if hasattr(instance, '_ctx') and instance._ctx is not None:
            if engine is not None:
                instance._ctx.engine._bind(engine, plugin_name)
            if adapter is not None:
                instance._ctx.adapter._bind(adapter, plugin_name)
            if message_context is not None:
                instance._ctx.message = message_context

        # ── 执行（带超时保护） ──
        try:
            # v1.2+: 使用 async 执行路径
            # execute_async 内部会自动判断：有 @command → 调度；无 → to_thread(execute)
            if hasattr(instance, 'execute_async'):
                result = await asyncio.wait_for(
                    instance.execute_async(cmd),
                    timeout=self._default_timeout,
                )
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(instance.execute, cmd),
                    timeout=self._default_timeout,
                )
            if result is None:
                return PluginResult.ok(text="", data=None)
            return result
        except asyncio.TimeoutError:
            logger.error("Plugin %s 执行超时 (%.1fs)", plugin_name, self._default_timeout)
            return PluginResult.fail(f"Plugin 执行超时（{self._default_timeout}秒）")
        except Exception as exc:
            logger.error("Plugin %s 执行异常: %s", plugin_name, exc, exc_info=True)
            return PluginResult.fail(f"Plugin 执行异常: {exc}")

    # ── 权限校验 ──

    def _check_permissions(
        self,
        definition: "PluginDefinition",
        *,
        group_id: str,
        user_id: str,
        caller_is_developer: bool,
    ) -> str | None:
        """校验权限，返回错误描述或 None（通过）。"""
        perms = definition.permissions

        # Layer 1: 开发者限制
        if perms.developer_only and not caller_is_developer:
            return "此插件仅开发者可用"

        # Layer 2: 适配器类型（暂时跳过，后续实现）

        # Layer 3: 群组过滤
        if group_id:
            if perms.group_blacklist and group_id in perms.group_blacklist:
                return "此插件在当前群被禁用"
            if perms.group_whitelist and group_id not in perms.group_whitelist:
                return "此插件未授权在当前群使用"

        # Layer 4: 用户过滤
        if user_id and perms.user_whitelist and user_id not in perms.user_whitelist:
            return "此插件未授权给您使用"

        return None

    def _check_rate_limit(self, plugin_name: str, definition: "PluginDefinition") -> str | None:
        """检查速率限制，返回错误描述或 None（通过）。"""
        perms = definition.permissions
        now = time.time()

        if plugin_name not in self._rate_state:
            self._rate_state[plugin_name] = {
                "minute_calls": [],
                "hour_calls": [],
            }

        state = self._rate_state[plugin_name]
        minute_window = now - 60
        hour_window = now - 3600

        # 清理过期记录
        state["minute_calls"] = [t for t in state["minute_calls"] if t > minute_window]
        state["hour_calls"] = [t for t in state["hour_calls"] if t > hour_window]

        # 检查限制
        if len(state["minute_calls"]) >= perms.rate_limit_calls_per_minute:
            return f"调用过于频繁（每分钟最多 {perms.rate_limit_calls_per_minute} 次）"
        if len(state["hour_calls"]) >= perms.rate_limit_calls_per_hour:
            return f"调用过于频繁（每小时最多 {perms.rate_limit_calls_per_hour} 次）"

        # 记录本次调用
        state["minute_calls"].append(now)
        state["hour_calls"].append(now)
        return None

    # ── 卸载 ──

    def unload(self, plugin_name: str) -> None:
        """卸载指定 Plugin。"""
        instance = self._registry.get_instance(plugin_name)
        if instance is not None and hasattr(instance, 'on_unload'):
            try:
                instance.on_unload()
            except Exception as exc:
                logger.warning("Plugin %s on_unload 失败: %s", plugin_name, exc)
        self._registry.unregister(plugin_name)

    def unload_all(self) -> None:
        """卸载所有 Plugin。"""
        for name in list(self._registry.plugin_names):
            self.unload(name)
