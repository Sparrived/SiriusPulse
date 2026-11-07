"""插件定时任务：声明的时刻、cron 星期与固定间隔在固定时钟下何时触发。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from sirius_pulse.life.clock import LOCAL_TIMEZONE
from sirius_pulse.plugins.base import PluginBase
from sirius_pulse.plugins.models import PluginDefinition
from sirius_pulse.plugins.scheduler import PluginScheduler, ScheduledTask


def _at(day: int, hour: int, minute: int, second: int = 0) -> float:
    """2026 年 11 月某日的本地（中国）时刻；11-08 是周日，11-09 是周一。"""
    return datetime(2026, 11, day, hour, minute, second, tzinfo=LOCAL_TIMEZONE).timestamp()


def _scheduler_for(plugin_cls: type[PluginBase], fired: list[str]) -> PluginScheduler:
    """像运行时那样，把插件声明的定时事件注册进调度器。"""
    scheduler = PluginScheduler()
    definition = PluginDefinition.from_class(plugin_cls)
    for event in definition.events:

        async def callback(event_type: str = event.type) -> None:
            fired.append(event_type)

        scheduler.add_task(
            ScheduledTask(
                name=f"{definition.name}:{event.type}",
                plugin_name=definition.name,
                cron=event.cron,
                interval_seconds=event.interval_seconds,
                callback=callback,
            )
        )
    return scheduler


async def _tick(scheduler: PluginScheduler, now: float) -> None:
    await asyncio.gather(*scheduler.run_due(now))


def _cron_task(cron: str, fired: list[Any]) -> ScheduledTask:
    async def callback() -> None:
        fired.append(cron)

    return ScheduledTask(name="weekly", plugin_name="demo", cron=cron, callback=callback)


class _MorningPlugin(PluginBase):
    _plugin_name = "morning"
    _plugin_schedule = [{"time": "08:30", "duration": 30}]


@pytest.mark.asyncio
async def test_daily_schedule_fires_only_at_its_local_time_and_not_at_startup():
    fired: list[str] = []
    scheduler = _scheduler_for(_MorningPlugin, fired)

    # 启动于 07:00，之后每 10 秒检查一次：08:30 之前一次都不跑
    for now in (_at(9, 7, 0), _at(9, 7, 30, 10), _at(9, 8, 29, 50)):
        await _tick(scheduler, now)
    assert fired == []

    # 08:30 这一分钟只触发一次
    await _tick(scheduler, _at(9, 8, 30, 0))
    await _tick(scheduler, _at(9, 8, 30, 10))
    assert fired == ["timer.schedule"]

    # duration 不是重复间隔：30 分钟后不再触发，直到第二天 08:30
    for now in (_at(9, 9, 0), _at(9, 20, 0), _at(10, 8, 29, 50)):
        await _tick(scheduler, now)
    assert fired == ["timer.schedule"]
    await _tick(scheduler, _at(10, 8, 30, 5))
    assert fired == ["timer.schedule", "timer.schedule"]


@pytest.mark.asyncio
async def test_daily_schedule_started_inside_its_minute_still_fires_once():
    fired: list[str] = []
    scheduler = _scheduler_for(_MorningPlugin, fired)

    await _tick(scheduler, _at(9, 8, 30, 40))
    await _tick(scheduler, _at(9, 8, 30, 50))

    assert fired == ["timer.schedule"]


@pytest.mark.asyncio
@pytest.mark.parametrize("weekday", ["0", "7", "0,3", "5-7", "*/2"])
async def test_sunday_cron_fires_on_sunday_however_it_is_written(weekday: str):
    fired: list[str] = []
    scheduler = PluginScheduler()
    scheduler.add_task(_cron_task(f"0 9 * * {weekday}", fired))

    await _tick(scheduler, _at(8, 9, 0))

    assert len(fired) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("weekday", "fires_on"),
    [("1", 9), ("6", 14), ("1-5", 13)],
    ids=["monday", "saturday", "weekdays"],
)
async def test_weekday_cron_fires_on_its_day_and_not_on_sunday(weekday: str, fires_on: int):
    fired: list[str] = []
    scheduler = PluginScheduler()
    scheduler.add_task(_cron_task(f"0 9 * * {weekday}", fired))

    await _tick(scheduler, _at(8, 9, 0))
    assert fired == []

    await _tick(scheduler, _at(fires_on, 9, 0))
    assert len(fired) == 1


class _HeartbeatPlugin(PluginBase):
    _plugin_name = "heartbeat"
    _plugin_events = [{"type": "timer.heartbeat", "interval_seconds": 60}]


@pytest.mark.asyncio
async def test_interval_event_fires_at_startup_then_every_interval():
    fired: list[str] = []
    scheduler = _scheduler_for(_HeartbeatPlugin, fired)
    start = _at(9, 7, 0, 5)

    await _tick(scheduler, start)
    assert fired == ["timer.heartbeat"]

    await _tick(scheduler, start + 59)
    assert fired == ["timer.heartbeat"]

    await _tick(scheduler, start + 60)
    assert fired == ["timer.heartbeat", "timer.heartbeat"]
