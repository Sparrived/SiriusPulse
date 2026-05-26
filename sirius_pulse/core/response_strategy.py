"""Response strategy engine: four-layer decision system (paper §2.3 / §6).

IMMEDIATE → DELAYED → SILENT → PROACTIVE
"""

from __future__ import annotations

import logging

from sirius_pulse.models.intent_v3 import IntentAnalysisV3, SocialIntent
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision

logger = logging.getLogger(__name__)


class ResponseStrategyEngine:
    """Decides response strategy based on intent, emotion, and context."""

    def decide(
        self,
        intent: IntentAnalysisV3,
        *,
        is_mentioned: bool = False,
        weak_directed_threshold: float = 0.4,
        is_developer: bool = False,
        heat_level: str = "warm",
        sender_type: str = "human",
    ) -> StrategyDecision:
        """Decide response strategy from intent analysis.

        Decision matrix:
            urgency >= 80 and relevance >= 0.7  → IMMEDIATE
            urgency >= 60 and relevance >= 0.55 → DELAYED (high priority)
            urgency >= 35 and relevance >= 0.5  → DELAYED (low priority)
            else                                → SILENT

        Heat suppression:
            hot:       urgency × 0.85, relevance × 0.92
            overheated: urgency × 0.68, relevance × 0.85
        """
        urgency = intent.urgency_score
        relevance = intent.relevance_score
        threshold = intent.threshold

        # threshold 参与决策：threshold 越低 → 等效 urgency 越高 → 越容易触发回复
        # 以 sensitivity=0.5 时的典型 threshold=0.45 为基准
        scale = 0.45 / max(threshold, 0.1)
        scaled_urgency = urgency * scale

        # Heat suppression: reduce scores in hot/overheated groups
        heat_mult = {"cold": 1.0, "warm": 1.0, "hot": 0.85, "overheated": 0.68}
        rel_mult = {"cold": 1.0, "warm": 1.0, "hot": 0.92, "overheated": 0.85}
        scaled_urgency *= heat_mult.get(heat_level, 1.0)
        relevance *= rel_mult.get(heat_level, 1.0)

        # 预计算复合分数（用于 undirected 门槛降级判断）
        # score = urgency * 0.6 + relevance * 0.4，反映消息的综合"值得回复"程度
        score = (urgency / 100.0) * 0.6 + relevance * 0.4

        # Special rules
        if is_mentioned and intent.social_intent == SocialIntent.HELP_SEEKING:
            return StrategyDecision(
                strategy=ResponseStrategy.IMMEDIATE,
                score=1.0,
                threshold=threshold,
                urgency=urgency,
                relevance=relevance,
                reason="direct_mention_help_seeking",
            )

        if intent.social_intent == SocialIntent.EMOTIONAL and scaled_urgency >= 70:
            return StrategyDecision(
                strategy=ResponseStrategy.IMMEDIATE,
                score=0.95,
                threshold=threshold,
                urgency=urgency + 20,
                relevance=relevance,
                reason="emotional_crisis",
            )

        if intent.social_intent == SocialIntent.SILENT and not is_mentioned:
            return StrategyDecision(
                strategy=ResponseStrategy.SILENT,
                score=0.0,
                threshold=threshold,
                urgency=urgency,
                relevance=relevance,
                reason="silent_intent",
            )

        # Direct mention override
        if is_mentioned:
            # peer-AI @ 你时降级为 DELAYED，避免 AI 互聊过热
            if sender_type == "other_ai":
                return StrategyDecision(
                    strategy=ResponseStrategy.DELAYED,
                    score=0.8,
                    threshold=threshold,
                    urgency=urgency,
                    relevance=relevance,
                    reason="peer_ai_direct_mention",
                )
            return StrategyDecision(
                strategy=ResponseStrategy.IMMEDIATE,
                score=1.0,
                threshold=threshold,
                urgency=urgency,
                relevance=relevance,
                reason="direct_mention",
            )

        # Standard matrix (with higher thresholds for peer-AI messages)
        directed_score = getattr(intent, "directed_score", 0.0)
        undirected = not is_mentioned and directed_score < weak_directed_threshold
        # 当复合分数达到 threshold 时，放弃 undirected 的高门槛惩罚
        # 避免出现 score > threshold 但仍被判 silent 的矛盾情况
        undirected_high_bar = 55 if (undirected and score < threshold) else 35
        if sender_type == "other_ai":
            if scaled_urgency >= 90 and relevance >= 0.75:
                strategy = ResponseStrategy.IMMEDIATE
                reason = "peer_ai_high_urgency_high_relevance"
            elif scaled_urgency >= 75 and relevance >= 0.6:
                strategy = ResponseStrategy.DELAYED
                reason = "peer_ai_medium_urgency_delayed"
            elif scaled_urgency >= 50 and relevance >= 0.55:
                strategy = ResponseStrategy.DELAYED
                reason = "peer_ai_low_urgency_delayed"
            else:
                strategy = ResponseStrategy.SILENT
                reason = "peer_ai_below_threshold"
        else:
            if scaled_urgency >= 80 and relevance >= 0.7:
                strategy = ResponseStrategy.IMMEDIATE
                reason = "high_urgency_high_relevance"
            elif scaled_urgency >= 60 and relevance >= 0.55:
                strategy = ResponseStrategy.DELAYED
                reason = "medium_urgency_delayed"
            elif scaled_urgency >= undirected_high_bar and relevance >= 0.5:
                strategy = ResponseStrategy.DELAYED
                reason = "low_urgency_delayed"
            else:
                strategy = ResponseStrategy.SILENT
                reason = "below_threshold"

        # 社交底线：没被弱指向（<weak_directed_threshold）就没有抢话权，最高只能 delayed
        # 弱指向保留 IMMEDIATE 资格，强指向（>=directed_threshold）已由 is_mentioned 处理
        if (
            not is_mentioned
            and directed_score < weak_directed_threshold
            and strategy == ResponseStrategy.IMMEDIATE
        ):
            strategy = ResponseStrategy.DELAYED
            reason = f"not_directed_{reason}"

        return StrategyDecision(
            strategy=strategy,
            score=score,
            threshold=threshold,
            urgency=urgency,
            relevance=relevance,
            reason=reason,
            estimated_delay_seconds=self._estimate_delay(strategy, urgency),
        )

    @staticmethod
    def _estimate_delay(strategy: ResponseStrategy, urgency: float) -> float:
        if strategy == ResponseStrategy.IMMEDIATE:
            return 0.0
        if strategy == ResponseStrategy.DELAYED:
            if urgency >= 70:
                return 15.0
            if urgency >= 40:
                return 30.0
            return 60.0
        return 0.0
