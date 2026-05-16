"""Plugin 执行器 —— 权限校验、参数校验、调用 Plugin 生命周期。

职责：
    1. 校验调用者权限（群组白名单/黑名单、用户白名单、速率限制）
    2. 将 CommandAST 传递给 PluginBase.execute_async()
    3. 捕获异常并生成错误 PluginResponse
    4. 管理 Plugin 生命周期（on_load / on_unload）
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sirius_chat.plugins.models import CommandAST, PluginDefinition, PluginResponse
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
        engine: Any = None,
        adapter: Any = None,
    ) -> None:
        self._registry = registry
        self._persona_data_path = persona_data_path or Path(".")
        self._default_timeout = default_execution_timeout
        self._engine = engine
        self._adapter = adapter
        # 速率限制状态：{plugin_name: {minute_calls: [(timestamp, ...)], hour_calls: [(timestamp, ...)]}}
        self._rate_state: dict[str, dict[str, Any]] = {}

    def set_adapter(self, adapter: Any) -> None:
        """运行时注入平台 adapter（在 NapCat 连接后调用）。"""
        self._adapter = adapter

    # ── Plugin 生命周期 ──

    async def instantiate(self, definition: "PluginDefinition") -> object | None:
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
        config = {
            "plugin_path": str(definition.source_path) if definition.source_path else "",
            "render_mode": definition.render.mode,
        }
        # 注入用户在 WebUI 中配置的自定义 settings
        user_settings = getattr(definition, "user_settings", None)
        if isinstance(user_settings, dict):
            config.update(user_settings)
        ctx = PluginContext.create(
            plugin_name=definition.name,
            data_store=data_store,
            config=config,
            engine=self._engine,
            adapter=self._adapter,
        )
        instance._setup(definition.name, ctx)
        if definition.source_path:
            instance._set_source_path(definition.source_path)

        # 调用 on_load（支持同步和异步）
        try:
            on_load = instance.on_load
            if asyncio.iscoroutinefunction(on_load):
                await on_load()
            else:
                on_load()
        except Exception as exc:
            logger.warning("Plugin %s on_load 失败: %s", definition.name, exc)

        # 同步 @command 装饰器元数据到注册表索引
        try:
            command_metas = instance.get_command_metas()
            if command_metas:
                self._registry.sync_command_metas(definition.name, command_metas)
        except Exception as exc:
            logger.debug("同步 @command 元数据失败: %s", exc)

        logger.info("实例化 Plugin: %s", definition.name)
        return instance

    async def instantiate_all(self) -> int:
        """实例化所有已注册的 Plugin。

        Returns:
            成功实例化的 Plugin 数量
        """
        count = 0
        for definition in self._registry.get_all_definitions():
            instance = await self.instantiate(definition)
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
    ) -> list["PluginResponse"]:
        """执行 Plugin 的核心逻辑。

        支持两种模式：
            - @command 模式：返回 list[PluginResponse]（单个或流式多个）
            - 传统 execute() 模式：返回单元素 list[PluginResponse]

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
            list[PluginResponse]（至少一个元素）
        """
        from sirius_chat.plugins.models import PluginResponse

        definition = self._registry.get(plugin_name)
        if definition is None:
            return [PluginResponse.fail(f"Plugin 未找到: {plugin_name}")]

        # ── 权限校验 ──
        perm_error = self._check_permissions(
            definition, group_id=group_id, user_id=user_id,
            caller_is_developer=caller_is_developer,
        )
        if perm_error:
            logger.warning("Plugin %s 权限校验失败: %s", plugin_name, perm_error)
            # 权限校验失败时静默处理，不向用户发送错误消息
            # 无论是群聊还是私聊，都不产生任何输出，仅记录日志
            return [PluginResponse(success=False, render_mode="silent")]

        # ── 速率限制校验 ──
        rate_error = self._check_rate_limit(plugin_name, definition)
        if rate_error:
            logger.warning("Plugin %s 速率限制: %s", plugin_name, rate_error)
            return [PluginResponse.fail(rate_error)]

        # ── 获取或创建实例 ──
        instance = self._registry.get_instance(plugin_name)
        if instance is None:
            instance = await self.instantiate(definition)
            if instance is not None:
                self._registry.set_instance(plugin_name, instance)
        if instance is None:
            return [PluginResponse.fail(f"Plugin {plugin_name} 实例化失败")]

        # ── 注入运行时上下文 ──
        if hasattr(instance, '_ctx') and instance._ctx is not None:
            if engine is not None:
                instance._ctx.engine._bind(engine, plugin_name)
            if adapter is not None:
                instance._ctx.adapter = adapter  # 直接赋值 BaseAdapter 实例
            if message_context is not None:
                instance._ctx.message = message_context

        # ── 执行（带超时保护，支持命令级超时覆盖）──
        # 从 @command 装饰器元数据读取命令级 timeout，未设置则使用默认值
        cmd_timeout = self._default_timeout
        if hasattr(instance, 'get_command_metas'):
            cmd_metas = instance.get_command_metas()
            cmd_meta = cmd_metas.get(cmd.command) if isinstance(cmd_metas, dict) else None
            if cmd_meta is not None and hasattr(cmd_meta, 'timeout'):
                meta_timeout = getattr(cmd_meta, 'timeout', 0.0)
                if meta_timeout > 0:
                    cmd_timeout = meta_timeout

        try:
            # execute_async 总是返回 list[PluginResponse]，此处无需额外类型检查
            if hasattr(instance, 'execute_async'):
                results = await asyncio.wait_for(
                    instance.execute_async(cmd),
                    timeout=cmd_timeout,
                )
            else:
                raw = await asyncio.wait_for(
                    asyncio.to_thread(instance.execute, cmd),
                    timeout=cmd_timeout,
                )
                results = [raw] if raw is not None else [PluginResponse.ok(text="", data=None)]

            return results
        except asyncio.TimeoutError:
            logger.error("Plugin %s 执行超时 (%.1fs)", plugin_name, cmd_timeout)
            return [PluginResponse.fail(f"Plugin 执行超时（{cmd_timeout}秒）")]
        except Exception as exc:
            logger.error("Plugin %s 执行异常: %s", plugin_name, exc, exc_info=True)
            return [PluginResponse.fail(f"Plugin 执行异常: {exc}")]

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

        # Layer 3: 群组黑名单（插件可在所有引擎活跃群使用，黑名单遮蔽特定群）
        if group_id:
            if perms.group_blacklist and group_id in perms.group_blacklist:
                return "此插件在当前群被禁用"

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

    async def unload(self, plugin_name: str) -> None:
        """卸载指定 Plugin。"""
        instance = self._registry.get_instance(plugin_name)
        if instance is not None and hasattr(instance, 'on_unload'):
            try:
                on_unload = instance.on_unload
                if asyncio.iscoroutinefunction(on_unload):
                    await on_unload()
                else:
                    on_unload()
            except Exception as exc:
                logger.warning("Plugin %s on_unload 失败: %s", plugin_name, exc)
        self._registry.unregister(plugin_name)

    async def unload_all(self) -> None:
        """卸载所有 Plugin。"""
        for name in list(self._registry.plugin_names):
            await self.unload(name)
