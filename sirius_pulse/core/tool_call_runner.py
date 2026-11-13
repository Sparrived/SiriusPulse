"""单次工具调用的统一执行：一个 ``tool_call`` 怎样变成一条 tool 消息。

聊天回合（``bg_tasks_delayed``）与工作模式回合（``tool_engine_context``）都会让
模型调工具。两边的编排不同——聊天要过互动门槛、记 AgentTurn、把队列项标成不可
重放；工作模式要写轨迹、在执行前存重放边界——但下面这些只能有一份，否则两条路
迟早各改各的：

- 查工具、解析参数（解析失败按空参数处理）；
- 绑定本回合已接纳的指令来源（只认框架给的 evidence，不认模型自报）；
- 超时、按调用判断能否重试（同一个工具的不同 action 可能一个能重试一个不能）；
- 执行、异常兜底成失败结果；
- 小本写入后排期整理；
- 收集多模态结果。

用法：编排方先 :meth:`ToolCallRunner.prepare`，按自己的策略决定
:meth:`ToolCallRunner.reject` 还是 :meth:`ToolCallRunner.execute`，最后把
:meth:`ToolCallOutcome.tool_message` 追加进消息链。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from sirius_pulse.providers.base import ToolCall
from sirius_pulse.tools.executor import _bind_pending_source, _is_builtin_tool
from sirius_pulse.tools.models import ToolDefinition, ToolInvocationContext, ToolResult

logger = logging.getLogger(__name__)

NOTEBOOK_TOOL = "persona_notebook"

#: 同一工具里只读的 action：副作用与能否重试都按这次调用实际做的事判断。
_READ_ONLY_ACTIONS: dict[str, frozenset[str]] = {
    "interaction_with_master": frozenset({"status"}),
}

_DEFAULT_TOOL_TIMEOUT = 30.0
_RETRIES_WHEN_SAFE = 2


def _action(params: dict[str, Any] | None) -> str:
    return str((params or {}).get("action", "")).strip().lower()


def _is_read_only_call(tool: Any, params: dict[str, Any] | None) -> bool | None:
    """按 action 判断这次调用是否只读；工具没有按 action 区分时返回 None。"""
    actions = _READ_ONLY_ACTIONS.get(getattr(tool, "name", ""))
    if actions is None:
        return None
    return _action(params) in actions


def side_effect_name(tool: Any, params: dict[str, Any] | None = None) -> str:
    """这次调用的副作用分类（字符串），供去重与审计。"""
    if _is_read_only_call(tool, params):
        return "read_only"
    value = getattr(tool, "side_effect", "unknown")
    return str(getattr(value, "value", value) or "unknown")


def retry_safe(tool: Any, params: dict[str, Any] | None = None) -> bool:
    """这次调用失败后能否由执行器重试；会对外投递的 action 一律不重试。"""
    read_only = _is_read_only_call(tool, params)
    if read_only is not None:
        return read_only
    return bool(getattr(tool, "retry_safe", False))


def parse_tool_arguments(tool_name: str, arguments: str | None) -> dict[str, Any]:
    """把模型给的参数 JSON 解析成字典；坏 JSON 或非对象都按空参数处理。"""
    if not arguments:
        return {}
    try:
        params = json.loads(arguments)
    except json.JSONDecodeError:
        logger.warning(
            "tool_call 参数解析失败: %s, arguments=%s",
            tool_name,
            "<redacted>" if tool_name == NOTEBOOK_TOOL else arguments,
        )
        return {}
    return params if isinstance(params, dict) else {}


@dataclass(slots=True)
class PreparedToolCall:
    """查好工具、解析好参数、绑定好来源，还没执行的一次调用。"""

    call: ToolCall
    tool: ToolDefinition | None
    params: dict[str, Any]
    context: ToolInvocationContext | None
    source_error: str = ""

    @property
    def name(self) -> str:
        return self.call.function_name

    @property
    def side_effect(self) -> str:
        return side_effect_name(self.tool, self.params)

    @property
    def rejection(self) -> str:
        """不能执行的原因；可以执行时为空。"""
        if self.tool is None:
            return f"Tool '{self.name}' not found"
        if self.source_error:
            return f"Tool '{self.name}' 被拒绝：{self.source_error}"
        return ""


@dataclass(slots=True)
class ToolCallOutcome:
    """一次调用的结果：要回给模型的 tool 消息正文，以及执行结果（若真的执行了）。"""

    call: ToolCall
    content: str
    result: ToolResult | None = None
    multimodal: list[dict[str, Any]] = field(default_factory=list)

    @property
    def executed(self) -> bool:
        return self.result is not None

    @property
    def ok(self) -> bool:
        return self.result is not None and self.result.success

    def tool_message(self) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": self.call.id, "content": self.content}


class ToolCallRunner:
    """在一个引擎上执行模型发起的单次工具调用。"""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def timeout(self) -> float:
        try:
            value = float(self._engine.config.get("tool_execution_timeout", _DEFAULT_TOOL_TIMEOUT))
        except (TypeError, ValueError, AttributeError):
            return _DEFAULT_TOOL_TIMEOUT
        return max(0.0, value)

    def prepare(self, call: ToolCall, context: ToolInvocationContext | None) -> PreparedToolCall:
        """查工具、解析参数，并把调用者收窄到本回合已接纳的那条指令的作者。"""
        name = call.function_name
        registry = getattr(self._engine, "_tool_registry", None)
        tool = registry.get(name) if registry is not None else None
        params = parse_tool_arguments(name, call.function_arguments)
        if tool is None:
            return PreparedToolCall(call=call, tool=None, params=params, context=context)
        bound, source_error = _bind_pending_source(tool, params, context)
        return PreparedToolCall(
            call=call, tool=tool, params=params, context=bound, source_error=source_error
        )

    @staticmethod
    def reject(call: ToolCall, message: str) -> ToolCallOutcome:
        """不执行，只把拒绝原因回给模型。"""
        return ToolCallOutcome(call=call, content=message)

    async def execute(
        self,
        prepared: PreparedToolCall,
        *,
        group_id: str,
        before_execute: Callable[[], None] | None = None,
    ) -> ToolCallOutcome:
        """执行一次已准备好的调用；``before_execute`` 在真正动手前调用一次。"""
        if prepared.rejection:
            return self.reject(prepared.call, prepared.rejection)
        tool = prepared.tool
        assert tool is not None
        executor = getattr(self._engine, "_tool_executor", None)
        if executor is None:
            return self.reject(prepared.call, f"Tool '{prepared.name}' 暂时无法执行：工具执行器不可用")

        if before_execute is not None:
            before_execute()
        try:
            result = await executor.execute_async(
                tool,
                prepared.params,
                timeout=self.timeout(),
                invocation_context=prepared.context,
                max_retries=_RETRIES_WHEN_SAFE if retry_safe(tool, prepared.params) else 0,
            )
        except Exception as exc:
            logger.error("TOOL '%s' 执行异常: %s", prepared.name, exc)
            result = ToolResult(success=False, error=str(exc))

        if result.success:
            self._after_success(tool, result, group_id)
        else:
            logger.warning("TOOL '%s' 执行失败: %s", prepared.name, result.error or "Unknown error")
        return ToolCallOutcome(
            call=prepared.call,
            content=result.to_model_text(),
            result=result,
            multimodal=[
                {"type": "image_url", "image_url": {"url": block.value}}
                for block in result.multimodal_blocks
            ],
        )

    def _after_success(self, tool: ToolDefinition, result: ToolResult, group_id: str) -> None:
        """小本记下待整理的内容后，交给后台整理任务排期。"""
        if tool.name != NOTEBOOK_TOOL or not _is_builtin_tool(tool, NOTEBOOK_TOOL):
            return
        data = result.data if isinstance(result.data, dict) else {}
        status = data.get("status")
        if status not in {"pending", "maintenance_requested"}:
            return
        manager = getattr(self._engine, "_bg_tasks_mgr", None)
        maintenance = getattr(manager, "notebook_maintenance", None)
        if maintenance is not None:
            maintenance.schedule(group_id, force=status == "maintenance_requested")
