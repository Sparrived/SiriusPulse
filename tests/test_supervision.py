"""人格或后台循环挂掉后会自己爬起来，而不是悄悄停摆。"""

from __future__ import annotations

import asyncio
import logging

import pytest

from sirius_pulse.utils.supervision import run_with_restarts


@pytest.mark.asyncio
async def test_a_crashed_loop_is_logged_and_restarted_until_it_succeeds(caplog):
    attempts: list[int] = []
    rebuilt: list[int] = []

    async def flaky() -> None:
        attempts.append(len(attempts))
        if len(attempts) < 3:
            raise RuntimeError("调度库被锁住")

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(
            run_with_restarts(
                "后台任务 delayed_queue",
                flaky,
                should_continue=lambda: True,
                before_restart=lambda: rebuilt.append(1),
                initial_delay=0.01,
            ),
            timeout=2,
        )

    assert len(attempts) == 3
    assert len(rebuilt) == 2, "每次重启前都要换一个干净的实例"
    assert sum("异常退出" in r.message for r in caplog.records) == 2
    assert any(r.exc_info for r in caplog.records), "异常要带堆栈立刻进日志"


@pytest.mark.asyncio
async def test_shutting_down_during_the_backoff_cancels_the_restart():
    stop = asyncio.Event()
    attempts: list[int] = []

    async def crash() -> None:
        attempts.append(1)
        stop.set()
        raise RuntimeError("启动失败")

    await asyncio.wait_for(
        run_with_restarts(
            "人格「月白」",
            crash,
            should_continue=lambda: not stop.is_set(),
            stop_event=stop,
            initial_delay=60,
        ),
        timeout=2,
    )
    assert attempts == [1]


@pytest.mark.asyncio
async def test_backoff_grows_between_consecutive_failures(monkeypatch):
    waits: list[float] = []
    real_sleep = asyncio.sleep

    async def record_sleep(seconds: float) -> None:
        waits.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr("sirius_pulse.utils.supervision.asyncio.sleep", record_sleep)
    attempts: list[int] = []

    async def crash_four_times() -> None:
        attempts.append(1)
        if len(attempts) <= 4:
            raise RuntimeError("还是不行")

    await run_with_restarts(
        "后台任务 memory_checkpoint",
        crash_four_times,
        should_continue=lambda: True,
        initial_delay=5,
        max_delay=12,
    )
    assert waits == [5, 10, 12, 12]
