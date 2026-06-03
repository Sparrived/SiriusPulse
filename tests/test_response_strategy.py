"""机器人回复策略的业务场景测试。"""

from __future__ import annotations

from sirius_pulse.core.response_strategy import ResponseStrategyEngine
from sirius_pulse.models.intent_v3 import IntentAnalysisV3, SocialIntent
from sirius_pulse.models.response_strategy import ResponseStrategy


def _intent(
    *,
    urgency: float = 50.0,
    relevance: float = 0.5,
    threshold: float = 0.5,
    social_intent: SocialIntent = SocialIntent.SOCIAL,
    directed_score: float = 0.8,
) -> IntentAnalysisV3:
    return IntentAnalysisV3(
        urgency_score=urgency,
        relevance_score=relevance,
        threshold=threshold,
        social_intent=social_intent,
        directed_score=directed_score,
    )


def test_strategy_when_user_mentions_bot_for_help_then_reply_is_immediate():
    decision = ResponseStrategyEngine().decide(
        _intent(urgency=10, relevance=0.1, social_intent=SocialIntent.HELP_SEEKING),
        is_mentioned=True,
        sender_type="human",
    )

    assert decision.strategy == ResponseStrategy.IMMEDIATE
    assert decision.estimated_delay_seconds == 0.0
    assert decision.reason == "direct_mention_help_seeking"


def test_strategy_when_user_shows_emotional_crisis_then_reply_is_immediate_even_without_mention():
    decision = ResponseStrategyEngine().decide(
        _intent(urgency=80, relevance=0.5, social_intent=SocialIntent.EMOTIONAL),
        is_mentioned=False,
    )

    assert decision.strategy == ResponseStrategy.IMMEDIATE
    assert decision.reason == "emotional_crisis"


def test_strategy_when_message_is_not_for_bot_then_bot_stays_silent():
    decision = ResponseStrategyEngine().decide(
        _intent(urgency=90, relevance=0.9, social_intent=SocialIntent.SILENT),
        is_mentioned=False,
    )

    assert decision.strategy == ResponseStrategy.SILENT
    assert decision.estimated_delay_seconds == 0.0


def test_strategy_when_human_directly_mentions_bot_then_human_gets_priority_over_matrix():
    decision = ResponseStrategyEngine().decide(
        _intent(urgency=20, relevance=0.1),
        is_mentioned=True,
        sender_type="human",
    )

    assert decision.strategy == ResponseStrategy.IMMEDIATE
    assert decision.reason == "direct_mention"


def test_strategy_when_peer_ai_mentions_bot_then_reply_is_delayed_to_avoid_ai_loop():
    decision = ResponseStrategyEngine().decide(
        _intent(urgency=70, relevance=0.7),
        is_mentioned=True,
        sender_type="other_ai",
    )

    assert decision.strategy == ResponseStrategy.DELAYED
    assert decision.reason == "peer_ai_direct_mention"


def test_strategy_when_message_is_urgent_and_relevant_then_bot_replies_now():
    decision = ResponseStrategyEngine().decide(
        _intent(urgency=95, relevance=0.8, threshold=0.3),
        sender_type="human",
    )

    assert decision.strategy == ResponseStrategy.IMMEDIATE


def test_strategy_when_message_is_useful_but_not_urgent_then_reply_is_delayed():
    decision = ResponseStrategyEngine().decide(
        _intent(urgency=65, relevance=0.6),
        sender_type="human",
    )

    assert decision.strategy == ResponseStrategy.DELAYED
    assert decision.estimated_delay_seconds == 30.0


def test_strategy_when_group_is_overheated_then_high_priority_message_can_be_delayed():
    warm = ResponseStrategyEngine().decide(
        _intent(urgency=80, relevance=0.7, threshold=0.45),
        heat_level="warm",
        sender_type="human",
    )
    overheated = ResponseStrategyEngine().decide(
        _intent(urgency=80, relevance=0.7, threshold=0.45),
        heat_level="overheated",
        sender_type="human",
    )

    assert warm.strategy == ResponseStrategy.IMMEDIATE
    assert overheated.strategy == ResponseStrategy.DELAYED


def test_strategy_when_message_is_weakly_directed_then_bot_does_not_grab_immediate_reply():
    decision = ResponseStrategyEngine().decide(
        _intent(urgency=95, relevance=0.8, threshold=0.3, directed_score=0.1),
        is_mentioned=False,
        weak_directed_threshold=0.4,
    )

    assert decision.strategy == ResponseStrategy.DELAYED
    assert decision.reason.startswith("not_directed")


def test_strategy_when_peer_ai_sends_same_content_then_threshold_is_stricter_than_human():
    human_decision = ResponseStrategyEngine().decide(
        _intent(urgency=95, relevance=0.8),
        sender_type="human",
    )
    peer_decision = ResponseStrategyEngine().decide(
        _intent(urgency=95, relevance=0.8),
        sender_type="other_ai",
    )

    assert human_decision.strategy == ResponseStrategy.IMMEDIATE
    assert peer_decision.strategy in {ResponseStrategy.IMMEDIATE, ResponseStrategy.DELAYED}
    assert peer_decision.reason.startswith("peer_ai")


def test_strategy_when_delayed_reply_has_higher_urgency_then_wait_time_is_shorter():
    high = ResponseStrategyEngine._estimate_delay(ResponseStrategy.DELAYED, urgency=80)
    medium = ResponseStrategyEngine._estimate_delay(ResponseStrategy.DELAYED, urgency=50)
    low = ResponseStrategyEngine._estimate_delay(ResponseStrategy.DELAYED, urgency=20)

    assert high < medium < low
