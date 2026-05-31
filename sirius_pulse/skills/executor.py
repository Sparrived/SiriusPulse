"""Skill executor — validates parameters and safely runs skills."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from pathlib import Path
from typing import Any

from sirius_pulse.skills.data_store import SkillDataStore
from sirius_pulse.skills.models import (
    SkillChainContext,
    SkillDefinition,
    SkillInvocationContext,
    SkillResult,
)
from sirius_pulse.skills.security import validate_skill_access
from sirius_pulse.skills.telemetry import SkillExecutionRecord, SkillTelemetry
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


class SkillExecutor:
    """Execute skills with parameter validation, retry, telemetry, and data store injection."""

    def __init__(self, work_path: Path | WorkspaceLayout) -> None:
        self._layout = work_path if isinstance(work_path, WorkspaceLayout) else WorkspaceLayout(work_path)
        self._data_stores: dict[str, SkillDataStore] = {}
        self._telemetry = SkillTelemetry(self._layout.skill_data_dir() / ".telemetry.jsonl")
        self._bridges: dict[str, Any] = {}
        self._chat_context: dict[str, Any] = {}

    def set_chat_context(self, group_id: str = "", user_id: str = "") -> None:
        """Set current chat context so skills know where they are being invoked from."""
        is_private = group_id.startswith("private_")
        if is_private:
            chat_id = group_id.replace("private_", "").replace("qq_", "")
            chat_type = "private"
        else:
            chat_id = group_id
            chat_type = "group"
        self._chat_context = {
            "group_id": group_id,
            "user_id": user_id,
            "chat_type": chat_type,
            "chat_id": chat_id,
            "is_private": is_private,
        }

    def set_bridge(self, adapter_type: str, bridge: Any) -> None:
        """Register a platform bridge for a given adapter type."""
        self._bridges[adapter_type] = bridge

    def get_bridge_for_skill(self, skill: SkillDefinition) -> Any | None:
        """Return the best-matching bridge for a skill.

        If the skill declares adapter_types, return the first matching bridge.
        Otherwise return the first available bridge (or None).
        """
        if not self._bridges:
            return None
        if skill.adapter_types:
            for at in skill.adapter_types:
                if at in self._bridges:
                    return self._bridges[at]
            return None
        return next(iter(self._bridges.values()), None)

    def get_data_store(self, skill_name: str) -> SkillDataStore:
        """Get or create the persistent data store for a skill."""
        if skill_name not in self._data_stores:
            store_path = self._layout.skill_data_dir() / f"{skill_name}.json"
            self._data_stores[skill_name] = SkillDataStore(store_path)
        return self._data_stores[skill_name]

    # Backward-compatible alias
    _get_data_store = get_data_store

    def execute(
        self,
        skill: SkillDefinition,
        params: dict[str, Any],
        chain_context: SkillChainContext | None = None,
        invocation_context: SkillInvocationContext | None = None,
        max_retries: int = 0,
    ) -> SkillResult:
        """Execute a skill synchronously with parameter validation and optional retry.

        If *chain_context* is provided, any ``${skill_name}`` / ``${skill_name.field}``
        placeholders in parameter values are resolved against previously executed
        skills' results before the skill is called.  After execution the result is
        stored back into *chain_context* under ``skill.name`` for downstream use.

        The data_store is automatically injected as a keyword argument
        if the skill's run() function accepts it.

        Args:
            max_retries: Number of extra attempts for transient failures
                (timeout, connection error, etc.).
        """
        start_time = time.perf_counter()
        skill_result: SkillResult | None = None
        logger.info(
            "Skill execute start: %s(params=%s, caller=%s)",
            skill.name,
            params,
            getattr(invocation_context, "caller", None) if invocation_context else None,
        )

        try:
            if skill._run_func is None:
                skill_result = SkillResult(success=False, error=f"SKILL '{skill.name}' 没有可执行的 run() 函数")
                logger.warning("Skill execute failed: %s -> no run() function", skill.name)
                return skill_result

            # Resolve chain-context template placeholders before validation
            if chain_context is not None:
                params = chain_context.resolve_templates(params)

            # Validate required parameters
            for param_def in skill.parameters:
                if param_def.required and param_def.name not in params:
                    skill_result = SkillResult(
                        success=False,
                        error=f"缺少必填参数: {param_def.name}",
                    )
                    logger.warning(
                        "Skill execute failed: %s -> missing required param '%s'",
                        skill.name,
                        param_def.name,
                    )
                    return skill_result

            # Apply defaults for optional parameters
            call_params: dict[str, Any] = {}
            for param_def in skill.parameters:
                if param_def.name in params:
                    call_params[param_def.name] = _coerce_type(
                        params[param_def.name], param_def.type
                    )
                elif param_def.default is not None:
                    call_params[param_def.name] = param_def.default

            access_error = validate_skill_access(skill=skill, invocation_context=invocation_context)
            if access_error:
                skill_result = SkillResult(success=False, error=access_error)
                logger.warning("Skill execute failed: %s -> access denied: %s", skill.name, access_error)
                return skill_result

            data_store = self._get_data_store(skill.name)
            injection_plan = _build_injection_plan(skill._run_func)
            if injection_plan.accepts("data_store"):
                call_params["data_store"] = data_store
            if invocation_context is not None and injection_plan.accepts("invocation_context"):
                call_params["invocation_context"] = invocation_context
            bridge = self.get_bridge_for_skill(skill)
            if bridge is not None and injection_plan.accepts("bridge"):
                call_params["bridge"] = bridge
            if injection_plan.accepts("chat_context") and self._chat_context:
                call_params["chat_context"] = dict(self._chat_context)

            logger.info("Skill execute calling: %s(final_params=%s)", skill.name, call_params)

            # Run with optional retry for transient failures
            for attempt in range(max_retries + 1):
                try:
                    if inspect.iscoroutinefunction(skill._run_func):
                        # Synchronous execute() cannot await; raise so caller uses execute_async
                        raise RuntimeError(
                            f"SKILL '{skill.name}' is async and must be executed via execute_async"
                        )
                    result = skill._run_func(**call_params)
                    # Persist data store after execution
                    data_store.save()
                    skill_result = SkillResult.from_raw_result(result)
                    skill_result.success = True if skill_result.error == "" else skill_result.success
                    logger.info(
                        "Skill execute done: %s -> success=%s | summary=%r | text_blocks=%d | "
                        "multimodal_blocks=%d",
                        skill.name,
                        skill_result.success,
                        skill_result.to_display_text()[:200],
                        len(skill_result.text_blocks),
                        len(skill_result.multimodal_blocks),
                    )
                    break
                except Exception as exc:
                    if attempt < max_retries and _should_retry(exc):
                        logger.warning(
                            "SKILL '%s' 第%d次执行失败（将重试）: %s",
                            skill.name, attempt + 1, exc,
                        )
                        continue
                    logger.error("SKILL '%s' 执行异常: %s", skill.name, exc)
                    skill_result = SkillResult(success=False, error=str(exc))
                    break
        finally:
            # Telemetry is best-effort and must not affect the result
            if skill_result is not None:
                duration_ms = (time.perf_counter() - start_time) * 1000
                try:
                    caller_id = ""
                    if invocation_context is not None:
                        caller_id = getattr(invocation_context, "caller_user_id", "") or ""
                    summary = skill_result.to_display_text()[:500] if skill_result.success else ""
                    self._telemetry.record(
                        SkillExecutionRecord(
                            skill_name=skill.name,
                            timestamp=time.time(),
                            success=skill_result.success,
                            duration_ms=round(duration_ms, 2),
                            error=skill_result.error if not skill_result.success else "",
                            caller_user_id=caller_id,
                            params=params if params else None,
                            result_summary=summary,
                        )
                    )
                except Exception:
                    pass

        # Record into chain context so subsequent skills can reference this result
        if chain_context is not None and skill_result is not None:
            chain_context.store(skill.name, skill_result)

        return skill_result if skill_result is not None else SkillResult(success=False, error="未知错误")

    async def execute_async(
        self,
        skill: SkillDefinition,
        params: dict[str, Any],
        timeout: float = 0,
        chain_context: SkillChainContext | None = None,
        invocation_context: SkillInvocationContext | None = None,
        max_retries: int = 0,
    ) -> SkillResult:
        """Execute a skill in a thread pool to avoid blocking the event loop.

        Async skills (coroutine functions) are awaited directly in the event
        loop instead of being dispatched to a thread pool.

        Args:
            skill: The skill definition to execute.
            params: Parameters to pass to the skill.
            timeout: Max seconds to wait. 0 means no limit.
            chain_context: Optional chain context for template resolution and
                result accumulation across a multi-skill round.
            max_retries: Number of extra attempts for transient failures.
        """
        if inspect.iscoroutinefunction(skill._run_func):
            # Async skills run directly in the event loop so they can await
            # bridge/adapter I/O without thread-pool indirection.
            coro = self._execute_async_skill(
                skill, params, chain_context, invocation_context, max_retries
            )
            if timeout > 0:
                try:
                    return await asyncio.wait_for(coro, timeout=timeout)
                except asyncio.TimeoutError:
                    logger.error("SKILL '%s' 执行超时 (限制 %.1f秒)", skill.name, timeout)
                    return SkillResult(
                        success=False,
                        error=f"SKILL执行超时（限制 {timeout:.0f} 秒），请稍后重试或联系管理员",
                    )
            return await coro

        if timeout > 0:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        self.execute,
                        skill,
                        params,
                        chain_context,
                        invocation_context,
                        max_retries,
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.error("SKILL '%s' 执行超时 (限制 %.1f秒)", skill.name, timeout)
                return SkillResult(
                    success=False,
                    error=f"SKILL执行超时（限制 {timeout:.0f} 秒），请稍后重试或联系管理员",
                )
        return await asyncio.to_thread(
            self.execute,
            skill,
            params,
            chain_context,
            invocation_context,
            max_retries,
        )

    async def _execute_async_skill(
        self,
        skill: SkillDefinition,
        params: dict[str, Any],
        chain_context: SkillChainContext | None = None,
        invocation_context: SkillInvocationContext | None = None,
        max_retries: int = 0,
    ) -> SkillResult:
        """Execute an async skill directly in the event loop."""
        start_time = time.perf_counter()
        logger.info(
            "Skill async execute start: %s(params=%s, caller=%s)",
            skill.name,
            params,
            getattr(invocation_context, "caller", None) if invocation_context else None,
        )
        try:
            if skill._run_func is None:
                logger.warning("Skill async execute failed: %s -> no run() function", skill.name)
                return SkillResult(success=False, error=f"SKILL '{skill.name}' 没有可执行的 run() 函数")

            # Resolve chain-context template placeholders before validation
            if chain_context is not None:
                params = chain_context.resolve_templates(params)

            call_params = dict(params)
            data_store = self._get_data_store(skill.name)
            injection_plan = _build_injection_plan(skill._run_func)
            if injection_plan.accepts("data_store"):
                call_params["data_store"] = data_store
            if invocation_context is not None and injection_plan.accepts("invocation_context"):
                call_params["invocation_context"] = invocation_context
            bridge = self.get_bridge_for_skill(skill)
            if bridge is not None and injection_plan.accepts("bridge"):
                call_params["bridge"] = bridge
            if injection_plan.accepts("chat_context") and self._chat_context:
                call_params["chat_context"] = dict(self._chat_context)

            logger.info("Skill async execute calling: %s(final_params=%s)", skill.name, call_params)

            skill_result: SkillResult | None = None
            for attempt in range(max_retries + 1):
                try:
                    result = await skill._run_func(**call_params)
                    data_store.save()
                    skill_result = SkillResult.from_raw_result(result)
                    skill_result.success = True if skill_result.error == "" else skill_result.success
                    logger.info(
                        "Skill async execute done: %s -> success=%s | summary=%r | text_blocks=%d | "
                        "multimodal_blocks=%d",
                        skill.name,
                        skill_result.success,
                        skill_result.to_display_text()[:200],
                        len(skill_result.text_blocks),
                        len(skill_result.multimodal_blocks),
                    )
                    break
                except Exception as exc:
                    if attempt < max_retries and _should_retry(exc):
                        logger.warning(
                            "SKILL '%s' 第%d次执行失败（将重试）: %s",
                            skill.name, attempt + 1, exc,
                        )
                        continue
                    logger.error("SKILL '%s' 执行异常: %s", skill.name, exc)
                    skill_result = SkillResult.from_raw_result(str(exc))
                    skill_result.success = False
                    skill_result.error = str(exc)
                    break

            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            if skill_result is not None:
                try:
                    caller_id = ""
                    if invocation_context is not None:
                        caller_id = getattr(invocation_context, "caller_user_id", "") or getattr(invocation_context, "caller", "") or ""
                    summary = skill_result.to_display_text()[:500] if skill_result.success else ""
                    self._telemetry.record(
                        SkillExecutionRecord(
                            skill_name=skill.name,
                            timestamp=time.time(),
                            success=skill_result.success,
                            duration_ms=elapsed_ms,
                            error=skill_result.error if not skill_result.success else "",
                            caller_user_id=caller_id,
                            params=params if params else None,
                            result_summary=summary,
                        )
                    )
                except Exception:
                    pass

            # Record into chain context so subsequent skills can reference this result
            if chain_context is not None and skill_result is not None:
                chain_context.store(skill.name, skill_result)

            return skill_result if skill_result is not None else SkillResult(success=False, error="未知错误")
        except Exception as exc:
            logger.error("Skill async execute exception: %s -> %s", skill.name, exc)
            return SkillResult(success=False, error=str(exc))

    def save_all_stores(self) -> None:
        """Persist all dirty data stores."""
        for store in self._data_stores.values():
            store.save()


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
