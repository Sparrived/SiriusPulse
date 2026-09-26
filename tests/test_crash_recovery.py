"""崩溃与故障恢复：WS 重连不放弃、连接健康可观测、孤儿子进程回收。

对应评估报告 §3.3。三个问题的共同特征是「故障发生了但没人知道」：
适配器永久离线而心跳照写、UI 仍显示运行中；孤儿进程堆在 PID 表里。
"""

import asyncio
import os

import pytest

from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter
from sirius_pulse.utils.child_reaper import (
    child_reaper_loop,
    reap_orphaned_children,
    start_child_reaper,
)


def test_reconnect_loop_never_gives_up_by_default():
    """默认不允许「重连次数耗尽后永久离线」。

    这是 §3.3 的核心：放弃重连后进程照常写心跳，但再也收不到消息，
    运维无从发现。默认值必须表达「一直重试」。
    """
    assert NapCatAdapter._MAX_RECONNECT_ATTEMPTS == 0


@pytest.mark.asyncio
async def test_reconnect_loop_keeps_retrying_after_many_failures(tmp_path, monkeypatch):
    """连续失败远超旧的 5 次上限后，循环仍在继续重试。"""
    adapter = NapCatAdapter("ws://example.invalid", work_path=tmp_path)
    adapter.reconnect_interval = 0.01
    adapter._RECONNECT_BASE_DELAY = 0.001

    attempts = 0

    async def _always_fail() -> bool:
        nonlocal attempts
        attempts += 1
        if attempts >= 12:  # 远超旧上限 5
            adapter._running = False
        return False

    monkeypatch.setattr(adapter, "_connect_once", _always_fail)
    adapter._running = True

    await asyncio.wait_for(adapter._reconnect_loop(), timeout=5)

    assert attempts >= 12, "重连循环不应在 5 次失败后停止"


@pytest.mark.asyncio
async def test_adapter_reports_offline_state_and_recovers(tmp_path, monkeypatch):
    """断线期间连接状态可读；恢复连接后状态归零。"""
    adapter = NapCatAdapter("ws://example.invalid", work_path=tmp_path)
    assert adapter.connection_state()["online"] is False

    class _OpenWS:
        closed = False

    adapter.ws = _OpenWS()  # type: ignore[assignment]
    online = adapter.connection_state()
    assert online["online"] is True
    assert online["reconnect_exhausted"] is False


@pytest.mark.asyncio
async def test_reconnect_loop_clears_offline_marker_after_reconnect(tmp_path, monkeypatch):
    """重连成功后离线起点被清空，避免把已恢复的适配器报成离线。"""
    adapter = NapCatAdapter("ws://example.invalid", work_path=tmp_path)
    adapter._RECONNECT_BASE_DELAY = 0.001
    adapter.reconnect_interval = 0.001

    calls = 0

    async def _fail_then_succeed() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        adapter._running = False
        return True

    class _OpenWS:
        closed = False

    async def _fake_listen() -> None:
        return None

    monkeypatch.setattr(adapter, "_connect_once", _fail_then_succeed)
    monkeypatch.setattr(adapter, "_listen_loop", _fake_listen)
    adapter._running = True

    await asyncio.wait_for(adapter._reconnect_loop(), timeout=5)

    assert adapter._offline_since is None


def test_start_child_reaper_skips_non_pid1(monkeypatch):
    """非 PID 1 不启动回收，避免抢走其他代码正要 wait() 的子进程。"""
    monkeypatch.setattr(os, "getpid", lambda: 4321)
    assert start_child_reaper() is None


def test_start_child_reaper_skips_windows(monkeypatch):
    import sirius_pulse.utils.child_reaper as module

    monkeypatch.setattr(module.sys, "platform", "win32")
    assert start_child_reaper() is None


def test_reap_orphaned_children_returns_empty_on_windows(monkeypatch):
    import sirius_pulse.utils.child_reaper as module

    monkeypatch.setattr(module.sys, "platform", "win32")
    assert reap_orphaned_children() == []


@pytest.mark.asyncio
async def test_child_reaper_loop_reaps_and_logs(monkeypatch, caplog):
    """回收循环每轮调用 waitpid，并在回收成功时记录数量。"""
    import sirius_pulse.utils.child_reaper as module

    reaped_batches = [[101, 102], [], [303]]
    calls = 0

    def _fake_reap() -> list[int]:
        nonlocal calls
        batch = reaped_batches[calls] if calls < len(reaped_batches) else []
        calls += 1
        if calls > len(reaped_batches):
            raise asyncio.CancelledError
        return batch

    monkeypatch.setattr(module, "reap_orphaned_children", _fake_reap)

    with pytest.raises(asyncio.CancelledError):
        await child_reaper_loop(interval=0)

    assert calls > len(reaped_batches)


def test_reap_orphaned_children_stops_when_no_children(monkeypatch):
    """没有子进程时 ChildProcessError 是正常终止条件，不应抛出。"""
    import sirius_pulse.utils.child_reaper as module

    def _no_children(_pid: int, _flags: int):
        raise ChildProcessError

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.os, "waitpid", _no_children)
    assert reap_orphaned_children() == []


def test_reap_orphaned_children_collects_until_none_left(monkeypatch):
    """wnohang 返回 0 表示本轮已清空，应停止循环。"""
    import sirius_pulse.utils.child_reaper as module

    sequence = [(11, 0), (12, 0), (0, 0)]
    calls = 0

    def _waitpid(_pid: int, _flags: int):
        nonlocal calls
        result = sequence[calls]
        calls += 1
        return result

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.os, "waitpid", _waitpid)
    assert reap_orphaned_children() == [11, 12]
