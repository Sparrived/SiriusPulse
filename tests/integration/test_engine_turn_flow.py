"""Integration tests for the engine message turn pipeline."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sirius_pulse.core.emotional_engine import EmotionalGroupChatEngine
from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.models.models import Message
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.providers.mock import MockProvider

pytestmark = pytest.mark.integration


def _cognition_json(
    *,
    directed_score: float = 1.0,
    urgency_score: float = 85.0,
) -> str:
    return json.dumps(
        {
            "valence": 0.1,
            "arousal": 0.3,
            "intensity": 0.4,
            "basic_emotion": "neutral",
            "social_intent": "social",
            "intent_subtype": "topic_discussion",
            "urgency_score": urgency_score,
            "relevance_score": 0.9,
            "directed_score": directed_score,
            "directed_reason": "the message names the bot",
            "sarcasm_score": 0.0,
            "confidence": 0.9,
            "search_query": "build plan",
            "image_caption": "",
        }
    )


def _engine(tmp_path: Path, provider: MockProvider) -> EmotionalGroupChatEngine:
    return EmotionalGroupChatEngine(
        work_path=tmp_path,
        persona=PersonaProfile(
            name="TestBot",
            aliases=["bot"],
            reply_frequency="high",
        ),
        provider_async=provider,
        config={
            "sensitivity": 1.0,
            "reply_cooldown_seconds": 0.0,
            "expressiveness": {
                "expressiveness": 1.0,
                "overrides": {
                    "cooldown_seconds": 0.0,
                    "gap_readiness_threshold": 0.0,
                },
            },
        },
    )


def _participant() -> UnifiedUser:
    return UnifiedUser(
        user_id="u1",
        name="Alice",
        identities={"qq": "1001"},
    )


def _message(content: str, message_id: str = "msg-1") -> Message:
    return Message(
        role="human",
        content=content,
        speaker="Alice",
        channel="qq",
        channel_user_id="1001",
        message_id=message_id,
    )


def _expire_pending_item(engine: EmotionalGroupChatEngine, group_id: str) -> None:
    pending = engine.delayed_queue.get_pending(group_id)
    assert len(pending) == 1
    pending[0].enqueue_time = (
        datetime.now(timezone.utc)
        - timedelta(seconds=pending[0].window_seconds + 1)
    ).isoformat()


def _close_engine(engine: EmotionalGroupChatEngine) -> None:
    for attr in (
        "event_bus",
        "semantic_memory",
        "user_manager",
        "evolution_chain",
        "provenance_store",
        "situation_store",
        "_memory_storage",
        "cognition_store",
    ):
        target = getattr(engine, attr, None)
        close = getattr(target, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "send"):
                # event_bus.close is async; these tests never start subscribers.
                result.close()


@pytest.mark.asyncio
async def test_engine_turn_when_directed_message_expires_then_reply_is_generated_and_remembered(
    tmp_path: Path,
):
    provider = MockProvider([_cognition_json(), "final reply"])
    engine = _engine(tmp_path, provider)

    try:
        result = await engine.process_message(
            _message("TestBot, can you help with the build plan?"),
            [_participant()],
            "group-a",
        )

        assert result["strategy"] == "immediate"
        assert result["reply"] is None
        assert engine.delayed_queue.has_pending("group-a") is True
        assert len(provider.requests) == 1

        _expire_pending_item(engine, "group-a")
        ticked = await engine.tick_delayed_queue("group-a")

        assert ticked[0]["reply"] == "final reply"
        assert engine.delayed_queue.has_pending("group-a") is False
        assert len(provider.requests) == 2
        assert provider.requests[1].purpose == "response_generate"

        entries = engine.basic_memory.get_all("group-a")
        assert [entry.role for entry in entries] == ["human", "assistant"]
        assert entries[0].user_id == "u1"
        assert entries[1].content == "final reply"
    finally:
        _close_engine(engine)


@pytest.mark.asyncio
async def test_engine_turn_when_pending_reply_exists_then_next_message_merges_without_llm(
    tmp_path: Path,
):
    provider = MockProvider([_cognition_json()])
    engine = _engine(tmp_path, provider)

    try:
        await engine.process_message(
            _message("TestBot, first question?", "msg-1"),
            [_participant()],
            "group-a",
        )
        merged = await engine.process_message(
            _message("also include this detail", "msg-2"),
            [_participant()],
            "group-a",
        )

        pending = engine.delayed_queue.get_pending("group-a")
        assert merged["strategy"] == "merged"
        assert len(provider.requests) == 1
        assert len(pending) == 1
        assert "first question" in pending[0].message_content
        assert "also include this detail" in pending[0].message_content
        assert [entry.content for entry in engine.basic_memory.get_all("group-a")] == [
            "TestBot, first question?",
            "also include this detail",
        ]
    finally:
        _close_engine(engine)
