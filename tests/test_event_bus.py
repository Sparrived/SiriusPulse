"""SessionEventBus 异步事件总线测试。"""
from __future__ import annotations

import asyncio

import pytest

from sirius_pulse.core.events import SessionEvent, SessionEventBus, SessionEventType


class TestEventBusBasic:
    """基础发布/订阅测试。"""

    @pytest.mark.asyncio
    async def test_emit_and_subscribe(self):
        bus = SessionEventBus()
        events: list[SessionEvent] = []

        async def collector():
            async for event in bus.subscribe():
                events.append(event)

        task = asyncio.create_task(collector())
        await asyncio.sleep(0.01)

        event = SessionEvent(type=SessionEventType.PERCEPTION_COMPLETED, data={"key": "value"})
        await bus.emit(event)
        await asyncio.sleep(0.01)

        await bus.close()
        await task

        assert len(events) == 1
        assert events[0].type == SessionEventType.PERCEPTION_COMPLETED
        assert events[0].data["key"] == "value"

    @pytest.mark.asyncio
    async def test_multiple_subscribers_receive_same_event(self):
        bus = SessionEventBus()
        results: list[list[SessionEvent]] = [[], []]

        async def collector(idx: int):
            async for event in bus.subscribe():
                results[idx].append(event)

        task0 = asyncio.create_task(collector(0))
        task1 = asyncio.create_task(collector(1))
        await asyncio.sleep(0.01)

        await bus.emit(SessionEvent(type=SessionEventType.COGNITION_COMPLETED))
        await asyncio.sleep(0.01)

        await bus.close()
        await asyncio.gather(task0, task1)

        assert len(results[0]) == 1
        assert len(results[1]) == 1
        assert results[0][0].type == SessionEventType.COGNITION_COMPLETED
        assert results[1][0].type == SessionEventType.COGNITION_COMPLETED


class TestEventBusClose:
    """关闭行为测试。"""

    @pytest.mark.asyncio
    async def test_close_terminates_subscribers(self):
        bus = SessionEventBus()
        events: list[SessionEvent] = []

        async def collector():
            async for event in bus.subscribe():
                events.append(event)

        task = asyncio.create_task(collector())
        await asyncio.sleep(0.01)

        await bus.emit(SessionEvent(type=SessionEventType.DECISION_COMPLETED))
        await asyncio.sleep(0.01)
        await bus.close()
        await task

        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_emit_after_close_is_noop(self):
        bus = SessionEventBus()
        await bus.close()

        await bus.emit(SessionEvent(type=SessionEventType.EXECUTION_COMPLETED))

    @pytest.mark.asyncio
    async def test_closed_property(self):
        bus = SessionEventBus()
        assert not bus.closed
        await bus.close()
        assert bus.closed


class TestEventBusProperties:
    """属性测试。"""

    @pytest.mark.asyncio
    async def test_subscriber_count(self):
        bus = SessionEventBus()
        assert bus.subscriber_count == 0

        async def collector():
            async for event in bus.subscribe():
                pass

        task = asyncio.create_task(collector())
        await asyncio.sleep(0.01)
        assert bus.subscriber_count == 1

        await bus.close()
        await task

    @pytest.mark.asyncio
    async def test_event_timestamp_auto_set(self):
        bus = SessionEventBus()
        event = SessionEvent(type=SessionEventType.PERCEPTION_COMPLETED)
        assert event.timestamp > 0

        await bus.close()


class TestEventBusQueueOverflow:
    """队列溢出测试。"""

    @pytest.mark.asyncio
    async def test_full_queue_drops_event(self):
        bus = SessionEventBus()
        events: list[SessionEvent] = []

        async def slow_collector():
            async for event in bus.subscribe(max_queue_size=2):
                events.append(event)

        task = asyncio.create_task(slow_collector())
        await asyncio.sleep(0.01)

        for i in range(10):
            await bus.emit(SessionEvent(type=SessionEventType.PERCEPTION_COMPLETED, data={"i": i}))
        await asyncio.sleep(0.05)

        await bus.close()
        await task

        assert len(events) <= 10
