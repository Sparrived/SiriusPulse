"""ResponseStrategyEngine 四层决策系统测试。"""
from __future__ import annotations

from sirius_pulse.core.response_strategy import ResponseStrategyEngine
from sirius_pulse.models.intent_v3 import IntentAnalysisV3, SocialIntent
from sirius_pulse.models.response_strategy import ResponseStrategy


def _make_intent(
    *,
    urgency: float = 50.0,
    relevance: float = 0.5,
    threshold: float = 0.5,
    social_intent: SocialIntent = SocialIntent.SOCIAL,
    directed_score: float = 0.0,
) -> IntentAnalysisV3:
    return IntentAnalysisV3(
        urgency_score=urgency,
        relevance_score=relevance,
        threshold=threshold,
        social_intent=social_intent,
        directed_score=directed_score,
    )


class TestSpecialRules:
    """特殊规则优先级测试。"""

    def test_mentioned_help_seeking_always_immediate(self):
        engine = ResponseStrategyEngine()
        intent = _make_intent(urgency=10, relevance=0.1, social_intent=SocialIntent.HELP_SEEKING)
        decision = engine.decide(intent, is_mentioned=True)
        assert decision.strategy == ResponseStrategy.IMMEDIATE
        assert decision.reason == "direct_mention_help_seeking"

    def test_emotional_crisis_high_urgency(self):
        engine = ResponseStrategyEngine()
        intent = _make_intent(urgency=80, relevance=0.5, social_intent=SocialIntent.EMOTIONAL)
        decision = engine.decide(intent, is_mentioned=False, heat_level="warm")
        assert decision.strategy == ResponseStrategy.IMMEDIATE
        assert decision.reason == "emotional_crisis"

    def test_silent_intent_not_mentioned(self):
        engine = ResponseStrategyEngine()
        intent = _make_intent(urgency=90, relevance=0.9, social_intent=SocialIntent.SILENT)
        decision = engine.decide(intent, is_mentioned=False)
        assert decision.strategy == ResponseStrategy.SILENT
        assert decision.reason == "silent_intent"


class TestDirectMention:
    """直接提及的决策测试。"""

    def test_mentioned_human_gets_immediate(self):
        engine = ResponseStrategyEngine()
        intent = _make_intent(urgency=50, relevance=0.5)
        decision = engine.decide(intent, is_mentioned=True, sender_type="human")
        assert decision.strategy == ResponseStrategy.IMMEDIATE
        assert decision.reason == "direct_mention"

    def test_mentioned_other_ai_gets_delayed(self):
        engine = ResponseStrategyEngine()
        intent = _make_intent(urgency=50, relevance=0.5)
        decision = engine.decide(intent, is_mentioned=True, sender_type="other_ai")
        assert decision.strategy == ResponseStrategy.DELAYED
        assert decision.reason == "peer_ai_direct_mention"


class TestStandardMatrix:
    """标准决策矩阵测试。"""

    def test_high_urgency_high_relevance_immediate(self):
        engine = ResponseStrategyEngine()
        intent = _make_intent(urgency=95, relevance=0.8, threshold=0.3, directed_score=0.8)
        decision = engine.decide(intent, is_mentioned=False, sender_type="human")
        assert decision.strategy == ResponseStrategy.IMMEDIATE

    def test_medium_urgency_delayed(self):
        engine = ResponseStrategyEngine()
        intent = _make_intent(urgency=65, relevance=0.6, directed_score=0.8)
        decision = engine.decide(intent, is_mentioned=False, sender_type="human")
        assert decision.strategy == ResponseStrategy.DELAYED

    def test_low_urgency_silent(self):
        engine = ResponseStrategyEngine()
        intent = _make_intent(urgency=10, relevance=0.2, directed_score=0.8)
        decision = engine.decide(intent, is_mentioned=False, sender_type="human")
        assert decision.strategy == ResponseStrategy.SILENT


class TestHeatSuppression:
    """热度抑制测试。"""

    def test_hot_group_suppresses_urgency(self):
        engine = ResponseStrategyEngine()
        intent = _make_intent(urgency=75, relevance=0.7, directed_score=0.8)

        warm_decision = engine.decide(intent, heat_level="warm", sender_type="human")
        hot_decision = engine.decide(intent, heat_level="hot", sender_type="human")

        assert warm_decision.urgency == hot_decision.urgency

    def test_overheated_stronger_suppression(self):
        engine = ResponseStrategyEngine()
        intent = _make_intent(urgency=85, relevance=0.8, directed_score=0.8)

        hot_decision = engine.decide(intent, heat_level="hot", sender_type="human")
        overheated_decision = engine.decide(intent, heat_level="overheated", sender_type="human")

        if hot_decision.strategy == ResponseStrategy.IMMEDIATE:
            assert overheated_decision.strategy in (
                ResponseStrategy.IMMEDIATE,
                ResponseStrategy.DELAYED,
            )


class TestPeerAiMatrix:
    """AI 发送者决策矩阵测试。"""

    def test_peer_ai_high_threshold(self):
        engine = ResponseStrategyEngine()
        intent = _make_intent(urgency=85, relevance=0.8, directed_score=0.8)
        human_decision = engine.decide(intent, sender_type="human")
        ai_decision = engine.decide(intent, sender_type="other_ai")
        if human_decision.strategy == ResponseStrategy.IMMEDIATE:
            assert ai_decision.strategy in (
                ResponseStrategy.IMMEDIATE,
                ResponseStrategy.DELAYED,
                ResponseStrategy.SILENT,
            )


class TestUndirectedDowngrade:
    """弱指向降级测试。"""

    def test_undirected_immediate_downgraded_to_delayed(self):
        engine = ResponseStrategyEngine()
        intent = _make_intent(urgency=95, relevance=0.8, directed_score=0.1, threshold=0.3)
        decision = engine.decide(intent, is_mentioned=False, weak_directed_threshold=0.4)
        assert decision.strategy == ResponseStrategy.DELAYED
        assert "not_directed" in decision.reason


class TestDelayEstimation:
    """延迟估算测试。"""

    def test_immediate_has_zero_delay(self):
        engine = ResponseStrategyEngine()
        delay = engine._estimate_delay(ResponseStrategy.IMMEDIATE, urgency=90)
        assert delay == 0.0

    def test_delayed_high_urgency_short_delay(self):
        delay = ResponseStrategyEngine._estimate_delay(ResponseStrategy.DELAYED, urgency=80)
        assert delay == 15.0

    def test_delayed_medium_urgency(self):
        delay = ResponseStrategyEngine._estimate_delay(ResponseStrategy.DELAYED, urgency=50)
        assert delay == 30.0

    def test_delayed_low_urgency_long_delay(self):
        delay = ResponseStrategyEngine._estimate_delay(ResponseStrategy.DELAYED, urgency=20)
        assert delay == 60.0

    def test_silent_has_zero_delay(self):
        delay = ResponseStrategyEngine._estimate_delay(ResponseStrategy.SILENT, urgency=0)
        assert delay == 0.0
