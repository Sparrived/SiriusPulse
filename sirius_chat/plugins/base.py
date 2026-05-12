"""Plugin 基类 —— 所有 Plugin 必须继承此类。

PluginBase 定义了 Plugin 的生命周期方法和必需接口：
    - on_load(): 加载时调用一次（可选覆写）
    - on_unload(): 卸载时调用一次（可选覆写）
    - execute(cmd): 核心执行方法（必须覆写）

子类通过 self.ctx 访问 PluginContext。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sirius_chat.plugins.models import CommandAST, PluginResult
    from sirius_chat.plugins.context import PluginContext

logger = logging.getLogger(__name__)


class PluginBase:
    """Plugin 基类。

    所有 Plugin 必须继承此类并实现 execute() 方法。

    使用示例：

        class WeatherPlugin(PluginBase):
            def execute(self, cmd):
                city = cmd.kwargs.get("city", ArgNode("北京", "北京", "str"))
                # 业务逻辑...
                return PluginResult.ok(text=f"{city}天气: 晴 25°C")
    """

    def __init__(self) -> None:
        self._ctx: "PluginContext | None" = None
        self._name: str = ""
        self._source_path: Path | None = None

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

    def execute(self, cmd: "CommandAST") -> "PluginResult":
        """执行 Plugin 核心逻辑。

        Args:
            cmd: 从用户输入解析的命令 AST

        Returns:
            PluginResult 实例

        必须由子类覆写。默认实现返回失败结果。
        """
        from sirius_chat.plugins.models import PluginResult

        return PluginResult.fail(f"Plugin '{self._name}' 未实现 execute() 方法")

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
        """获取平台适配器代理。"""
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
            # 简单的 {key} 替换
            for key, value in data.items():
                template_text = template_text.replace(f"{{{key}}}", str(value))
            return template_text
        except Exception as exc:
            logger.error("渲染模板失败: %s", exc)
            return f"[模板渲染失败: {template_name}]"

    # ── 内部方法（框架调用） ──

    def _setup(self, name: str, ctx: "PluginContext") -> None:
        """由框架调用：设置 Plugin 名称和上下文。"""
        self._name = name
        self._ctx = ctx

    def _set_source_path(self, path: Path) -> None:
        """由框架调用：设置源代码路径。"""
        self._source_path = path
