from __future__ import annotations

import pytest

from sirius_chat.core.delayed_response_queue import DelayedResponseQueue
from sirius_chat.models.response_strategy import (
    DelayedResponseItem,
    ResponseStrategy,
    StrategyDecision,
)


class TestDelayedQueueMerge:
    """Test debounce merge behaviour for high-frequency messages."""

    def test_immediate_merges_into_pending_delayed(self):
        """An IMMEDIATE item should merge into an existing pending DELAYED item."""
        q = DelayedResponseQueue()
        dec_delayed = StrategyDecision(strategy=ResponseStrategy.DELAYED, urgency=50)
        dec_imm = StrategyDecision(strategy=ResponseStrategy.IMMEDIATE, urgency=90)

        item1 = q.enqueue("g1", "u1", "hello", dec_delayed)
        assert item1.strategy_decision.strategy == ResponseStrategy.DELAYED
        assert item1.window_seconds == 30.0

        item2 = q.enqueue("g1", "u2", "world", dec_imm)
        # Should return the same merged item
        assert item2 is item1
        assert item1.message_content == "hello\nworld"
        # Strategy upgraded to IMMEDIATE
        assert item1.strategy_decision.strategy == ResponseStrategy.IMMEDIATE
        # Window shortened to immediate debounce
        assert item1.window_seconds == 5.0

    def test_delayed_merges_into_pending_immediate(self):
        """A DELAYED item should merge into an existing pending IMMEDIATE item."""
        q = DelayedResponseQueue()
        dec_imm = StrategyDecision(strategy=ResponseStrategy.IMMEDIATE, urgency=90)
        dec_delayed = StrategyDecision(strategy=ResponseStrategy.DELAYED, urgency=50)

        item1 = q.enqueue("g1", "u1", "hello", dec_imm)
        assert item1.window_seconds == 5.0

        item2 = q.enqueue("g1", "u2", "world", dec_delayed)
        assert item2 is item1
        assert item1.message_content == "hello\nworld"
        # Strategy stays IMMEDIATE (more urgent)
        assert item1.strategy_decision.strategy == ResponseStrategy.IMMEDIATE
        # Window stays 5.0 (min of 5 and 30)
        assert item1.window_seconds == 5.0

    def test_multiple_delayed_merge(self):
        """Multiple DELAYED items in the same group should merge."""
        q = DelayedResponseQueue()
        dec = StrategyDecision(strategy=ResponseStrategy.DELAYED, urgency=50)

        item1 = q.enqueue("g1", "u1", "msg1", dec)
        item2 = q.enqueue("g1", "u2", "msg2", dec)
        item3 = q.enqueue("g1", "u3", "msg3", dec)

        assert item1 is item2 is item3
        assert item1.message_content == "msg1\nmsg2\nmsg3"
        assert len(q.get_pending("g1")) == 1

    def test_no_merge_across_groups(self):
        """Items in different groups should not merge."""
        q = DelayedResponseQueue()
        dec = StrategyDecision(strategy=ResponseStrategy.IMMEDIATE, urgency=90)

        item1 = q.enqueue("g1", "u1", "hello", dec)
        item2 = q.enqueue("g2", "u1", "hello", dec)

        assert item1 is not item2
        assert len(q.get_pending("g1")) == 1
        assert len(q.get_pending("g2")) == 1

    def test_no_merge_after_triggered(self):
        """Once an item is triggered, new messages should create a fresh item."""
        q = DelayedResponseQueue()
        dec = StrategyDecision(strategy=ResponseStrategy.IMMEDIATE, urgency=90)

        item1 = q.enqueue("g1", "u1", "hello", dec)
        item1.status = "triggered"

        item2 = q.enqueue("g1", "u2", "world", dec)
        assert item2 is not item1
        assert item2.status == "pending"

    def test_merge_updates_caller_identity(self):
        """Merging should update user_id / channel to the latest message."""
        q = DelayedResponseQueue()
        dec = StrategyDecision(strategy=ResponseStrategy.DELAYED, urgency=50)

        item1 = q.enqueue("g1", "u1", "hello", dec, channel="qq", channel_user_id="100")
        q.enqueue("g1", "u2", "world", dec, channel="qq", channel_user_id="200")

        assert item1.user_id == "u2"
        assert item1.channel_user_id == "200"

    def test_merge_multimodal_inputs(self):
        """Merging should accumulate multimodal_inputs from multiple messages."""
        q = DelayedResponseQueue()
        dec = StrategyDecision(strategy=ResponseStrategy.DELAYED, urgency=50)

        item1 = q.enqueue(
            "g1", "u1", "hello", dec,
            multimodal_inputs=[{"type": "image", "value": "http://a.jpg"}],
        )
        q.enqueue(
            "g1", "u2", "world", dec,
            multimodal_inputs=[{"type": "image", "value": "http://b.jpg"}],
        )

        assert len(item1.multimodal_inputs) == 2
        assert item1.multimodal_inputs[0]["value"] == "http://a.jpg"
        assert item1.multimodal_inputs[1]["value"] == "http://b.jpg"

    def test_enqueue_with_multimodal_inputs(self):
        """A fresh enqueue should preserve multimodal_inputs."""
        q = DelayedResponseQueue()
        dec = StrategyDecision(strategy=ResponseStrategy.IMMEDIATE, urgency=90)

        item = q.enqueue(
            "g1", "u1", "hello", dec,
            multimodal_inputs=[{"type": "image", "value": "http://c.jpg"}],
        )

        assert item.multimodal_inputs == [{"type": "image", "value": "http://c.jpg"}]


class TestTextSimilarity:
    """Test the _text_similarity helper used for deduplication."""

    def test_exact_match(self):
        from sirius_chat.core.emotional_engine import EmotionalGroupChatEngine

        assert EmotionalGroupChatEngine._text_similarity("abc", "abc") == 1.0

    def test_completely_different(self):
        from sirius_chat.core.emotional_engine import EmotionalGroupChatEngine

        assert EmotionalGroupChatEngine._text_similarity("abc", "xyz") < 0.2

    def test_high_prefix_overlap(self):
        from sirius_chat.core.emotional_engine import EmotionalGroupChatEngine

        a = "收到啦临雀大人！以后每天早上九点"
        b = "收到啦临雀大人！以后每天早上九点月白都会准时"
        sim = EmotionalGroupChatEngine._text_similarity(a, b)
        assert sim > 0.6

    def test_similar_but_not_identical(self):
        from sirius_chat.core.emotional_engine import EmotionalGroupChatEngine

        a = "无名大人太厉害啦，9小时曝光的草帽星系一定超美喵！"
        b = "哇，这次是曝光9.3小时的M104草帽星系喵！无名大人太厉害啦"
        sim = EmotionalGroupChatEngine._text_similarity(a, b)
        # Same topic, high overlap in bigrams
        assert sim > 0.3

    def test_empty_strings(self):
        from sirius_chat.core.emotional_engine import EmotionalGroupChatEngine

        assert EmotionalGroupChatEngine._text_similarity("", "hello") == 0.0
        assert EmotionalGroupChatEngine._text_similarity("hello", "") == 0.0
