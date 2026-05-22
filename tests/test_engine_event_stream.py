"""Tests for EmotionalGroupChatEngine event stream integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from sirius_pulse.core.events import SessionEventType
from sirius_pulse.models.models import Message, Participant
from sirius_pulse.providers.mock import MockProvider


class TestEventStream:
    @pytest.mark.asyncio
    async def test_process_message_emits_pipeline_events(self, engine_factory):
        engine = engine_factory()
        events = []

        async def collector():
            async for event in engine.event_bus.subscribe():
                events.append(event)

        task = asyncio.create_task(collector())
        await asyncio.sleep(0)  # let subscriber register

        p = Participant(name="u1", user_id="u1")
        await engine.process_message(
            Message(role="human", content="大家好", speaker="u1"),
            [p], "group_a",
        )

        # Give event bus time to deliver
        await asyncio.sleep(0.05)
        await engine.event_bus.close()
        await task

        types = [e.type for e in events]
        assert SessionEventType.PERCEPTION_COMPLETED in types
        assert SessionEventType.COGNITION_COMPLETED in types
        assert SessionEventType.DECISION_COMPLETED in types
        assert SessionEventType.EXECUTION_COMPLETED in types

    @pytest.mark.asyncio
    async def test_decision_event_contains_strategy(self, engine_factory):
        # Use a different persona name to get a fresh engine instance
        from sirius_pulse.models.persona import PersonaProfile
        engine = engine_factory(persona=PersonaProfile(name="DecisionTestBot"))
        events = []

        async def collector():
            async for event in engine.event_bus.subscribe():
                events.append(event)

        task = asyncio.create_task(collector())
        await asyncio.sleep(0)  # let subscriber register

        p = Participant(name="u1", user_id="u1")
        await engine.process_message(
            Message(role="human", content="崩溃了！救命！", speaker="u1"),
            [p], "group_a",
        )

        await asyncio.sleep(0.05)
        await engine.event_bus.close()
        await task

        decision_event = next(
            (e for e in events if e.type == SessionEventType.DECISION_COMPLETED),
            None,
        )
        assert decision_event is not None
        assert decision_event.data["group_id"] == "group_a"
        assert decision_event.data["strategy"] in ("immediate", "delayed", "silent")

    @pytest.mark.asyncio
    async def test_proactive_check_emits_event(self, engine_factory):
        provider = MockProvider(responses=[" proactively!"])
        engine = engine_factory(
            provider_async=provider,
            config={"expressiveness": 1.0, "sensitivity": 0.0},
        )
        events = []

        async def collector():
            async for event in engine.event_bus.subscribe():
                events.append(event)

        task = asyncio.create_task(collector())
        await asyncio.sleep(0)  # let subscriber register

        # Fake a last message time far in the past to force proactive trigger
        engine._group_last_message_at["group_a"] = "2026-04-01T00:00:00+00:00"
        result = await engine.proactive_check("group_a", _now=datetime(2026, 5, 3, 15, 0, 0, tzinfo=timezone.utc))

        await asyncio.sleep(0.05)
        await engine.event_bus.close()
        await task

        assert result is not None, "proactive_check should trigger with old last_message_at"
        assert result["strategy"] == "proactive"
        proactive_events = [e for e in events if e.type == SessionEventType.PROACTIVE_RESPONSE_TRIGGERED]
        assert len(proactive_events) >= 1
        assert proactive_events[0].data["group_id"] == "group_a"

    @pytest.mark.asyncio
    async def test_multiple_messages_in_order(self, engine_factory):
        engine = engine_factory()
        events = []

        async def collector():
            async for event in engine.event_bus.subscribe():
                events.append(event)

        task = asyncio.create_task(collector())
        await asyncio.sleep(0)  # let subscriber register

        p = Participant(name="u1", user_id="u1")
        await engine.process_message(
            Message(role="human", content="msg1", speaker="u1"), [p], "group_a",
        )
        await engine.process_message(
            Message(role="human", content="msg2", speaker="u1"), [p], "group_a",
        )

        await asyncio.sleep(0.05)
        await engine.event_bus.close()
        await task

        # Each message produces 4 pipeline events
        assert len(events) >= 8
        # Events should be in order: perception -> cognition -> decision -> execution
        for i in range(0, len(events), 4):
            if i + 3 < len(events):
                assert events[i].type == SessionEventType.PERCEPTION_COMPLETED
                assert events[i + 1].type == SessionEventType.COGNITION_COMPLETED
                assert events[i + 2].type == SessionEventType.DECISION_COMPLETED
                assert events[i + 3].type == SessionEventType.EXECUTION_COMPLETED
