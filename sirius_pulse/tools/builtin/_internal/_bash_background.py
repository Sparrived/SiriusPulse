"""Bash 命令超时后转入后台：进程登记、输出落盘、结束回调与停止。

前台等不完的命令不再被直接杀掉。``bash`` 工具在超时那一刻把进程交给这里登记，
立刻给模型一个 ``bash_xxxxxxxx`` 任务 ID；进程继续跑，输出一直写在
``runtime/bg/<id>.log``。每个后台命令在 ``persona.db`` 的到期任务表里有一行
（``kind = bash_background``）：还在跑或结果还没交回时每隔两秒到期一次，统一的 ticker
到点调 :func:`process_once`，发现进程结束后像生图那样把结果回调给 Agent，恢复一轮
内部工作回合；没有后台命令时这一类完全不醒。

几条约束：

- 进程放在独立进程组里，停止时整组结束，管道和子进程不会残留；
- 每个后台任务都有最长存活时间，到点整组结束，免得忘了的命令一直占资源；
- 进程句柄在内存里、按人格隔离（同一进程跑多个人格）；表里那一行只是它的描述，
  供 ``background_task`` 列出和查找。进程活不过重启，重启后表里还标着运行中的
  记为「被中断」。人格卸载时会结束它名下所有后台进程；
- 被主动停止的任务不再回调——停的人已经知道结果了。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sirius_pulse.scheduling.due_jobs import DueJobBoard, DueJobKind, board_for_store
from sirius_pulse.tools.builtin._internal import _bash_runtime

logger = logging.getLogger(__name__)

TASK_PREFIX = "bash_"
KIND = "bash_background"
INTERRUPTED = "interrupted"
DEFAULT_MAX_BACKGROUND_JOBS = 3
DEFAULT_MAX_BACKGROUND_SECONDS = 1800.0
TICK_SECONDS = 2.0

_LOG_DIR_NAME = "bg"
_MAX_RETAINED_FINISHED = 20
_CALLBACK_MAX_ATTEMPTS = 3
_TAIL_CHARS = 4_000
_SUMMARY_CHARS = 500
_TERMINATE_GRACE_SECONDS = 2.0

#: 运行中 → 结束（exited / expired / stopped）；回调只对 exited / expired 发。
RUNNING = "running"
EXITED = "exited"
EXPIRED = "expired"
STOPPED = "stopped"


@dataclass(slots=True)
class BashJob:
    """一条正在（或曾经）后台运行的 Bash 命令。"""

    id: str
    owner: str
    command: str
    cwd: str
    log_path: Path
    process: Any
    deadline: float
    output_limit: int
    group_id: str = ""
    user_id: str = ""
    user_name: str = ""
    adapter_type: str = ""
    caller_is_developer: bool = False
    self_initiated: bool = False
    started_at: str = field(default_factory=lambda: _now_iso())
    started_monotonic: float = field(default_factory=time.monotonic)
    state: str = RUNNING
    returncode: int | None = None
    finished_at: str = ""
    callback_state: str = "pending"
    callback_attempts: int = 0

    @property
    def finished(self) -> bool:
        return self.state != RUNNING

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic

    def output_tail(self, limit: int = _TAIL_CHARS) -> str:
        """最近的输出（去掉 docker inspect 的内部标记），超出时只保留末尾。"""
        try:
            raw = self.log_path.read_bytes()
        except OSError:
            return ""
        text, _statuses, _ = _bash_runtime.decode_output_with_inspect_status(raw, len(raw) + 1)
        if len(text) > limit:
            return f"[前面的输出已省略，完整输出见 {self.log_path}]\n{text[-limit:]}"
        return text

    def describe(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "kind": "bash",
            "status": self.state,
            "command": self.command[:_SUMMARY_CHARS],
            "cwd": self.cwd,
            "started_at": self.started_at,
            "elapsed_seconds": round(self.elapsed_seconds(), 1),
            "returncode": self.returncode,
            "finished_at": self.finished_at,
            "log_path": str(self.log_path),
            "group_id": self.group_id,
        }


_jobs: dict[str, BashJob] = {}
#: 每个人格（owner）的到期任务表；登记第一个后台命令时接上。
_boards: dict[str, DueJobBoard] = {}


def due_job_kind(ctx: Any, data_store: Any) -> DueJobKind:
    """交给统一 ticker 的处理方式：有后台命令在跑或待回调时每两秒看一次。"""
    owner = owner_key(data_store)
    attach(owner, data_store)

    async def _handle() -> None:
        await process_once(ctx, owner)

    async def _recover() -> None:
        await asyncio.to_thread(recover_interrupted, owner)

    return DueJobKind(
        kind=KIND,
        handler=_handle,
        data_store=data_store,
        retry_seconds=TICK_SECONDS,
        on_start=_recover,
        name="bash_background_jobs",
    )


def attach(owner: str, data_store: Any) -> DueJobBoard | None:
    """记下这个人格的到期任务表；存储不在人格目录里（测试替身）时只留在内存。"""
    board = _boards.get(owner)
    if board is not None:
        return board
    try:
        board = board_for_store(data_store)
    except (OSError, ValueError):
        return None
    _boards[owner] = board
    return board


def recover_interrupted(owner: str) -> int:
    """上个进程留下、标着运行中的后台命令：进程已经不在了，记为被中断，不再回调。"""
    board = _boards.get(owner)
    if board is None:
        return 0
    stale = [
        job
        for job in board.jobs(KIND)
        if job.id not in _jobs and (job.payload.get("status") == RUNNING or job.due_at is not None)
    ]
    for job in stale:
        board.patch(
            job.id,
            {"status": INTERRUPTED, "callback_state": "skipped", "finished_at": _now_iso()},
            due_at=None,
        )
    return len(stale)


def list_rows(data_store: Any) -> list[dict[str, Any]]:
    """表里该人格的后台命令；还在内存里的用实时状态。"""
    owner = owner_key(data_store)
    board = attach(owner, data_store)
    if board is None:
        return [job.describe() for job in list_jobs(owner)]
    rows = []
    for row in board.jobs(KIND):
        live = get(owner, row.id)
        rows.append(live.describe() if live is not None else describe_row(row.payload))
    return rows


def describe_row(payload: dict[str, Any]) -> dict[str, Any]:
    """表里记下的描述（进程已不在内存里时用）。"""
    keys = (
        "task_id",
        "kind",
        "status",
        "command",
        "cwd",
        "started_at",
        "elapsed_seconds",
        "returncode",
        "finished_at",
        "log_path",
        "group_id",
    )
    described = {key: payload.get(key) for key in keys}
    described["kind"] = "bash"
    described["command"] = str(described.get("command") or "")
    described["elapsed_seconds"] = float(described.get("elapsed_seconds") or 0.0)
    return described


def _sync(job: BashJob) -> None:
    """把内存里的状态写回表里那一行；还要再看的，两秒后到期。"""
    board = _boards.get(job.owner)
    if board is None:
        return
    payload = {
        **job.describe(),
        "callback_state": job.callback_state,
        "callback_attempts": job.callback_attempts,
    }
    watching = job.state == RUNNING or job.callback_state == "pending"
    board.put(KIND, job.id, payload, due_at=time.time() + TICK_SECONDS if watching else None)


def owner_key(data_store: Any) -> str:
    """按人格隔离后台任务：同一进程里的多个人格各有各的 bash 数据存储。"""
    store_path = getattr(data_store, "store_path", None)
    if not store_path:
        return ""
    try:
        return str(Path(store_path).expanduser().resolve())
    except OSError:
        return str(store_path)


def log_dir(data_store: Any) -> Path:
    runtime_root = _bash_runtime.runtime_root_path(data_store)
    base = runtime_root if runtime_root is not None else Path(tempfile.gettempdir()) / "sirius_bash"
    directory = base / _LOG_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def new_task_id() -> str:
    return f"{TASK_PREFIX}{uuid.uuid4().hex[:8]}"


def spawn(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    log_path: Path,
    popen: Callable[..., Any] = subprocess.Popen,
) -> Any:
    """启动命令，标准输出与错误一起按时间顺序写进日志文件。"""
    kwargs: dict[str, Any] = {}
    if os.name == "posix":
        # 独立进程组：停止时 killpg 能把管道里的每个子进程一起带走。
        kwargs["start_new_session"] = True
    with open(log_path, "wb") as log_file:
        return popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            **kwargs,
        )


def register(job: BashJob, data_store: Any = None) -> None:
    _jobs[job.id] = job
    if data_store is not None:
        attach(job.owner, data_store)
    _sync(job)
    _prune(job.owner)


def running_count(owner: str) -> int:
    return sum(1 for job in _jobs.values() if job.owner == owner and job.state == RUNNING)


def get(owner: str, task_id: str) -> BashJob | None:
    job = _jobs.get(task_id)
    if job is None or job.owner != owner:
        return None
    return job


def list_jobs(owner: str) -> list[BashJob]:
    return [job for job in _jobs.values() if job.owner == owner]


def terminate_tree(process: Any, *, grace_seconds: float = _TERMINATE_GRACE_SECONDS) -> None:
    """结束进程及其所有子进程；先礼后兵。阻塞调用，请放进线程里跑。"""
    if process.poll() is not None:
        return
    pid = getattr(process, "pid", None)
    try:
        if os.name == "posix" and pid:
            os.killpg(pid, signal.SIGTERM)
        elif os.name == "nt" and pid:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        else:
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix" and pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=grace_seconds)
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        logger.warning("后台 Bash 进程未能确认结束: pid=%s", pid)


async def stop(owner: str, task_id: str) -> BashJob | None:
    """主动停止一个后台命令；已经结束的原样返回。"""
    job = get(owner, task_id)
    if job is None:
        return None
    if job.state == RUNNING:
        await asyncio.to_thread(terminate_tree, job.process)
        _finish(job, STOPPED)
        # 停的人已经知道结果，不再回调。
        job.callback_state = "skipped"
        await asyncio.to_thread(_sync, job)
        logger.info("后台 Bash 任务已按要求停止: %s", job.id)
    return job


def kill_all(owner: str) -> None:
    """人格卸载时调用：立即结束名下所有后台进程，不等待。"""
    for job in list_jobs(owner):
        if job.state != RUNNING:
            continue
        pid = getattr(job.process, "pid", None)
        try:
            if os.name == "posix" and pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                job.process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        _finish(job, STOPPED)
        job.callback_state = "skipped"
        _sync(job)


def _finish(job: BashJob, state: str) -> None:
    job.state = state
    job.returncode = job.process.poll()
    job.finished_at = _now_iso()


async def refresh(owner: str) -> None:
    """检查名下运行中的命令：结束的记下退出码，超过存活上限的整组结束。"""
    now = time.monotonic()
    for job in list_jobs(owner):
        if job.state != RUNNING:
            continue
        if job.process.poll() is not None:
            _finish(job, EXITED)
            logger.info("后台 Bash 任务已结束: %s (退出码 %s)", job.id, job.returncode)
        elif now >= job.deadline:
            await asyncio.to_thread(terminate_tree, job.process)
            _finish(job, EXPIRED)
            logger.warning("后台 Bash 任务超过存活上限，已结束: %s", job.id)


async def process_once(ctx: Any, owner: str) -> None:
    """后台轮询一次：刷新状态，最多把一个结束的任务回调给 Agent。"""
    try:
        await refresh(owner)
        for job in list_jobs(owner):
            if job.state not in {EXITED, EXPIRED} or job.callback_state != "pending":
                continue
            if not job.group_id:
                job.callback_state = "skipped"
                continue
            if await _deliver(ctx, job):
                return
    finally:
        jobs = list_jobs(owner)

        def _sync_all() -> None:
            for job in jobs:
                _sync(job)

        await asyncio.to_thread(_sync_all)


async def _deliver(ctx: Any, job: BashJob) -> bool:
    """把结果交回 Agent；返回 True 表示本轮已经处理过一个回调。"""
    if job.callback_attempts >= _CALLBACK_MAX_ATTEMPTS:
        job.callback_state = "failed"
        return True
    succeeded = job.state == EXITED and job.returncode == 0
    if job.state == EXPIRED:
        error = f"命令运行超过后台存活上限，已被结束（运行了 {job.elapsed_seconds():.0f} 秒）"
    elif succeeded:
        error = ""
    else:
        error = f"命令退出码 {job.returncode}"
    output = job.output_tail(job.output_limit)
    unsafe = False

    def before_tool() -> None:
        nonlocal unsafe
        unsafe = True

    job.callback_state = "running"
    try:
        payload = await ctx.handle_background_tool_result(
            callback_id=f"bash:{job.id}",
            tool_name="bash",
            outcome="success" if succeeded else "error",
            request_summary=f"（后台任务 {job.id}）{job.command[:_SUMMARY_CHARS]}",
            deliver_hint="这条命令之前超时转入了后台，现在已经结束；不要再重复执行同一条命令。",
            output=output or "（没有输出）",
            error=error,
            group_id=job.group_id,
            user_id=job.user_id,
            user_name=job.user_name,
            adapter_type=job.adapter_type,
            caller_is_developer=job.caller_is_developer,
            self_initiated=job.self_initiated,
            before_tool=before_tool,
        )
        if isinstance(payload, dict) and payload.get("status") == "deferred":
            job.callback_state = "pending"
            return False
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            raise ValueError("后台回调未确认处理完成")
    except asyncio.CancelledError:
        job.callback_state = "uncertain" if unsafe else "pending"
        raise
    except Exception as exc:
        job.callback_attempts += 1
        job.callback_state = (
            "uncertain"
            if unsafe
            else "failed"
            if job.callback_attempts >= _CALLBACK_MAX_ATTEMPTS
            else "pending"
        )
        logger.warning("后台 Bash 回调失败 (%s, %s): %s", job.id, job.callback_state, exc)
        return True
    job.callback_state = "completed"
    return True


def _prune(owner: str) -> None:
    """只保留最近若干条已结束且已回调的任务，连同它们的日志。"""
    done = [
        job
        for job in list_jobs(owner)
        if job.finished and job.callback_state not in {"pending", "running"}
    ]
    board = _boards.get(owner)
    for job in done[:-_MAX_RETAINED_FINISHED] if len(done) > _MAX_RETAINED_FINISHED else []:
        _jobs.pop(job.id, None)
        if board is not None:
            board.delete(job.id)
        try:
            job.log_path.unlink(missing_ok=True)
        except OSError:
            pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
