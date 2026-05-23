"""Plugin 基类 —— 所有 Plugin 必须继承此类。

PluginBase 定义了 Plugin 的生命周期方法和必需接口：
    - on_load(): 加载时调用一次（可选覆写）
    - on_unload(): 卸载时调用一次（可选覆写）
    - execute(cmd): 核心执行方法（可覆写，或使用 @command 装饰器替代）

子类通过 self.ctx 访问 PluginContext。

v1.2+: 支持 @command 装饰器声明式指令注册
    使用 @command 装饰的方法，框架自动按 CommandAST.command 路由，
    并根据方法的类型注解自动进行参数校验与注入。
    不需要覆写 execute()。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sirius_pulse.plugins.models import CommandAST, PluginResponse
    from sirius_pulse.plugins.context import PluginContext

logger = logging.getLogger(__name__)


class PluginBase:
    """Plugin 基类。

    所有 Plugin 必须继承此类。支持两种指令定义方式：

    1. 【传统方式】覆写 execute() 方法
    2. 【装饰器方式】使用 @command 装饰器（推荐）

    类属性（在子类上覆写以声明元数据）：
        _plugin_name: str           — 内部标识名（必需）
        _plugin_display_name: str   — 显示名称
        _plugin_description: str    — 描述
        _plugin_version: str        — 版本号
        _plugin_author: str         — 作者
        _plugin_events: list[dict]  — 事件触发器定义
        _plugin_schedule: list[dict] — 声明式定时（自动转为 _plugin_events），格式 [{"time": "HH:MM", "duration": 1440}, ...]
        _plugin_permissions: dict   — 权限配置
        _plugin_nl_examples: list[str] — 自然语言触发示例
        _plugin_nl_slots: dict      — 自然语言槽位定义
        _plugin_dependencies: list[str] — pip 依赖
    """

    # ── 类级别配置（子类覆写） ──
    _plugin_name: str = ""
    _plugin_display_name: str = ""
    _plugin_description: str = ""
    _plugin_version: str = "1.0.0"
    _plugin_author: str = ""
    _plugin_events: list[dict[str, Any]] = []
    _plugin_schedule: list[dict[str, Any]] = []  # [{"time": "HH:MM", "duration": 1440}, ...] 声明式定时，由 from_class() 自动转为 PluginEventDef
    _plugin_permissions: dict[str, Any] | None = None
    _plugin_nl_examples: list[str] = []
    _plugin_nl_slots: dict[str, dict[str, Any]] = {}
    _plugin_dependencies: list[str] = []
    _plugin_prompt_inject: str = ""  # 注入到人格 prompt 的额外提示词（v1.3+）

    def __init__(self) -> None:
        self._ctx: "PluginContext | None" = None
        self._name: str = ""
        self._source_path: Path | None = None

        # @command 装饰器发现的 handler 字典（延迟初始化）
        self._command_handlers: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        """Plugin 名称。"""
        return self._name

    @property
    def ctx(self) -> "PluginContext":
        """Plugin 执行上下文。

        在 execute() 被调用前由框架注入，包含 engine、adapter、message 等。
        """
        if self._ctx is None:
            raise RuntimeError("PluginContext 尚未注入，请确保 Plugin 已正确初始化")
        return self._ctx

    @property
    def source_path(self) -> Path | None:
        """Plugin 源代码路径。"""
        return self._source_path

    # ── 生命周期方法 ──

    def on_load(self) -> None:
        """Plugin 加载时调用一次。

        可在此初始化资源、建立连接等。默认无操作。
        """

    def on_unload(self) -> None:
        """Plugin 卸载时调用一次。

        可在此释放资源、关闭连接等。默认无操作。
        """

    # ── 核心方法 ──

    def execute(self, cmd: "CommandAST") -> "PluginResponse":
        """执行 Plugin 核心逻辑（同步入口）。

        默认行为：
            1. 如果子类使用 @command 装饰器注册了指令，按 cmd.command 路由调度
            2. 否则返回"未实现"错误

        子类可以覆写此方法以自定义逻辑，但会覆盖自动调度功能。

        Args:
            cmd: 从用户输入解析的命令 AST

        Returns:
            PluginResponse 实例
        """
        from sirius_pulse.plugins.models import PluginResponse

        # 检查是否有 @command 已发现
        if self._command_handlers is None:
            self._discover_commands()
        if self._command_handlers:
            # 有装饰器命令但 execute 未被覆写 → 返回未调度错误
            # （装饰器命令应通过 execute_async 异步调度）
            return PluginResponse.fail(
                f"Plugin '{self._name}' 使用了 @command 装饰器但未通过异步调度。"
                f" 请使用 execute_async() 方法。"
            )

        return PluginResponse.fail(f"Plugin '{self._name}' 未实现 execute() 方法")

    async def execute_async(self, cmd: "CommandAST") -> list["PluginResponse"]:
        """执行 Plugin 核心逻辑（异步入口，v1.2+）。

        框架优先调用此方法。默认行为：
            1. 如果子类使用 @command 装饰器注册了指令，进行异步调度
            2. 否则通过 asyncio.to_thread 执行同步 execute()

        子类可覆写此方法以自定义异步逻辑。

        Args:
            cmd: 从用户输入解析的命令 AST

        Returns:
            list[PluginResponse]（至少一个元素）：
            - @command 模式：调度结果（单个/流式多个）
            - 传统模式：单元素列表
        """
        from sirius_pulse.plugins.models import PluginResponse

        # 检查是否有 @command
        if self._command_handlers is None:
            self._discover_commands()
        if self._command_handlers:
            return await self._dispatch_decorated_command(cmd)

        # 无装饰器命令 → 执行传统同步 execute()
        result = await asyncio.to_thread(self.execute, cmd)
        return [result]

    def get_command_metas(self) -> dict[str, Any]:
        """获取所有 @command 装饰器注册的指令元数据。

        用于 PluginLoader / Registry 自动构建 PluginDefinition.commands。

        Returns:
            {command_name: PluginCommandMeta} 字典
        """
        if self._command_handlers is None:
            self._discover_commands()
        return dict(self._command_handlers or {})

    # ── 内部方法 ──

    def _discover_commands(self) -> None:
        """延迟扫描所有 @command 装饰的方法，构建 handler 映射。

        在首次 execute() / execute_async() 调用时自动触发。
        """
        from sirius_pulse.plugins.decorators import discover_commands

        self._command_handlers = discover_commands(self)
        if self._command_handlers:
            logger.debug(
                "Plugin '%s' 发现 %d 个 @command: %s",
                self._name,
                len(self._command_handlers),
                ", ".join(self._command_handlers.keys()),
            )

    async def _dispatch_decorated_command(self, cmd: "CommandAST") -> list["PluginResponse"]:
        """将 CommandAST 路由到对应的 @command 方法并调用。

        Args:
            cmd: 用户命令 AST

        Returns:
            list[PluginResponse]（支持流式多输出）
        """
        from sirius_pulse.plugins.decorators import dispatch_command_stream

        return await dispatch_command_stream(self, cmd, self._command_handlers or {})

    # ── 辅助方法 ──

    @property
    def logger(self) -> logging.Logger:
        """Plugin 专用 logger。"""
        return self.ctx.logger if self._ctx else logger

    def get_data_store(self):
        """获取 Plugin 独立数据存储。"""
        if self._ctx and self._ctx.data_store:
            return self._ctx.data_store
        raise RuntimeError("PluginDataStore 不可用")

    def get_adapter(self):
        """获取平台适配器实例（BaseAdapter）。"""
        return self.ctx.adapter

    def render_template(self, template_name: str, data: dict[str, Any]) -> str:
        """渲染 Plugin 目录下的模板文件。

        Args:
            template_name: 模板文件名（相对于 templates/ 目录）
            data: 模板变量

        Returns:
            渲染后的字符串
        """
        if not self._source_path:
            return f"[模板不可用: {template_name}]"

        template_path = self._source_path / "templates" / template_name
        if not template_path.exists():
            logger.warning("模板不存在: %s", template_path)
            return f"[模板不存在: {template_name}]"

        try:
            template_text = template_path.read_text(encoding="utf-8")
            for key, value in data.items():
                template_text = template_text.replace(f"{{{key}}}", str(value))
            return template_text
        except Exception as exc:
            logger.error("渲染模板失败: %s", exc)
            return f"[模板渲染失败: {template_name}]"

    def _setup(self, name: str, ctx: "PluginContext") -> None:
        """由框架调用：设置 Plugin 名称和上下文。"""
        self._name = name
        self._ctx = ctx

    def _set_source_path(self, path: Path) -> None:
        """由框架调用：设置源代码路径。"""
        self._source_path = path
