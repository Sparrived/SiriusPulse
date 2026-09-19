"""被动后台任务的启动时序。

这类任务在引擎**构造期**就被 ``asyncio.create_task`` 建好，而宿主把
``running_check`` 置为真要等构造结束。事件循环会在构造期剩余的 ``await``
里首次调度这些任务，如果此时检查为假就返回，任务会被永久地丢掉——没有任何
日志，也没有任何异常，表现为"注册了却从来不跑"。
"""

from __future__ import annotations

import asyncio

import pytest

from sirius_pulse.extension_runtime import BackgroundTaskSpec


class _Host:
    """占位宿主，``running`` 模拟引擎的 ``_bg_running``。"""

    def __init__(self) -> None:
        self.running = False


@pytest.mark.asyncio
async def test_background_task_registered_before_host_starts_still_runs():
    """宿主尚未就绪时注册的任务，就绪后必须照常运行。"""
    host = _Host()
    beats: list[int] = []

    async def beat() -> None:
        beats.append(1)

    spec = BackgroundTaskSpec(name="tick", interval_seconds=0.01, task_func=beat)
    task = asyncio.create_task(spec.run_loop(lambda: host.running))

    # 构造期仍在进行：此刻宿主还没起来，事件循环已经拿到了这个任务。
    await asyncio.sleep(0.05)
    assert beats == [], "宿主未就绪时不该执行"

    host.running = True
    await asyncio.sleep(0.1)

    assert beats, "宿主就绪后任务应当开始运行，而不是已经退出"
    assert not task.done(), "任务不应在首次检查为假时就结束"

    host.running = False
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_background_task_stops_when_host_reports_stopped():
    """宿主停止后循环要退出，不能继续空转。"""
    host = _Host()
    host.running = True
    beats: list[int] = []

    async def beat() -> None:
        beats.append(1)

    spec = BackgroundTaskSpec(name="tick", interval_seconds=0.01, task_func=beat)
    task = asyncio.create_task(spec.run_loop(lambda: host.running))

    await asyncio.sleep(0.05)
    assert beats

    host.running = False
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
