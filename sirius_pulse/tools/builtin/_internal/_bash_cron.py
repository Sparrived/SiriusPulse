"""Project crontab storage and scheduling for the built-in Bash tool."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.tools import cron_tasks
from sirius_pulse.tools.models import BackgroundTaskSpec, ToolInvocationContext

logger = logging.getLogger(__name__)
CommandRunner = Callable[..., Awaitable[dict[str, Any]]]
_PENDING_PAYLOAD_TTL_SECONDS = 24 * 60 * 60
_MAX_PENDING_PAYLOADS = 256
_pending_payloads: dict[str, tuple[float, dict[str, Any]]] = {}
_pending_payload_locks: dict[str, asyncio.Lock] = {}


def _pending_payload_key(job: dict[str, Any], run_key: str) -> str:
    return f"{job.get('id', '')}:{run_key or job.get('last_run_key', '')}"


def _prune_pending_payloads() -> None:
    cutoff = time.monotonic() - _PENDING_PAYLOAD_TTL_SECONDS
    keep = sorted(
        ((key, cached) for key, cached in _pending_payloads.items() if cached[0] >= cutoff),
        key=lambda item: item[1][0],
        reverse=True,
    )[:_MAX_PENDING_PAYLOADS]
    _pending_payloads.clear()
    _pending_payloads.update(keep)
    retained = set(_pending_payloads)
    for key, lock in list(_pending_payload_locks.items()):
        if key not in retained and not lock.locked():
            _pending_payload_locks.pop(key, None)


def create_background_tasks(ctx: Any, run_command: CommandRunner) -> list[BackgroundTaskSpec]:
    async def _check() -> None:
        await check_tasks(ctx, run_command)

    return [
        BackgroundTaskSpec(
            name="bash_cron_check",
            interval_seconds=max(
                1.0, float(ctx.get_config_value("cron_check_interval_seconds", 10))
            ),
            task_func=_check,
        )
    ]


def handle_request(
    request: dict[str, Any],
    *,
    cwd: Path,
    data_store: Any,
    chat_context: dict[str, Any] | None,
    engine_context: Any,
    invocation_context: ToolInvocationContext | None,
) -> dict[str, Any]:
    context = dict(chat_context or {})
    group_id = str(context.get("group_id") or context.get("chat_id") or "").strip()
    if not group_id:
        return {"success": False, "error": "crontab 任务必须绑定当前聊天会话"}

    jobs = [
        job
        for job in (data_store.get("cron_jobs", []) if data_store else [])
        if isinstance(job, dict)
    ]
    owner_id = (
        invocation_context.caller_user_id if invocation_context else str(context.get("user_id", ""))
    )
    owner_name = invocation_context.caller_name if invocation_context else ""
    adapter_type = str(context.get("adapter_type", ""))
    if not adapter_type and engine_context is not None:
        getter = getattr(engine_context, "get_current_adapter_type", None)
        if callable(getter):
            adapter_type = str(getter() or "")

    scoped = [job for job in jobs if job.get("group_id") == group_id]
    action = request.get("action")
    if action == "list":
        if not scoped:
            return {
                "success": True,
                "summary": "当前聊天没有定时任务",
                "text_blocks": ["当前没有定时任务。"],
            }
        lines = [f"{job.get('expression', '')} {job.get('command', '')}".strip() for job in scoped]
        return {
            "success": True,
            "summary": f"列出 {len(scoped)} 个定时任务（标准 crontab 格式）",
            "text_blocks": ["\n".join(lines)],
        }

    if action == "remove":
        kept = [job for job in jobs if job.get("group_id") != group_id]
        removed = len(jobs) - len(kept)
        data_store.set("cron_jobs", kept)
        data_store.save()
        return {
            "success": True,
            "summary": f"已删除 {removed} 个定时任务",
            "text_blocks": [f"✅ 已删除当前聊天的 {removed} 个定时任务。"],
        }

    created: list[dict[str, Any]] = []
    replacement = [job for job in jobs if job.get("group_id") != group_id]
    for entry in request.get("entries", []):
        job = {
            "id": f"cron_{uuid.uuid4().hex[:12]}",
            "schedule_type": "cron",
            "expression": entry["expression"],
            "command": entry["command"],
            "cwd": str(cwd),
            "group_id": group_id,
            "owner_user_id": owner_id,
            "owner_name": owner_name,
            "owner_is_developer": bool(
                invocation_context and invocation_context.caller_is_developer
            ),
            "adapter_type": adapter_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_run_key": "",
            "run_count": 0,
        }
        replacement.append(job)
        created.append(job)
    data_store.set("cron_jobs", replacement)
    data_store.save()
    lines = ["✅ 已注册内部定时任务："]
    lines.extend(f"[{job['id']}] {job['expression']} {job['command']}" for job in created)
    return {
        "success": True,
        "summary": f"已注册 {len(created)} 个定时任务",
        "text_blocks": ["\n".join(lines)],
    }


async def check_tasks(ctx: Any, run_command: CommandRunner) -> None:
    store = ctx.get_data_store("bash")
    jobs = [job for job in (store.get("cron_jobs", []) or []) if isinstance(job, dict)]
    if not jobs:
        return

    now = datetime.now(cron_tasks.CRON_TIMEZONE)
    changed = False
    remaining: list[dict[str, Any]] = []
    for job in jobs:
        if not job.get("group_id") or not job.get("command"):
            changed = True
            continue
        if not job.get("owner_is_developer"):
            # cron 重放直接调 run()，不经过 ToolExecutor 的 developer 门禁，所以这里
            # 必须自己把关。注册者是普通成员的任务是旧语义（谁都能执行 bash）下登记的，
            # 现在不可能再合法执行：移除并留痕，而不是每个 tick 失败一次。
            logger.warning(
                "移除越权的内部 cron 任务 %s：注册者 %s 不是人格 owner",
                job.get("id", "unknown"),
                job.get("owner_user_id", "unknown"),
            )
            changed = True
            continue
        pending_run_key = str(job.get("pending_run_key", "") or "")
        due, due_run_key = cron_tasks.task_is_due(job, now)
        run_key = pending_run_key or due_run_key
        if not pending_run_key and (not due or job.get("last_run_key") == due_run_key):
            remaining.append(job)
            continue

        # Persist the occurrence identity and an at-most-once execution fence
        # before arbitrary shell work. The generated payload stays memory-only;
        # after a restart we consume an ambiguous started occurrence rather than
        # rerun a command whose side effects may already have happened.
        job["pending_run_key"] = run_key
        execution_started = str(job.get("pending_execution_started_key", "") or "") == run_key
        allow_command_execution = not execution_started
        if allow_command_execution:
            job["pending_execution_started_key"] = run_key
        changed = True
        store.set("cron_jobs", jobs)
        store.save()
        try:
            accepted = await _run_job(
                ctx,
                job,
                run_command,
                run_key=run_key,
                allow_command_execution=allow_command_execution,
            )
        except Exception:
            accepted = False
            logger.exception("内部 cron 任务执行失败: %s", job.get("id", "unknown"))
        if accepted is True:
            job["last_run_key"] = run_key
            job["run_count"] = int(job.get("run_count", 0) or 0) + 1
            job.pop("pending_run_key", None)
            job.pop("pending_execution_started_key", None)
        remaining.append(job)

    if changed:
        store.set("cron_jobs", remaining)
        store.save()


async def _run_job(
    ctx: Any,
    job: dict[str, Any],
    run_command: CommandRunner,
    *,
    run_key: str = "",
    allow_command_execution: bool = True,
) -> bool:
    event_id = _pending_payload_key(job, run_key)
    _prune_pending_payloads()
    lock = _pending_payload_locks.setdefault(event_id, asyncio.Lock())
    async with lock:
        return await _run_job_locked(
            ctx,
            job,
            run_command,
            run_key=run_key,
            allow_command_execution=allow_command_execution,
        )


async def _run_job_locked(
    ctx: Any,
    job: dict[str, Any],
    run_command: CommandRunner,
    *,
    run_key: str,
    allow_command_execution: bool,
) -> bool:
    group_id = str(job.get("group_id", ""))
    user_id = str(job.get("owner_user_id", ""))
    user_name = str(job.get("owner_name", ""))
    adapter_type = str(job.get("adapter_type", ""))
    if not adapter_type:
        getter = getattr(ctx, "get_current_adapter_type", None)
        if callable(getter):
            adapter_type = str(getter() or "")
    event_id = _pending_payload_key(job, run_key)
    _prune_pending_payloads()
    cached = _pending_payloads.get(event_id)
    if cached is not None:
        payload = cached[1]
    elif not allow_command_execution:
        logger.warning(
            "内部 cron occurrence 已越过执行围栏但内存载荷不可用，禁止重复执行: %s",
            event_id,
        )
        return True
    else:
        caller = UnifiedUser(
            user_id=user_id,
            name=user_name,
            metadata={"is_developer": bool(job.get("owner_is_developer", False))},
        )
        invocation_context = ToolInvocationContext(
            caller=caller,
            group_id=group_id,
            adapter_type=adapter_type or "",
        )
        chat_context = dict(invocation_context.chat_context)
        result = await run_command(
            str(job["command"]),
            cwd=str(job.get("cwd") or "."),
            timeout_seconds=10.0,
            max_output_chars=12_000,
            data_store=ctx.get_data_store("bash"),
            chat_context=chat_context,
            engine_context=ctx,
            invocation_context=invocation_context,
            _skip_crontab=True,
        )
        output = (
            result.get("text_blocks", [""])[0] if result.get("success") else result.get("error", "")
        )
        if not output:
            output = "命令执行成功，但没有输出。"
        message = await ctx.generate_scheduled_message(
            job=job,
            command_output=str(output),
            group_id=group_id,
            user_id=user_id,
            user_name=user_name,
            adapter_type=adapter_type,
            caller_is_developer=bool(job.get("owner_is_developer", False)),
        )
        raw_payload = message if isinstance(message, dict) else {"text": str(message)}
        payload = {
            "text": str(raw_payload.get("text", "")).strip(),
            "reply_references": raw_payload.get("reply_references", []),
            "sticker_names": raw_payload.get("sticker_names", []),
            "poke_user_ids": raw_payload.get("poke_user_ids", []),
        }
        if not (payload["text"] or payload["sticker_names"] or payload["poke_user_ids"]):
            return True
        # Retain generated output only in bounded process memory.  Persisting a
        # command-derived payload could write credentials or other sensitive
        # output to disk.  The durable job stores only the stable occurrence key.
        _pending_payloads[event_id] = (time.monotonic(), payload)
        _prune_pending_payloads()

    accepted = await ctx.dispatch_proactive_message(
        group_id=group_id,
        text=str(payload.get("text", "")),
        adapter_type=adapter_type,
        event_id=event_id,
        reply_references=payload.get("reply_references", []),
        sticker_names=payload.get("sticker_names", []),
        poke_user_ids=payload.get("poke_user_ids", []),
    )
    if accepted is True:
        _pending_payloads.pop(event_id, None)
        return True
    return False
