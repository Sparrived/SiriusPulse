"""工具系统本身：注册表、执行器、给提示词用的工具描述。

实现 :class:`sirius_pulse.tools.engine_context.ToolingAccess`。
"""

from __future__ import annotations

from typing import Any


class ToolingMixin:
    """工具系统本身：注册表、执行器、给提示词用的工具描述。"""

    _engine: Any

    @property
    def tool_registry(self) -> Any:
        return self._engine._tool_registry

    @property
    def tool_executor(self) -> Any:
        return self._engine._tool_executor

    def get_tool_descriptions(
        self,
        caller_is_developer: bool = False,
        adapter_type: str | None = None,
        *,
        work_mode: bool = False,
        exclude_names: tuple[str, ...] = (),
    ) -> str:
        """获取工具描述文本（用于被动工具的 prompt 注入）。"""
        if self._engine._tool_registry is None:
            return ""
        from sirius_pulse.memory.user.unified_models import UnifiedUser
        from sirius_pulse.tools.models import ToolInvocationContext

        caller = UnifiedUser(
            user_id="caller",
            name="caller",
            metadata={"is_developer": caller_is_developer},
        )
        ctx = ToolInvocationContext(caller=caller)
        tools = self._engine._tool_registry.build_tools_list(
            invocation_context=ctx,
            adapter_type=adapter_type or self._engine._current_adapter_type or None,
            exclude_names=set(exclude_names),
            work_mode=work_mode,
        )
        if not tools:
            return ""
        # 将 tools 格式转换为文本描述
        lines = []
        for tool in tools:
            func = tool.get("function", {})
            name = func.get("name", "")
            desc = func.get("description", "")
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)

    def _chat_scoped_tool_names(self) -> tuple[str, ...]:
        """没有可信聊天来源的回合要藏起来的工具（例如只读写某个聊天的小本）。"""
        registry = getattr(self._engine, "_tool_registry", None)
        if registry is None:
            return ()
        return registry.names_where(lambda tool: tool.chat_scoped)
