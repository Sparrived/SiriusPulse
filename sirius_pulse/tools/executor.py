"""Tool executor — validates parameters and safely runs tools."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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


def _logged_params(tool: ToolDefinition, params: dict[str, Any]) -> dict[str, Any]:
    # 人物声明、私有资产与小本不进通用诊断日志，完整内容只留在受控领域审计。
    return {key: "<redacted>" for key in params} if tool.redact_logs else params


def _caller_id(invocation_context: ToolInvocationContext | None) -> str:
    return invocation_context.caller_user_id if invocation_context is not None else ""


_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _error_code(result: ToolResult) -> str:
    """工具结果里的稳定错误码；只认 snake_case 标识符，不会带出用户内容。"""
    data = result.data if isinstance(result.data, dict) else {}
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    for candidate in (data.get("code"), nested.get("code")):
        code = str(candidate or "").strip()
        if _ERROR_CODE_RE.match(code):
            return code
    return ""


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

    @property
    def engine_context(self) -> Any | None:
        """注入给内置工具的引擎上下文（未接入引擎时为 None）。"""
        return self._engine_context

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
    ) -> tuple[dict[str, Any] | None, ToolInvocationContext | None, ToolResult | None]:
        """Apply the same validation contract to sync and async tools."""
        if tool._run_func is None:
            return (
                None,
                invocation_context,
                ToolResult(success=False, error=f"TOOL '{tool.name}' 没有可执行的 run() 函数"),
            )

        if invocation_context is not None and getattr(invocation_context, "self_initiated", False):
            blocked = _self_initiated_block_reason(tool)
            if blocked:
                return None, invocation_context, ToolResult(success=False, error=blocked)

        if chain_context is not None:
            params = chain_context.resolve_templates(params)

        invocation_context, source_error = _bind_pending_source(tool, params, invocation_context)
        if source_error:
            return None, invocation_context, ToolResult(success=False, error=source_error)

        access_error = validate_tool_access(tool=tool, invocation_context=invocation_context)
        if access_error:
            return None, invocation_context, ToolResult(success=False, error=access_error)

        admin_error = self._validate_admin_requirement(tool, invocation_context)
        if admin_error:
            return None, invocation_context, ToolResult(success=False, error=admin_error)

        for param_def in tool.parameters:
            if param_def.required and param_def.name not in params:
                return (
                    None,
                    invocation_context,
                    ToolResult(success=False, error=f"缺少必填参数: {param_def.name}"),
                )

        call_params: dict[str, Any] = {}
        for param_def in tool.parameters:
            if param_def.name in params:
                call_params[param_def.name] = _coerce_type(params[param_def.name], param_def.type)
            elif param_def.default is not None:
                call_params[param_def.name] = param_def.default
        if tool.allow_extra_parameters:
            for name, value in params.items():
                call_params.setdefault(name, value)
        return call_params, invocation_context, None

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
            and tool.trusted
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
        """同步入口：给没有 ``await`` 的调用方（脚本、测试）用，执行逻辑与
        :meth:`execute_async` 完全相同。

        已经在事件循环里时，这次调用放到独立线程的新事件循环里跑，并阻塞当前线程
        直到结束——和以前的同步执行一样会占住调用方。引擎里一律用
        :meth:`execute_async`；依赖主循环对象（例如平台适配器）的异步工具不要走这里。
        """
        coro = self.execute_async(
            tool,
            params,
            chain_context=chain_context,
            invocation_context=invocation_context,
            max_retries=max_retries,
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="tool-execute") as pool:
            return pool.submit(asyncio.run, coro).result()

    async def execute_async(
        self,
        tool: ToolDefinition,
        params: dict[str, Any],
        timeout: float = 0,
        chain_context: ToolChainContext | None = None,
        invocation_context: ToolInvocationContext | None = None,
        max_retries: int = 0,
    ) -> ToolResult:
        """执行一次工具调用：校验参数、注入运行时参数、按需重试、记录遥测。

        同步工具在线程池里跑（连同数据存储落盘），异步工具直接在事件循环上
        ``await``，其余步骤两者完全一致。

        * ``chain_context``：先用它解析参数里的 ``${tool_name}`` / ``${tool_name.field}``
          占位符，执行完再把结果存回去，供同一轮后面的工具引用。
        * ``timeout``：最长等待秒数，0 表示不限；超时返回失败结果（线程里的同步工具
          无法被强行中止，会在后台跑完）。
        * ``max_retries``：暂时性失败（超时、连接错误等）额外重试的次数。
        """
        run = self._execute(tool, params, chain_context, invocation_context, max_retries)
        if timeout <= 0:
            return await run
        try:
            return await asyncio.wait_for(run, timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("TOOL '%s' 执行超时 (限制 %.1f秒)", tool.name, timeout)
            return ToolResult(
                success=False,
                error=f"TOOL执行超时（限制 {timeout:.0f} 秒），请稍后重试或联系管理员",
            )

    async def _execute(
        self,
        tool: ToolDefinition,
        params: dict[str, Any],
        chain_context: ToolChainContext | None,
        invocation_context: ToolInvocationContext | None,
        max_retries: int,
    ) -> ToolResult:
        """唯一的执行流程；拒绝、成功、失败都会记遥测并写回 chain_context。"""
        start_time = time.perf_counter()
        logger.info(
            "Tool execute start: %s(params=%s, caller=%s)",
            tool.name,
            _logged_params(tool, params),
            _caller_id(invocation_context),
        )
        try:
            call_params, invocation_context, rejected = self._prepare_call_params(
                tool, params, chain_context, invocation_context
            )
            if rejected is not None:
                logger.warning("Tool execute rejected: %s -> %s", tool.name, rejected.error)
                result = rejected
            else:
                assert call_params is not None
                self._inject_runtime_params(tool, call_params, invocation_context)
                logger.info(
                    "Tool execute calling: %s(final_params=%s)",
                    tool.name,
                    _logged_params(tool, call_params),
                )
                result = await self._call_with_retries(tool, call_params, max_retries)
        except Exception as exc:
            logger.error("Tool execute exception: %s -> %s", tool.name, exc)
            result = ToolResult(success=False, error=str(exc))

        await asyncio.to_thread(
            self._record_telemetry,
            tool,
            params,
            invocation_context,
            result,
            (time.perf_counter() - start_time) * 1000,
        )
        if chain_context is not None:
            chain_context.store(tool.name, result)
        return result

    async def _call_with_retries(
        self, tool: ToolDefinition, call_params: dict[str, Any], max_retries: int
    ) -> ToolResult:
        assert tool._run_func is not None
        run_func = tool._run_func
        data_store = self._get_data_store(tool.name)
        is_async = inspect.iscoroutinefunction(run_func)

        def run_sync() -> Any:
            raw = run_func(**call_params)
            data_store.save()
            return raw

        for attempt in range(max_retries + 1):
            try:
                if is_async:
                    raw = await run_func(**call_params)
                    data_store.save()
                else:
                    # 同步工具与它的数据存储落盘都不能占住事件循环。
                    raw = await asyncio.to_thread(run_sync)
            except Exception as exc:
                if attempt < max_retries and _should_retry(exc):
                    logger.warning("TOOL '%s' 第%d次执行失败（将重试）: %s", tool.name, attempt + 1, exc)
                    continue
                logger.error("TOOL '%s' 执行异常: %s", tool.name, exc)
                return ToolResult(success=False, error=str(exc))
            result = ToolResult.from_raw_result(raw)
            result.success = True if result.error == "" else result.success
            logger.info(
                "Tool execute done: %s -> success=%s | summary=%r | text_blocks=%d | "
                "multimodal_blocks=%d",
                tool.name,
                result.success,
                "<redacted>" if tool.redact_logs else result.to_display_text()[:200],
                len(result.text_blocks),
                len(result.multimodal_blocks),
            )
            return result
        return ToolResult(success=False, error="未知错误")

    def _record_telemetry(
        self,
        tool: ToolDefinition,
        params: dict[str, Any],
        invocation_context: ToolInvocationContext | None,
        result: ToolResult,
        duration_ms: float,
    ) -> None:
        """遥测是尽力而为的，任何错误都不能影响执行结果。"""
        try:
            self._telemetry.record(
                ToolExecutionRecord(
                    tool_name=tool.name,
                    timestamp=time.time(),
                    success=result.success,
                    duration_ms=round(duration_ms, 2),
                    error=result.error if not result.success and not tool.redact_logs else "",
                    error_code="" if result.success else _error_code(result),
                    caller_user_id=_caller_id(invocation_context),
                    params=_logged_params(tool, params) if params else None,
                    result_summary=(
                        result.to_display_text()[:500]
                        if result.success and not tool.redact_logs
                        else ""
                    ),
                )
            )
        except Exception:
            pass

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
        if self._engine_context is not None and self._engine_context.is_qq_bot_group_admin(
            group_id
        ):
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
    if type_lower in {"integer"}:
        try:
            return int(value)
        except (ValueError, TypeError):
            return value
    if type_lower in {"number"}:
        try:
            return float(value)
        except (ValueError, TypeError):
            return value
    if type_lower in {"boolean"}:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    if type_lower in {"dict", "object", "json"}:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value
    if type_lower in {"array", "list[str]", "list"}:
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


def _needs_request_source(tool: Any, params: dict[str, Any]) -> bool:
    needs = getattr(tool, "needs_request_source", None)
    return bool(callable(needs) and needs(params))


def _bind_pending_source(
    tool: ToolDefinition,
    params: dict[str, Any],
    context: ToolInvocationContext | None,
) -> tuple[ToolInvocationContext | None, str]:
    """Select only an already admitted pending instruction, never arbitrary readable history."""
    if (
        context is None
        or context.self_initiated
        or context.evidence_callers is None
        or not _needs_request_source(tool, params)
    ):
        return context, ""
    target = params.get("target")
    refs = [str(params.get(key) or "") for key in ("evidence_ref", "source_ref")]
    if isinstance(target, dict):
        refs.append(str(target.get("evidence_ref") or ""))
    selected = list(dict.fromkeys(ref for ref in refs if ref))
    authors = dict(context.evidence_callers)
    if not authors:
        return context, "evidence_invalid: 本回合来源作者无法验证"
    if any(ref not in context.evidence_refs or ref not in authors for ref in selected):
        return context, "evidence_invalid: 只能使用本回合已接纳的真实指令来源"
    if not selected:
        optional = getattr(tool, "request_source_optional", None)
        if callable(optional) and optional(params):
            # Keep the actor for scope checks, but never manufacture write
            # authority from a read-only lookup by explicit identity.
            return replace(context, evidence_refs=()), ""
        selected = list(authors)
    identities = {authors[ref].user_id for ref in selected}
    if len(identities) != 1:
        return context, "source_ambiguous: 多人请求必须明确选择本次操作的指令来源"
    selected_ref = selected[0]
    caller = authors[selected_ref]
    return replace(context, caller=caller, evidence_refs=(selected_ref,)), ""


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
