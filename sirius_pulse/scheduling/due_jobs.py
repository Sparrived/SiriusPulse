"""到期任务表（``persona.db`` 的 ``due_jobs``）与唯一的 ticker。

以前每类后台事务各有一个 ``BackgroundTaskSpec`` 轮询循环和一份 JSON 存储：提醒、
``wake_me``、bash 的 crontab、转后台的 bash 命令、生图队列、视觉自动候选、生活心跳，
七个循环各按自己的间隔醒来，重复写着「读整表 → 找到点的 → 改状态 → 写回」。现在：

- **一张表**：每行是一件事，``kind`` 说它归谁管，``due_at`` 是下次要看它的时刻
  （``NULL`` 表示眼下不用管：已结束，或在等外部结果），``payload`` 是该类任务自己的 JSON；
- **一个 ticker**：每个人格一个 :class:`DueJobTicker`，睡到最近的 ``due_at``；任何一处
  写入新的到期时间都会立刻唤醒它（跨线程安全），不再按固定间隔空转；
- **按类登记处理器**：工具的 ``create_background_tasks(ctx)`` 返回 :class:`DueJobKind`，
  某类有行到期时 ticker 调它的 ``handler``。同一类同时只跑一个处理器（和以前一个循环
  一类的串行语义一样），不同类互不阻塞——慢的生图不会挡住提醒；
- **周期性心跳**（生活心跳、视觉自动候选）也是表里的一行：``every_seconds`` 的类由 ticker
  自己维护那一行的下次时间；
- **迁移**：:meth:`DueJobBoard.import_legacy` 把旧 JSON 存储里的记录搬进表并清掉旧键，
  工具每次读表前调用，读到空键时几乎不花时间。

表的读写都很小（单行或百来行以内），但 ticker 和异步路径仍然经 ``asyncio.to_thread``
访问，免得一次锁等待卡住所有人格共用的事件循环。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "DueJob",
    "DueJobBoard",
    "DueJobKind",
    "DueJobTicker",
    "board_for_dir",
    "board_for_store",
    "db_path_for_store",
]

#: 没有任何到期任务时 ticker 最多睡多久；也兜住别的进程（WebUI）直接改表的情况。
MAX_IDLE_SECONDS = 300.0
#: 宿主还没起来时，ticker 隔多久再看一次。
_STARTUP_POLL_SECONDS = 0.1
#: 周期性心跳在表里占的那一行的 ID 前缀。
EVERY_PREFIX = "every:"

_KEEP: Any = object()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS due_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    due_at REAL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_due_jobs_due ON due_jobs(due_at) WHERE due_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_due_jobs_kind ON due_jobs(kind, created_at);
"""


def _timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


@dataclass(slots=True)
class DueJob:
    """表里的一行。``payload`` 是拷贝，改了要经 :meth:`DueJobBoard.patch` 写回。"""

    id: str
    kind: str
    due_at: float | None
    payload: dict[str, Any]
    created_at: float
    updated_at: float


class DueJobBoard:
    """一个人格的到期任务表：线程安全的增删改查，外加「有新的到期时间」唤醒。"""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, timeout=10, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._depth = 0
        self._listeners: dict[int, Callable[[], None]] = {}
        self._next_listener = 0

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ------------------------------------------------------------------
    # 事务与唤醒
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[DueJobBoard]:
        """把多步读改写串成一个事务；可重入，最外层提交。"""
        with self._lock:
            outermost = self._depth == 0
            if outermost:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if outermost:
                    self._conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outermost:
                self._conn.execute("COMMIT")

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """登记一个「表里有新的到期时间」回调，返回取消登记的函数。回调必须线程安全。"""
        with self._lock:
            key = self._next_listener
            self._next_listener += 1
            self._listeners[key] = listener

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.pop(key, None)

        return unsubscribe

    def wake(self) -> None:
        with self._lock:
            listeners = list(self._listeners.values())
        for listener in listeners:
            try:
                listener()
            except RuntimeError:
                # 事件循环已经关了：那个 ticker 不会再跑。
                pass

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get(self, job_id: str) -> DueJob | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM due_jobs WHERE id = ?", (job_id,)).fetchone()
        return _row(row) if row is not None else None

    def jobs(self, kind: str | None = None) -> list[DueJob]:
        """按登记先后列出（某一类的）全部行。"""
        with self._lock:
            if kind is None:
                rows = self._conn.execute(
                    "SELECT * FROM due_jobs ORDER BY created_at, rowid"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM due_jobs WHERE kind = ? ORDER BY created_at, rowid", (kind,)
                ).fetchall()
        return [_row(row) for row in rows]

    def payloads(self, kind: str) -> list[dict[str, Any]]:
        return [job.payload for job in self.jobs(kind)]

    def due_kinds(self, now: float, kinds: Iterable[str]) -> list[str]:
        wanted = list(kinds)
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT DISTINCT kind FROM due_jobs WHERE due_at <= ? AND kind IN ({marks})",
                (now, *wanted),
            ).fetchall()
        return [str(row["kind"]) for row in rows]

    def next_due(self, kinds: Iterable[str]) -> float | None:
        wanted = list(kinds)
        if not wanted:
            return None
        marks = ",".join("?" for _ in wanted)
        with self._lock:
            row = self._conn.execute(
                f"SELECT MIN(due_at) AS due FROM due_jobs WHERE kind IN ({marks})", wanted
            ).fetchone()
        return None if row is None or row["due"] is None else float(row["due"])

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def put(
        self, kind: str, job_id: str, payload: dict[str, Any], *, due_at: Any = None
    ) -> DueJob:
        """新增或整行覆盖；覆盖时保留原来的登记时间。"""
        now = time.time()
        due = _timestamp(due_at)
        with self._lock:
            self._conn.execute(
                "INSERT INTO due_jobs (id, kind, due_at, payload, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET kind = excluded.kind, due_at = excluded.due_at,"
                " payload = excluded.payload, updated_at = excluded.updated_at",
                (job_id, kind, due, _dump(payload), now, now),
            )
        if due is not None:
            self.wake()
        return DueJob(job_id, kind, due, dict(payload), now, now)

    def patch(
        self, job_id: str, fields: dict[str, Any] | None = None, *, due_at: Any = _KEEP
    ) -> DueJob | None:
        """把 ``fields`` 合并进 payload，并可同时改下次到期时间；找不到返回 None。"""
        with self.transaction():
            job = self.get(job_id)
            if job is None:
                return None
            if fields:
                job.payload.update(fields)
            if due_at is not _KEEP:
                job.due_at = _timestamp(due_at)
            job.updated_at = time.time()
            self._conn.execute(
                "UPDATE due_jobs SET payload = ?, due_at = ?, updated_at = ? WHERE id = ?",
                (_dump(job.payload), job.due_at, job.updated_at, job_id),
            )
        if due_at is not _KEEP and job.due_at is not None:
            self.wake()
        return job

    def set_due(self, job_id: str, due_at: Any) -> None:
        due = _timestamp(due_at)
        with self._lock:
            self._conn.execute(
                "UPDATE due_jobs SET due_at = ?, updated_at = ? WHERE id = ?",
                (due, time.time(), job_id),
            )
        if due is not None:
            self.wake()

    def postpone_due(self, kind: str, now: float, until: float) -> int:
        """这一类里到 ``now`` 还没处理掉的行，下次在 ``until`` 再看。"""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE due_jobs SET due_at = ? WHERE kind = ? AND due_at <= ?",
                (until, kind, now),
            )
        return cursor.rowcount

    def replace(
        self, kind: str, rows: Iterable[tuple[str, dict[str, Any], Any]]
    ) -> None:
        """整类替换：给出的行写入（保留原登记时间与顺序），没给出的删掉。"""
        items = [(str(job_id), payload, _timestamp(due)) for job_id, payload, due in rows]
        now = time.time()
        with self.transaction():
            existing = {
                str(row["id"]): float(row["created_at"])
                for row in self._conn.execute(
                    "SELECT id, created_at FROM due_jobs WHERE kind = ?", (kind,)
                ).fetchall()
            }
            keep = {job_id for job_id, _payload, _due in items}
            for job_id in set(existing) - keep:
                self._conn.execute("DELETE FROM due_jobs WHERE id = ?", (job_id,))
            for offset, (job_id, payload, due) in enumerate(items):
                created = existing.get(job_id, now + offset * 1e-6)
                self._conn.execute(
                    "INSERT INTO due_jobs (id, kind, due_at, payload, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(id) DO UPDATE SET kind = excluded.kind,"
                    " due_at = excluded.due_at, payload = excluded.payload,"
                    " updated_at = excluded.updated_at",
                    (job_id, kind, due, _dump(payload), created, now),
                )
        if any(due is not None for _job_id, _payload, due in items):
            self.wake()

    def delete(self, job_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM due_jobs WHERE id = ?", (job_id,))
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # 迁移
    # ------------------------------------------------------------------

    def import_legacy(
        self,
        kind: str,
        data_store: Any,
        key: str,
        due_of: Callable[[dict[str, Any]], Any],
    ) -> int:
        """把旧 JSON 存储 ``data_store[key]`` 里的记录搬进表，然后删掉这个键。

        表里已有同 ID 的行时以表为准（迁移可能被打断过一次）。返回搬进来的条数。
        """
        if data_store is None:
            return 0
        lock = getattr(data_store, "lock", None)
        with lock if lock is not None else _NULL_LOCK:
            raw = data_store.get(key)
            if raw is None:
                return 0
            records = [
                item
                for item in (raw if isinstance(raw, list) else [])
                if isinstance(item, dict) and str(item.get("id") or "")
            ]
            imported = 0
            now = time.time()
            with self.transaction():
                for offset, record in enumerate(records):
                    cursor = self._conn.execute(
                        "INSERT OR IGNORE INTO due_jobs"
                        " (id, kind, due_at, payload, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            str(record["id"]),
                            kind,
                            _timestamp(due_of(record)),
                            _dump(record),
                            now + offset * 1e-6,
                            now,
                        ),
                    )
                    imported += cursor.rowcount
            data_store.delete(key)
            data_store.save()
        if imported:
            logger.info("已把 %d 条旧 %s 记录迁入 due_jobs 表", imported, kind)
            self.wake()
        return imported

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _NullLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


_NULL_LOCK = _NullLock()


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _row(row: sqlite3.Row) -> DueJob:
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    return DueJob(
        id=str(row["id"]),
        kind=str(row["kind"]),
        due_at=None if row["due_at"] is None else float(row["due_at"]),
        payload=payload if isinstance(payload, dict) else {},
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


# ---------------------------------------------------------------------------
# 每个 persona.db 一个表对象
# ---------------------------------------------------------------------------

_boards: dict[str, DueJobBoard] = {}
_boards_lock = threading.Lock()


def board_for_dir(persona_dir: Path | str) -> DueJobBoard:
    """人格目录下 ``persona.db`` 的到期任务表；同一进程里同一个库只开一份。"""
    path = Path(persona_dir) / "persona.db"
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path)
    with _boards_lock:
        board = _boards.get(key)
        if board is None:
            board = DueJobBoard(path)
            _boards[key] = board
        return board


def db_path_for_store(data_store: Any) -> Path:
    """工具数据存储所属人格的 ``persona.db``：``<人格目录>/tool_data/x.json`` → ``<人格目录>``。"""
    store_path = getattr(data_store, "store_path", None)
    if not store_path:
        raise ValueError("到期任务需要放在人格目录里的持久化数据存储")
    folder = Path(store_path).parent
    return (folder.parent if folder.name == "tool_data" else folder) / "persona.db"


def board_for_store(data_store: Any) -> DueJobBoard:
    return board_for_dir(db_path_for_store(data_store).parent)


# ---------------------------------------------------------------------------
# Ticker
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DueJobKind:
    """一类到期任务的处理方式，由工具的 ``create_background_tasks(ctx)`` 返回。

    Attributes:
        kind: 表里的 ``kind``。
        handler: 这一类有行到期时调用；自己从表里取到期的行处理，并把处理完的行的
            ``due_at`` 改掉（推到下一次，或清空）。
        data_store: 用来找到该人格的 ``persona.db``。
        retry_seconds: 处理器跑完后仍然到期（发送失败、会话正忙……）的行，隔这么久再看。
        every_seconds: 设了就是周期性心跳：ticker 自己维护表里的 ``every:<kind>`` 一行，
            每隔这么久调一次 ``handler``（第一次也要等满一个间隔）。
        on_start: ticker 起来后、第一次分派前调用一次（恢复上个进程中断的任务）。
    """

    kind: str
    handler: Callable[[], Awaitable[Any]]
    data_store: Any = None
    retry_seconds: float = 15.0
    every_seconds: float | None = None
    on_start: Callable[[], Awaitable[Any]] | None = None
    name: str = field(default="")

    @property
    def label(self) -> str:
        return self.name or self.kind


class DueJobTicker:
    """一个人格唯一的后台调度循环：睡到最近的到期时刻，或被新任务唤醒。"""

    def __init__(
        self,
        board: DueJobBoard,
        kinds: Iterable[DueJobKind],
        *,
        clock: Callable[[], float] = time.time,
        max_idle_seconds: float = MAX_IDLE_SECONDS,
    ) -> None:
        self._board = board
        self._kinds = {spec.kind: spec for spec in kinds}
        self._clock = clock
        self._max_idle = max_idle_seconds
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._event: asyncio.Event | None = None

    @property
    def kinds(self) -> list[str]:
        return list(self._kinds)

    async def run(self, running_check: Callable[[], bool]) -> None:
        """直到 ``running_check`` 为假一直调度；宿主就绪前先等着，不提前退出。"""
        while not running_check():
            await asyncio.sleep(_STARTUP_POLL_SECONDS)
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        self._event = event

        def _notify() -> None:
            loop.call_soon_threadsafe(event.set)

        unsubscribe = self._board.subscribe(_notify)
        try:
            await self._start()
            while running_check():
                event.clear()
                await self.dispatch_due()
                idle = [kind for kind in self._kinds if kind not in self._inflight]
                next_at = await asyncio.to_thread(self._board.next_due, idle)
                timeout = self._max_idle
                if next_at is not None:
                    timeout = min(self._max_idle, max(0.0, next_at - self._clock()))
                try:
                    await asyncio.wait_for(event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
        finally:
            unsubscribe()
            pending = [task for task in self._inflight.values() if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._inflight.clear()

    async def _start(self) -> None:
        for spec in self._kinds.values():
            if spec.on_start is not None:
                try:
                    await spec.on_start()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Background task '%s' failed to recover", spec.label)
            if spec.every_seconds:
                await asyncio.to_thread(
                    self._board.put,
                    spec.kind,
                    f"{EVERY_PREFIX}{spec.kind}",
                    {"every_seconds": spec.every_seconds},
                    due_at=self._clock() + spec.every_seconds,
                )

    async def dispatch_due(self) -> list[asyncio.Task[None]]:
        """给每个有到期行、且没有处理器在跑的类各起一个处理任务。"""
        idle = [kind for kind in self._kinds if kind not in self._inflight]
        due = await asyncio.to_thread(self._board.due_kinds, self._clock(), idle)
        started = []
        for kind in due:
            task = asyncio.create_task(self._run_kind(self._kinds[kind]), name=f"due_job:{kind}")
            self._inflight[kind] = task
            started.append(task)
        return started

    async def _run_kind(self, spec: DueJobKind) -> None:
        try:
            if spec.every_seconds:
                await asyncio.to_thread(
                    self._board.set_due,
                    f"{EVERY_PREFIX}{spec.kind}",
                    self._clock() + spec.every_seconds,
                )
            await spec.handler()
        except asyncio.CancelledError:
            self._inflight.pop(spec.kind, None)
            raise
        except Exception:
            logger.exception("Background task '%s' failed", spec.label)
        try:
            # 处理器跑完仍然到期的（投递失败、会话正忙、处理器出错），过一会儿再看，
            # 不能让 ticker 原地空转。
            now = self._clock()
            await asyncio.to_thread(
                self._board.postpone_due, spec.kind, now, now + spec.retry_seconds
            )
        except Exception:
            logger.exception("推迟到期任务失败: %s", spec.kind)
        finally:
            self._inflight.pop(spec.kind, None)
            if self._event is not None:
                self._event.set()
