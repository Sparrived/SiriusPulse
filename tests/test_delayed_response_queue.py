from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision


def _decision(strategy: ResponseStrategy, *, urgency: float = 50.0) -> StrategyDecision:
    return StrategyDecision(strategy=strategy, urgency=urgency, reason="test")


def _past(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def test_delayed_queue_when_immediate_messages_share_group_then_merges_into_one_item():
    queue = DelayedResponseQueue()

    item = queue.enqueue(
        "group-1",
        "u1",
        "first",
        _decision(ResponseStrategy.IMMEDIATE),
        candidate_memories=["m1"],
        multimodal_inputs=[{"type": "image", "value": "a.png"}],
        channel="qq",
        channel_user_id="qq-1",
        speaker_name="Alice",
        platform_message_id="msg-1",
    )
    merged = queue.enqueue(
        "group-1",
        "u2",
        "second",
        _decision(ResponseStrategy.IMMEDIATE),
        emotion_state={"mood": "warm"},
        candidate_memories=["m2"],
        multimodal_inputs=[{"type": "image", "value": "b.png"}],
        channel="qq",
        channel_user_id="qq-2",
        speaker_name="Bob",
        platform_message_id="msg-2",
    )

    assert merged is item
    assert len(queue.get_pending("group-1")) == 1
    assert item.window_seconds == 6.0
    assert item.user_id == "u2"
    assert item.channel_user_id == "qq-2"
    assert item.related_user_ids == ["u1", "u2"]
    assert item.candidate_memories == ["m1", "m2"]
    assert item.multimodal_inputs == [
        {"type": "image", "value": "a.png"},
        {"type": "image", "value": "b.png"},
    ]
    assert "first" in item.message_content
    assert "second" in item.message_content


def test_delayed_queue_when_immediate_window_expires_then_triggers_item():
    queue = DelayedResponseQueue()
    item = queue.enqueue("group-1", "u1", "hello", _decision(ResponseStrategy.IMMEDIATE))
    item.enqueue_time = _past(item.window_seconds + 1)

    triggered = queue.tick("group-1", [])

    assert triggered == [item]
    assert item.status == "triggered"
    assert queue.has_pending("group-1") is False


def test_delayed_queue_when_topic_gap_exceeds_threshold_then_delayed_item_triggers_early():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "hello",
        _decision(ResponseStrategy.DELAYED, urgency=50),
        heat_level="cold",
    )
    item.enqueue_time = datetime.now(timezone.utc).isoformat()
    recent_messages = [{"timestamp": _past(6)}]

    triggered = queue.tick("group-1", recent_messages)

    assert triggered == [item]
    assert item.status == "triggered"


def test_delayed_queue_when_merging_incoming_then_appends_to_existing_pending_item():
    queue = DelayedResponseQueue()
    item = queue.enqueue("group-1", "u1", "first", _decision(ResponseStrategy.DELAYED))

    assert (
        queue.merge_incoming(
            "group-1",
            "u2",
            "second",
            speaker_name="Bob",
            channel_user_id="qq-2",
            multimodal_inputs=[{"type": "image", "value": "b.png"}],
        )
        is True
    )

    assert "second" in item.message_content
    assert item.related_user_ids == ["u1", "u2"]
    assert item.multimodal_inputs == [{"type": "image", "value": "b.png"}]
    assert queue.merge_incoming("missing", "u3", "third") is False


def test_delayed_queue_when_cancelled_or_cleared_then_pending_items_disappear():
    queue = DelayedResponseQueue()
    first = queue.enqueue("group-1", "u1", "first", _decision(ResponseStrategy.DELAYED))
    second = queue.enqueue("group-2", "u2", "second", _decision(ResponseStrategy.DELAYED))

    assert queue.cancel_all_for_user("group-1", "u1") == 1
    assert first.status == "cancelled"
    assert queue.get_pending("group-1") == []

    queue.clear_group("group-2")

    assert queue.get_pending("group-2") == []
    assert second.status == "pending"


def test_delayed_queue_when_corrupted_entry_exists_then_tick_filters_it_out():
    queue = DelayedResponseQueue()
    queue._queues["group-1"] = [{"bad": "entry"}]  # type: ignore[list-item]

    assert queue.tick("group-1", []) == []
    assert queue.get_pending("group-1") == []
