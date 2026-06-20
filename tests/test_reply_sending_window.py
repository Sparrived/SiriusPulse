from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase
from sirius_pulse.models.models import Message
from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter


@pytest.mark.asyncio
async def test_napcat_multiline_reply_waits_between_parts_and_marks_active(monkeypatch):
    adapter = NapCatAdapter(
        "ws://example.invalid",
        config={
            "human_reply_chars_per_second": 10,
            "human_reply_min_delay_seconds": 0.5,
            "human_reply_max_delay_seconds": 2.0,
        },
    )
    sent: list[object] = []
    active_snapshots: list[bool] = []
    slept: list[float] = []

    async def fake_send_group_msg(group_id, message):
        active_snapshots.append(adapter._is_reply_send_active("100"))
        sent.append(message)
        return {"ok": True}

    async def fake_sleep(seconds):
        active_snapshots.append(adapter._is_reply_send_active("100"))
        slept.append(seconds)

    adapter.send_group_msg = fake_send_group_msg  # type: ignore[method-assign]
    monkeypatch.setattr(
        "sirius_pulse.platforms.onebot_v11.napcat.adapter.asyncio.sleep",
        fake_sleep,
    )

    ok = await adapter._send_group_text("100", "短\n1234567890")

    assert ok is True
    assert len(sent) == 2
    assert slept == [pytest.approx(1.0)]
    assert active_snapshots and all(active_snapshots)
    assert adapter._is_reply_send_active("100") is False


@pytest.mark.asyncio
async def test_napcat_marks_group_message_when_received_during_reply_send():
    adapter = NapCatAdapter("ws://example.invalid")
    adapter._engine = SimpleNamespace(is_ready=lambda: True)
    seen_events: list[dict] = []

    async def fake_process_event(event):
        seen_events.append(event)

    adapter._process_event = fake_process_event  # type: ignore[method-assign]
    adapter._begin_reply_send("100")
    try:
        await adapter._on_group_message(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "100",
                "user_id": "200",
                "self_id": "300",
                "message": [{"type": "text", "data": {"text": "插一句"}}],
            }
        )
    finally:
        adapter._end_reply_send("100")

    assert seen_events[0]["_sirius_received_during_reply_send"] is True


def _engine_for_sending_window() -> (
    tuple[_EmotionalGroupChatEngineBase, list[tuple[str, Message, str]], list[str]]
):
    engine = object.__new__(_EmotionalGroupChatEngineBase)
    background_updates: list[tuple[str, Message, str]] = []
    persisted: list[str] = []

    engine.persona = SimpleNamespace(name="Luna", aliases=["月白"])
    engine.config = {}
    engine._current_adapter_type = ""
    engine._pipeline = SimpleNamespace(
        perception=lambda group_id, message, participants: "u1",
        background_update=lambda group_id, message, emotion, intent, user_id: background_updates.append(
            (group_id, message, user_id)
        ),
    )
    engine.event_bus = SimpleNamespace(emit=AsyncMock())
    engine.delayed_queue = DelayedResponseQueue()
    engine._persistence = SimpleNamespace(
        persist_group_state=lambda group_id: persisted.append(group_id)
    )
    engine._log_inner_thought = lambda *args, **kwargs: None

    return engine, background_updates, persisted


@pytest.mark.asyncio
async def test_engine_when_message_arrives_during_send_without_bot_mention_then_silent():
    engine, background_updates, persisted = _engine_for_sending_window()

    result = await engine.process_message(
        Message(
            role="user",
            content="插一句普通话",
            speaker="Alice",
            received_during_bot_send=True,
        ),
        [SimpleNamespace(is_developer=False)],
        "group-1",
    )

    assert result["strategy"] == "silent"
    assert engine.delayed_queue.get_pending("group-1") == []
    assert background_updates
    assert persisted == []


@pytest.mark.asyncio
async def test_engine_when_message_arrives_during_send_with_bot_mention_then_delayed():
    engine, background_updates, persisted = _engine_for_sending_window()

    result = await engine.process_message(
        Message(
            role="user",
            content="Luna 等下看这里",
            speaker="Alice",
            received_during_bot_send=True,
        ),
        [SimpleNamespace(is_developer=False)],
        "group-1",
    )

    pending = engine.delayed_queue.get_pending("group-1")
    assert result["strategy"] == "delayed"
    assert len(pending) == 1
    assert pending[0].strategy_decision.reason == "received_during_bot_send_mention"
    assert "Luna 等下看这里" in pending[0].message_content
    assert background_updates
    assert persisted == ["group-1"]
