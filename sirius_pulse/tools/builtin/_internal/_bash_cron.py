"""Bash 工具的 crontab：登记、到点执行与投递。

定时任务存在人格 ``persona.db`` 的到期任务表里（``kind = bash_cron``，见
:mod:`sirius_pulse.scheduling.due_jobs`），每行的 ``due_at`` 是表达式下一次命中的整分钟，
由统一的 ticker 到点叫醒；不再每十秒扫一遍。旧版本 ``tool_data/bash.json`` 里的
``cron_jobs`` 在第一次读表时自动迁移过来。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.scheduling.due_jobs import DueJobBoard, DueJobKind, board_for_store
from sirius_pulse.tools import cron_tasks
from sirius_pulse.tools.models import ToolInvocationContext

logger = logging.getLogger(__name__)
KIND = "bash_cron"
#: 旧版本 JSON 存储里的键，只用于迁移。
_LEGACY_KEY = "cron_jobs"
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


def create_background_tasks(ctx: Any, run_command: CommandRunner) -> list[DueJobKind]:
    async def _check() -> None:
        await check_tasks(ctx, run_command)

    return [
        DueJobKind(
            kind=KIND,
            handler=_check,
            data_store=ctx.get_data_store("bash"),
            # 投递没被平台确认的那一次，隔这么久重试同一个 occurrence。
            retry_seconds=max(1.0, float(ctx.get_config_value("cron_check_interval_seconds", 10))),
            name="bash_cron_check",
        )
    ]


def list_jobs(data_store: Any) -> list[dict[str, Any]]:
    """该人格登记的全部定时任务（所有聊天）。"""
    return _board(data_store).payloads(KIND)


def _board(data_store: Any) -> DueJobBoard:
    board = board_for_store(data_store)
    board.import_legacy(KIND, data_store, _LEGACY_KEY, _first_check)
    return board


def _first_check(job: dict[str, Any]) -> datetime | None:
    """迁移进来的任务：有没投递完的 occurrence 就马上看，否则等下一次命中。"""
    if job.get("pending_run_key"):
        return datetime.now(timezone.utc)
    return _next_fire(job, datetime.now(cron_tasks.CRON_TIMEZONE))


def _next_fire(job: dict[str, Any], after: datetime) -> datetime | None:
    try:
        return cron_tasks.next_fire(str(job.get("expression", "")), after)
    except cron_tasks.CronParseError:
        return None


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

    board = _board(data_store)
    jobs = board.payloads(KIND)
    owner_id = (
        invocation_context.caller_user_id if invocation_context else str(context.get("user_id", ""))
    )
    owner_name = invocation_context.caller_name if invocation_context else ""
    adapter_type = str(context.get("adapter_type", ""))
    if not adapter_type and engine_context is not None:
        adapter_type = str(engine_context.get_current_adapter_type() or "")

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
        with board.transaction():
            for job in scoped:
                board.delete(str(job.get("id", "")))
        removed = len(scoped)
        return {
            "success": True,
            "summary": f"已删除 {removed} 个定时任务",
            "text_blocks": [f"✅ 已删除当前聊天的 {removed} 个定时任务。"],
        }

    created: list[dict[str, Any]] = []
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
        created.append(job)
    # crontab - 是覆盖语义：这个聊天原有的定时任务整体换成新的一份。
    now = datetime.now(cron_tasks.CRON_TIMEZONE)
    with board.transaction():
        for job in scoped:
            board.delete(str(job.get("id", "")))
        for job in created:
            board.put(KIND, job["id"], job, due_at=_next_fire(job, now))
    lines = ["✅ 已注册内部定时任务："]
    lines.extend(f"[{job['id']}] {job['expression']} {job['command']}" for job in created)
    return {
        "success": True,
        "summary": f"已注册 {len(created)} 个定时任务",
        "text_blocks": ["\n".join(lines)],
    }


async def check_tasks(ctx: Any, run_command: CommandRunner) -> None:
    store = ctx.get_data_store("bash")
    board = await asyncio.to_thread(_board, store)
    jobs = await asyncio.to_thread(board.payloads, KIND)
    if not jobs:
        return

    now = datetime.now(cron_tasks.CRON_TIMEZONE)
    for job in jobs:
        job_id = str(job.get("id", ""))
        if not job.get("group_id") or not job.get("command"):
            await asyncio.to_thread(board.delete, job_id)
            continue
        pending_run_key = str(job.get("pending_run_key", "") or "")
        due, due_run_key = cron_tasks.task_is_due(job, now)
        run_key = pending_run_key or due_run_key
        if not pending_run_key and (not due or job.get("last_run_key") == due_run_key):
            await asyncio.to_thread(board.set_due, job_id, _next_fire(job, now))
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
        await asyncio.to_thread(board.put, KIND, job_id, job, due_at=None)
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
            next_check = _next_fire(job, now)
        else:
            # 这一次还没投递成功：保持到期，ticker 隔一会儿重试同一个 occurrence。
            next_check = datetime.now(timezone.utc)
        if await asyncio.to_thread(board.get, job_id) is not None:
            await asyncio.to_thread(board.put, KIND, job_id, job, due_at=next_check)


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
        adapter_type = str(ctx.get_current_adapter_type() or "")
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
            # 定时任务要拿着输出立刻写消息，不能交给后台再回调。
            background_on_timeout=False,
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
