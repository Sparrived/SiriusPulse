"""业务视角：所有「到点要做的事」共用一张到期任务表和一个 ticker。

- 登记一件马上到期的事，ticker 立刻醒来处理，不用等哪个轮询间隔；
- 处理不掉的（平台没确认、会话正忙）隔一会儿再试，不会原地空转；
- 慢的一类（生图）不挡住别的类（提醒）；
- 周期性心跳照固定间隔跳；
- 旧版本存在 JSON 里的任务第一次读表时搬进来，只搬一次；
- bash 的 crontab 和插件定时任务用同一套 cron 规则算「下一次」。
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sirius_pulse.plugins.scheduler import PluginScheduler
from sirius_pulse.scheduling import cron
from sirius_pulse.scheduling.due_jobs import DueJobBoard, DueJobKind, DueJobTicker
from sirius_pulse.tools.builtin import reminder
from sirius_pulse.tools.data_store import ToolDataStore

LOCAL = timezone(timedelta(hours=8))


class _Running:
    def __init__(self) -> None:
        self.on = True

    def __call__(self) -> bool:
        return self.on


async def _run_ticker(board: DueJobBoard, kinds: list[DueJobKind], body) -> None:
    """起一个 ticker（空闲时一睡就是一分钟，证明处理靠唤醒而不是轮询），跑完 body 再停。"""
    running = _Running()
    ticker = DueJobTicker(board, kinds, max_idle_seconds=60)
    task = asyncio.create_task(ticker.run(running))
    try:
        await asyncio.sleep(0.05)
        await body()
    finally:
        running.on = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_job_added_from_a_tool_thread_is_handled_at_once(tmp_path: Path) -> None:
    board = DueJobBoard(tmp_path / "persona.db")
    handled = asyncio.Event()

    async def handle() -> None:
        for job in board.jobs("note"):
            if job.due_at is not None:
                board.set_due(job.id, None)
                handled.set()

    async def body() -> None:
        # 工具的 run() 在线程里执行：从别的线程登记也要能叫醒事件循环上的 ticker。
        thread = threading.Thread(
            target=board.put, args=("note", "n1", {"text": "hi"}), kwargs={"due_at": time.time()}
        )
        thread.start()
        thread.join()
        await asyncio.wait_for(handled.wait(), timeout=1)

    await _run_ticker(board, [DueJobKind(kind="note", handler=handle)], body)
    assert board.get("n1").due_at is None


@pytest.mark.asyncio
async def test_an_undeliverable_job_is_retried_later_instead_of_spinning(tmp_path: Path) -> None:
    board = DueJobBoard(tmp_path / "persona.db")
    calls: list[float] = []

    async def handle() -> None:
        calls.append(time.monotonic())  # 平台没确认：什么都没改，行仍然到期。

    async def body() -> None:
        board.put("note", "n1", {}, due_at=time.time())
        await asyncio.sleep(0.35)

    await _run_ticker(board, [DueJobKind(kind="note", handler=handle, retry_seconds=0.2)], body)
    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.18


@pytest.mark.asyncio
async def test_a_slow_kind_does_not_hold_up_another_kind(tmp_path: Path) -> None:
    board = DueJobBoard(tmp_path / "persona.db")
    release = asyncio.Event()
    reminded = asyncio.Event()

    async def draw() -> None:
        await release.wait()  # 一张图画几分钟
        board.set_due("img", None)

    async def remind() -> None:
        board.set_due("r1", None)
        reminded.set()

    async def body() -> None:
        board.put("image", "img", {}, due_at=time.time())
        await asyncio.sleep(0.05)
        board.put("reminder", "r1", {}, due_at=time.time())
        await asyncio.wait_for(reminded.wait(), timeout=1)
        release.set()

    await _run_ticker(
        board,
        [DueJobKind(kind="image", handler=draw), DueJobKind(kind="reminder", handler=remind)],
        body,
    )


@pytest.mark.asyncio
async def test_a_heartbeat_beats_on_its_interval_but_not_before(tmp_path: Path) -> None:
    board = DueJobBoard(tmp_path / "persona.db")
    beats: list[float] = []

    async def beat() -> None:
        beats.append(time.monotonic())

    started = time.monotonic()

    async def body() -> None:
        await asyncio.sleep(0.5)

    await _run_ticker(board, [DueJobKind(kind="life", handler=beat, every_seconds=0.2)], body)
    assert 2 <= len(beats) <= 3
    assert beats[0] - started >= 0.18


@pytest.mark.asyncio
async def test_reminders_left_in_the_old_json_store_are_moved_once(tmp_path: Path) -> None:
    store = ToolDataStore(tmp_path / "tool_data" / "reminder.json")
    store.set("max_pending_per_chat", 5)
    store.set(
        "reminders",
        [
            {
                "id": "r-legacy01",
                "status": "pending",
                "group_id": "g1",
                "message": "该交作业啦",
                "repeat": "none",
                "due_at": "2026-11-15T12:00:00+00:00",
            }
        ],
    )
    store.save()

    listed = await reminder.run(
        "list",
        data_store=ToolDataStore(store.store_path),
        chat_context={"group_id": "g1"},
        now=datetime(2026, 11, 15, 10, 0, tzinfo=LOCAL),
    )
    again = await reminder.run(
        "list",
        data_store=ToolDataStore(store.store_path),
        chat_context={"group_id": "g1"},
        now=datetime(2026, 11, 15, 10, 0, tzinfo=LOCAL),
    )

    assert [r["id"] for r in listed["data"]["reminders"]] == ["r-legacy01"]
    assert [r["id"] for r in again["data"]["reminders"]] == ["r-legacy01"]
    on_disk = ToolDataStore(store.store_path)
    assert on_disk.get("reminders") is None
    assert on_disk.get("max_pending_per_chat") == 5  # 配置原样留着
    moved = DueJobBoard(tmp_path / "persona.db").get("r-legacy01")
    assert moved.kind == "reminder"
    assert moved.due_at == datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize(
    ("expression", "after", "expected"),
    [
        # 周五 9 点之后的「工作日 8 点」是下周一 8 点。
        ("0 8 * * 1-5", datetime(2026, 11, 13, 9, 0), datetime(2026, 11, 16, 8, 0)),
        ("*/15 * * * *", datetime(2026, 11, 13, 9, 7, 30), datetime(2026, 11, 13, 9, 15)),
        # 日和周都受限时是「或」：13 号或者周日。
        ("30 9 13 * 0", datetime(2026, 11, 13, 10, 0), datetime(2026, 11, 15, 9, 30)),
        ("0 0 31 2 *", datetime(2026, 11, 13, 9, 0), None),
    ],
)
def test_next_fire_follows_local_cron_rules(expression, after, expected) -> None:
    fire = cron.next_fire(expression, after.replace(tzinfo=LOCAL))
    assert fire == (expected.replace(tzinfo=LOCAL) if expected else None)


def test_plugin_schedules_use_the_same_cron_rules() -> None:
    # 13 号是周五：「13 号或周日」在插件里也按标准 cron 的「或」命中，而不是「且」。
    friday_13th = datetime(2026, 11, 13, 9, 30, tzinfo=LOCAL).timestamp()
    assert PluginScheduler._check_simple_cron("30 9 13 * 0", friday_13th, 0.0)
    assert cron.cron_matches("30 9 13 * 0", datetime(2026, 11, 13, 9, 30, tzinfo=LOCAL))
