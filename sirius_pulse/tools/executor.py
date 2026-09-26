"""Tool executor — validates parameters and safely runs tools."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from pathlib import Path
from typing import Any

from sirius_pulse.tools.data_store import ToolDataStore
from sirius_pulse.tools.models import (
    ToolChainContext,
    ToolDefinition,
    ToolInvocationContext,
    ToolResult,
    ToolSideEffect,
    build_chat_context,
)
from sirius_pulse.tools.security import validate_tool_access
from sirius_pulse.tools.telemetry import ToolExecutionRecord, ToolTelemetry
from sirius_pulse.utils.layout import WorkspaceLayout

logger = logging.getLogger(__name__)


def _should_retry(exc: Exception) -> bool:
    """Heuristic: is this exception likely transient and worth retrying?"""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    exc_name = type(exc).__name__.lower()
    return any(
        keyword in exc_name
        for keyword in ("timeout", "connection", "temporary", "network", "retry", "unreachable")
    )


class ToolExecutor:
    """Execute tools with parameter validation, retry, telemetry, and data store injection."""

    def __init__(self, work_path: Path | WorkspaceLayout) -> None:
        self._layout = (
            work_path if isinstance(work_path, WorkspaceLayout) else WorkspaceLayout(work_path)
        )
        self._data_stores: dict[str, ToolDataStore] = {}
        self._telemetry = ToolTelemetry(self._layout.tool_data_dir() / ".telemetry.jsonl")
        self._bridges: dict[str, Any] = {}
        self._chat_context: dict[str, Any] = {}
        self._engine_context: Any | None = None

    def set_chat_context(
        self, group_id: str = "", user_id: str = "", adapter_type: str = ""
    ) -> None:
        """Set the fallback chat context for tools that lack an invocation context.

        Prefer the per-invocation :attr:`ToolInvocationContext.chat_context`:
        this setter writes a single shared dict, so a concurrent turn for another
        group can overwrite it before the tool runs.
        """
        self._chat_context = build_chat_context(
            group_id=group_id, user_id=user_id, adapter_type=adapter_type
        )

    def _chat_context_for(self, invocation_context: ToolInvocationContext | None) -> dict[str, Any]:
        """Resolve the chat context for one call, preferring the call's own identity."""
        if invocation_context is not None and invocation_context.group_id:
            return invocation_context.chat_context
        return self._chat_context or {}

    def set_bridge(self, adapter_type: str, bridge: Any) -> None:
        """Register a platform bridge for a given adapter type."""
        self._bridges[adapter_type] = bridge

    def set_engine_context(self, engine_context: Any) -> None:
        """Register the limited engine context injected into trusted built-in tools."""
        self._engine_context = engine_context

    def get_bridge_for_tool(self, tool: ToolDefinition) -> Any | None:
        """Return the best-matching bridge for a tool.

        If the tool declares adapter_types, return the first matching bridge.
        Otherwise return the first available bridge (or None).
        """
        if not self._bridges:
            return None
        if tool.adapter_types:
            for at in tool.adapter_types:
                if at in self._bridges:
                    return self._bridges[at]
            return None
        return next(iter(self._bridges.values()), None)

    def get_data_store(self, tool_name: str) -> ToolDataStore:
        """Get or create the persistent data store for a tool."""
        if tool_name not in self._data_stores:
            store_path = self._layout.tool_data_dir() / f"{tool_name}.json"
            self._data_stores[tool_name] = ToolDataStore(store_path)
        return self._data_stores[tool_name]

    # Backward-compatible alias
    _get_data_store = get_data_store

    def _prepare_call_params(
        self,
        tool: ToolDefinition,
        params: dict[str, Any],
        chain_context: ToolChainContext | None,
        invocation_context: ToolInvocationContext | None,
    ) -> tuple[dict[str, Any] | None, ToolResult | None]:
        """Apply the same validation contract to sync and async tools."""
        if tool._run_func is None:
            return None, ToolResult(success=False, error=f"TOOL '{tool.name}' 没有可执行的 run() 函数")

        if invocation_context is not None and getattr(invocation_context, "self_initiated", False):
            blocked = _self_initiated_block_reason(tool)
            if blocked:
                return None, ToolResult(success=False, error=blocked)

        if chain_context is not None:
            params = chain_context.resolve_templates(params)

        access_error = validate_tool_access(tool=tool, invocation_context=invocation_context)
        if access_error:
            return None, ToolResult(success=False, error=access_error)

        admin_error = self._validate_admin_requirement(tool, invocation_context)
        if admin_error:
            return None, ToolResult(success=False, error=admin_error)

        for param_def in tool.parameters:
            if param_def.required and param_def.name not in params:
                return None, ToolResult(success=False, error=f"缺少必填参数: {param_def.name}")

        call_params: dict[str, Any] = {}
        for param_def in tool.parameters:
            if param_def.name in params:
                call_params[param_def.name] = _coerce_type(params[param_def.name], param_def.type)
            elif param_def.default is not None:
                call_params[param_def.name] = param_def.default
        if tool.allow_extra_parameters:
            for name, value in params.items():
                call_params.setdefault(name, value)
        return call_params, None

    def _inject_runtime_params(
        self,
        tool: ToolDefinition,
        call_params: dict[str, Any],
        invocation_context: ToolInvocationContext | None,
    ) -> None:
        """Inject framework-owned inputs after model-owned parameters are validated."""
        assert tool._run_func is not None
        if not tool.inject_runtime_params:
            return
        data_store = self._get_data_store(tool.name)
        injection_plan = _build_injection_plan(tool._run_func)
        if injection_plan.accepts("data_store"):
            call_params["data_store"] = data_store
        if invocation_context is not None and injection_plan.accepts("invocation_context"):
            call_params["invocation_context"] = invocation_context
        bridge = self.get_bridge_for_tool(tool)
        if bridge is not None and injection_plan.accepts("bridge"):
            call_params["bridge"] = bridge
        if injection_plan.accepts("chat_context"):
            chat_context = self._chat_context_for(invocation_context)
            if chat_context:
                call_params["chat_context"] = dict(chat_context)
        if (
            self._engine_context is not None
            and _can_receive_engine_context(tool)
            and injection_plan.accepts("engine_context")
        ):
            call_params["engine_context"] = self._engine_context

    def execute(
        self,
        tool: ToolDefinition,
        params: dict[str, Any],
        chain_context: ToolChainContext | None = None,
        invocation_context: ToolInvocationContext | None = None,
        max_retries: int = 0,
    ) -> ToolResult:
        """Execute a tool synchronously with parameter validation and optional retry.

        If *chain_context* is provided, any ``${tool_name}`` / ``${tool_name.field}``
        placeholders in parameter values are resolved against previously executed
        tools' results before the tool is called.  After execution the result is
        stored back into *chain_context* under ``tool.name`` for downstream use.

        The data_store is automatically injected as a keyword argument
        if the tool's run() function accepts it.

        Args:
            max_retries: Number of extra attempts for transient failures
                (timeout, connection error, etc.).
        """
        start_time = time.perf_counter()
        tool_result: ToolResult | None = None
        logger.info(
            "Tool execute start: %s(params=%s, caller=%s)",
            tool.name,
            params,
            getattr(invocation_context, "caller", None) if invocation_context else None,
        )

        try:
            call_params, tool_result = self._prepare_call_params(
                tool, params, chain_context, invocation_context
            )
            if tool_result is not None:
                logger.warning("Tool execute rejected: %s -> %s", tool.name, tool_result.error)
                return tool_result
            assert call_params is not None
            self._inject_runtime_params(tool, call_params, invocation_context)
            data_store = self._get_data_store(tool.name)

            logger.info("Tool execute calling: %s(final_params=%s)", tool.name, call_params)

            # Run with optional retry for transient failures
            for attempt in range(max_retries + 1):
                try:
                    if inspect.iscoroutinefunction(tool._run_func):
                        # Synchronous execute() cannot await; raise so caller uses execute_async
                        raise RuntimeError(
                            f"TOOL '{tool.name}' is async and must be executed via execute_async"
                        )
                    result = tool._run_func(**call_params)
                    # Persist data store after execution
                    data_store.save()
                    tool_result = ToolResult.from_raw_result(result)
                    tool_result.success = True if tool_result.error == "" else tool_result.success
                    logger.info(
                        "Tool execute done: %s -> success=%s | summary=%r | text_blocks=%d | "
                        "multimodal_blocks=%d",
                        tool.name,
                        tool_result.success,
                        tool_result.to_display_text()[:200],
                        len(tool_result.text_blocks),
                        len(tool_result.multimodal_blocks),
                    )
                    break
                except Exception as exc:
                    if attempt < max_retries and _should_retry(exc):
                        logger.warning(
                            "TOOL '%s' 第%d次执行失败（将重试）: %s",
                            tool.name,
                            attempt + 1,
                            exc,
                        )
                        continue
                    logger.error("TOOL '%s' 执行异常: %s", tool.name, exc)
                    tool_result = ToolResult(success=False, error=str(exc))
                    break
        finally:
            # Telemetry is best-effort and must not affect the result
            if tool_result is not None:
                duration_ms = (time.perf_counter() - start_time) * 1000
                try:
                    caller_id = ""
                    if invocation_context is not None:
                        caller_id = getattr(invocation_context, "caller_user_id", "") or ""
                    summary = tool_result.to_display_text()[:500] if tool_result.success else ""
                    self._telemetry.record(
                        ToolExecutionRecord(
                            tool_name=tool.name,
                            timestamp=time.time(),
                            success=tool_result.success,
                            duration_ms=round(duration_ms, 2),
                            error=tool_result.error if not tool_result.success else "",
                            caller_user_id=caller_id,
                            params=params if params else None,
                            result_summary=summary,
                        )
                    )
                except Exception:
                    pass

        # Record into chain context so subsequent tools can reference this result
        if chain_context is not None and tool_result is not None:
            chain_context.store(tool.name, tool_result)

        return tool_result if tool_result is not None else ToolResult(success=False, error="未知错误")

    async def execute_async(
        self,
        tool: ToolDefinition,
        params: dict[str, Any],
        timeout: float = 0,
        chain_context: ToolChainContext | None = None,
        invocation_context: ToolInvocationContext | None = None,
        max_retries: int = 0,
    ) -> ToolResult:
        """Execute a tool in a thread pool to avoid blocking the event loop.

        Async tools (coroutine functions) are awaited directly in the event
        loop instead of being dispatched to a thread pool.

        Args:
            tool: The tool definition to execute.
            params: Parameters to pass to the tool.
            timeout: Max seconds to wait. 0 means no limit.
            chain_context: Optional chain context for template resolution and
                result accumulation across a multi-tool round.
            max_retries: Number of extra attempts for transient failures.
        """
        if inspect.iscoroutinefunction(tool._run_func):
            # Async tools run directly in the event loop so they can await
            # bridge/adapter I/O without thread-pool indirection.
            coro = self._execute_async_tool(
                tool, params, chain_context, invocation_context, max_retries
            )
            if timeout > 0:
                try:
                    return await asyncio.wait_for(coro, timeout=timeout)
                except asyncio.TimeoutError:
                    logger.error("TOOL '%s' 执行超时 (限制 %.1f秒)", tool.name, timeout)
                    return ToolResult(
                        success=False,
                        error=f"TOOL执行超时（限制 {timeout:.0f} 秒），请稍后重试或联系管理员",
                    )
            return await coro

        if timeout > 0:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        self.execute,
                        tool,
                        params,
                        chain_context,
                        invocation_context,
                        max_retries,
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.error("TOOL '%s' 执行超时 (限制 %.1f秒)", tool.name, timeout)
                return ToolResult(
                    success=False,
                    error=f"TOOL执行超时（限制 {timeout:.0f} 秒），请稍后重试或联系管理员",
                )
        return await asyncio.to_thread(
            self.execute,
            tool,
            params,
            chain_context,
            invocation_context,
            max_retries,
        )

    async def _execute_async_tool(
        self,
        tool: ToolDefinition,
        params: dict[str, Any],
        chain_context: ToolChainContext | None = None,
        invocation_context: ToolInvocationContext | None = None,
        max_retries: int = 0,
    ) -> ToolResult:
        """Execute an async tool directly in the event loop."""
        start_time = time.perf_counter()
        logger.info(
            "Tool async execute start: %s(params=%s, caller=%s)",
            tool.name,
            params,
            getattr(invocation_context, "caller", None) if invocation_context else None,
        )
        try:
            call_params, rejected = self._prepare_call_params(
                tool, params, chain_context, invocation_context
            )
            if rejected is not None:
                logger.warning("Tool async execute rejected: %s -> %s", tool.name, rejected.error)
                return rejected
            assert call_params is not None
            self._inject_runtime_params(tool, call_params, invocation_context)
            data_store = self._get_data_store(tool.name)

            logger.info("Tool async execute calling: %s(final_params=%s)", tool.name, call_params)

            tool_result: ToolResult | None = None
            for attempt in range(max_retries + 1):
                try:
                    result = await tool._run_func(**call_params)
                    data_store.save()
                    tool_result = ToolResult.from_raw_result(result)
                    tool_result.success = True if tool_result.error == "" else tool_result.success
                    logger.info(
                        "Tool async execute done: %s -> success=%s | summary=%r | text_blocks=%d | "
                        "multimodal_blocks=%d",
                        tool.name,
                        tool_result.success,
                        tool_result.to_display_text()[:200],
                        len(tool_result.text_blocks),
                        len(tool_result.multimodal_blocks),
                    )
                    break
                except Exception as exc:
                    if attempt < max_retries and _should_retry(exc):
                        logger.warning(
                            "TOOL '%s' 第%d次执行失败（将重试）: %s",
                            tool.name,
                            attempt + 1,
                            exc,
                        )
                        continue
                    logger.error("TOOL '%s' 执行异常: %s", tool.name, exc)
                    tool_result = ToolResult.from_raw_result(str(exc))
                    tool_result.success = False
                    tool_result.error = str(exc)
                    break

            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            if tool_result is not None:
                try:
                    caller_id = ""
                    if invocation_context is not None:
                        caller_id = (
                            getattr(invocation_context, "caller_user_id", "")
                            or getattr(invocation_context, "caller", "")
                            or ""
                        )
                    summary = tool_result.to_display_text()[:500] if tool_result.success else ""
                    self._telemetry.record(
                        ToolExecutionRecord(
                            tool_name=tool.name,
                            timestamp=time.time(),
                            success=tool_result.success,
                            duration_ms=elapsed_ms,
                            error=tool_result.error if not tool_result.success else "",
                            caller_user_id=caller_id,
                            params=params if params else None,
                            result_summary=summary,
                        )
                    )
                except Exception:
                    pass

            # Record into chain context so subsequent tools can reference this result
            if chain_context is not None and tool_result is not None:
                chain_context.store(tool.name, tool_result)

            return (
                tool_result if tool_result is not None else ToolResult(success=False, error="未知错误")
            )
        except Exception as exc:
            logger.error("Tool async execute exception: %s -> %s", tool.name, exc)
            return ToolResult(success=False, error=str(exc))

    def save_all_stores(self) -> None:
        """Persist all dirty data stores."""
        for store in self._data_stores.values():
            store.save()

    def _validate_admin_requirement(
        self,
        tool: ToolDefinition,
        invocation_context: ToolInvocationContext | None = None,
    ) -> str:
        """Refuse admin-required tools unless the Bot is admin *of this group*.

        The group comes from the invocation's own identity when available; using
        the shared ``_chat_context`` fallback would let a concurrent turn for
        another group decide which group gets checked.
        """
        if not tool.admin_required:
            return ""
        chat_context = self._chat_context_for(invocation_context)
        if chat_context.get("chat_type") != "group":
            return f"TOOL '{tool.name}' 只能在群聊中由管理员 Bot 执行"
        group_id = str(chat_context.get("chat_id") or chat_context.get("group_id") or "")
        checker = getattr(self._engine_context, "is_qq_bot_group_admin", None)
        if callable(checker) and checker(group_id):
            return ""
        return f"TOOL '{tool.name}' 需要 Bot 是当前群管理员"


def _coerce_type(value: Any, type_hint: str) -> Any:
    """Best-effort type coercion based on the parameter type hint."""
    type_lower = type_hint.lower().strip()
    if type_lower == "int":
        try:
            return int(value)
        except (ValueError, TypeError):
            return value
    if type_lower == "float":
        try:
            return float(value)
        except (ValueError, TypeError):
            return value
    if type_lower == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    if type_lower in ("list[str]", "list"):
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                return [v.strip() for v in value.split(",") if v.strip()]
        return value
    return value


def _self_initiated_block_reason(tool: ToolDefinition) -> str:
    """Refuse delivery-capable tools on a turn the persona started herself.

    Autonomy is about *making* something; whether to *tell* anyone is a separate
    decision made elsewhere.  Anything classified as an external write is
    therefore refused here, at the single choke point every tool call goes
    through, rather than trusting each tool to check its own chat context.  A
    Tool may opt out only when it records rather than delivers.
    """
    if getattr(tool, "allowed_when_self_initiated", False):
        return ""
    if tool.side_effect in {ToolSideEffect.EXTERNAL_WRITE, ToolSideEffect.DESTRUCTIVE}:
        return f"TOOL '{tool.name}' 会向外部发送内容；自主产出的内容先留作她自己的素材。"
    return ""


def _can_receive_engine_context(tool: ToolDefinition) -> bool:
    """Only package built-ins may receive the privileged engine context."""
    if tool.source_path is None:
        return False
    try:
        builtin_dir = (Path(__file__).resolve().parent / "builtin").resolve()
        return tool.source_path.resolve().is_relative_to(builtin_dir)
    except Exception:
        return False


class _InjectionPlan:
    def __init__(self, *, accepts_kwargs: bool, keyword_params: set[str]) -> None:
        self._accepts_kwargs = accepts_kwargs
        self._keyword_params = keyword_params

    def accepts(self, param_name: str) -> bool:
        return self._accepts_kwargs or param_name in self._keyword_params


def _build_injection_plan(run_func: Any) -> _InjectionPlan:
    try:
        signature = inspect.signature(run_func)
    except (TypeError, ValueError):
        return _InjectionPlan(accepts_kwargs=True, keyword_params=set())

    accepts_kwargs = False
    keyword_params: set[str] = set()
    for name, param in signature.parameters.items():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            accepts_kwargs = True
            continue
        if param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            keyword_params.add(name)
    return _InjectionPlan(accepts_kwargs=accepts_kwargs, keyword_params=keyword_params)
