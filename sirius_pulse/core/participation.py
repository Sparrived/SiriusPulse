"""Rule-based group participation policy.

This module decides whether a message should enter the reply queue before any
LLM call is made.  It is intentionally heuristic and explainable: the pipeline
can log each component score so thresholds can be tuned from production traces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sirius_pulse.models.response_strategy import ResponseStrategy
from sirius_pulse.models.signal import SignalAnalysis
from sirius_pulse.reply_time_curve import get_reply_time_coefficient


_HELP_RE = re.compile(
    r"(怎么|咋|如何|为什么|为啥|哪[个里]|帮|救|求|有没有|谁能|报错|异常|失败|不行|不能|坏了|崩|卡住|"
    r"error|exception|failed|fail|bug|fix|help)",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"(现在|马上|立刻|赶紧|急|今天|今晚|刚刚|快|asap|urgent)", re.IGNORECASE)
_LOW_INFO_RE = re.compile(
    r"^\s*(哈+|哈哈+|草+|艹+|乐+|笑死+|嗯+|哦+|额+|啊+|？+|\?+|！+|!+|[~。…,.，、 ]+)\s*$"
)
_SOCIAL_JOIN_RE = re.compile(
    r"(感觉|觉得|好像|确实|其实|笑死|离谱|有点|还挺|真的|什么情况|原来|难怪|懂了|绷不住|"
    r"可以|不错|太|好玩|有意思)"
)
_IMAGE_RE = re.compile(r"(\[图片描述|\[动画表情|图片|图里|截图|表情包)")


@dataclass(slots=True)
class ParticipationDecision:
    """A non-LLM decision about whether and when to participate."""

    strategy: ResponseStrategy
    reason: str
    score: float
    threshold: float
    delay_seconds: float
    addressing_score: float
    reply_need_score: float
    social_opportunity_score: float
    conversation_fit_score: float
    suppression_score: float
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def should_reply(self) -> bool:
        return self.strategy != ResponseStrategy.SILENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "reason": self.reason,
            "score": round(self.score, 4),
            "threshold": round(self.threshold, 4),
            "delay_seconds": self.delay_seconds,
            "addressing_score": round(self.addressing_score, 4),
            "reply_need_score": round(self.reply_need_score, 4),
            "social_opportunity_score": round(self.social_opportunity_score, 4),
            "conversation_fit_score": round(self.conversation_fit_score, 4),
            "suppression_score": round(self.suppression_score, 4),
            "context": dict(self.context),
        }


class ParticipationPolicy:
    """Pure-rule participation policy for group chat."""

    def evaluate(
        self,
        *,
        signal: SignalAnalysis,
        content: str,
        is_private: bool,
        sender_type: str = "human",
        seconds_since_reply: float = 999999.0,
        cooldown_seconds: float = 0.0,
        directed_gate: float = 0.5,
        entitlement_threshold: float = 0.4,
        reply_frequency: str = "moderate",
        affinity_score: float = 0.0,
        reply_time_coefficient: float = 1.0,
    ) -> ParticipationDecision:
        text = content or ""
        text_len = len(text.strip())
        coefficient = _clamp_coefficient(reply_time_coefficient)
        addressing = self._addressing_score(signal, directed_gate)
        reply_need = self._reply_need_score(signal, text)
        social = self._social_opportunity_score(signal, text, seconds_since_reply, affinity_score)
        fit = self._conversation_fit_score(signal, text, affinity_score)
        suppression = self._suppression_score(
            signal=signal,
            text=text,
            sender_type=sender_type,
            seconds_since_reply=seconds_since_reply,
            cooldown_seconds=cooldown_seconds,
            entitlement_threshold=entitlement_threshold,
        )

        if is_private:
            threshold = 0.2
            raw_score = max(addressing, reply_need, fit)
            final_score = _scale_reply_score(raw_score, coefficient)
            if final_score >= threshold:
                return self._decision(
                    signal,
                    ResponseStrategy.IMMEDIATE if signal.urgency_score >= 70 else ResponseStrategy.DELAYED,
                    "private_chat",
                    final_score,
                    threshold,
                    0.0 if signal.urgency_score >= 70 else 8.0,
                    addressing,
                    reply_need,
                    social,
                    fit,
                    suppression,
                    raw_score=raw_score,
                    reply_time_coefficient=coefficient,
                )
            return self._decision(
                signal,
                ResponseStrategy.SILENT,
                "below_participation_threshold",
                final_score,
                threshold,
                0.0,
                addressing,
                reply_need,
                social,
                fit,
                suppression,
                raw_score=raw_score,
                reply_time_coefficient=coefficient,
            )

        direct_threshold, need_threshold, join_threshold = self._thresholds(
            reply_frequency=reply_frequency,
            directed_gate=directed_gate,
        )
        if affinity_score > 0.35:
            need_threshold -= 0.04
            join_threshold -= 0.04
        if affinity_score < -0.25:
            need_threshold += 0.05
            join_threshold += 0.05

        addressed_score = _scale_reply_score(addressing, coefficient)
        if addressed_score >= direct_threshold and suppression < 0.9:
            strategy = (
                ResponseStrategy.IMMEDIATE
                if signal.is_mentioned or signal.urgency_score >= 80
                else ResponseStrategy.DELAYED
            )
            delay = 0.0 if strategy == ResponseStrategy.IMMEDIATE else 12.0
            return self._decision(
                signal,
                strategy,
                "addressed",
                addressed_score,
                direct_threshold,
                delay,
                addressing,
                reply_need,
                social,
                fit,
                suppression,
                raw_score=addressing,
                reply_time_coefficient=coefficient,
        )

        need_score = reply_need * 0.75 + fit * 0.25 - suppression * 0.25
        final_reply_need = _scale_reply_score(reply_need, coefficient)
        final_need_score = _scale_reply_score(need_score, coefficient)
        if final_reply_need >= need_threshold and final_need_score >= need_threshold and suppression < 0.78:
            strategy = ResponseStrategy.IMMEDIATE if signal.urgency_score >= 80 else ResponseStrategy.DELAYED
            delay = 0.0 if strategy == ResponseStrategy.IMMEDIATE else self._delay_for(signal, base=18.0)
            return self._decision(
                signal,
                strategy,
                "reply_needed",
                final_need_score,
                need_threshold,
                delay,
                addressing,
                reply_need,
                social,
                fit,
                suppression,
                raw_score=need_score,
                reply_time_coefficient=coefficient,
            )

        join_score = social * 0.45 + fit * 0.35 + reply_need * 0.20 - suppression * 0.35
        final_join_score = _scale_reply_score(join_score, coefficient)
        can_join = (
            text_len >= 4
            and social >= 0.42
            and fit >= 0.35
            and suppression < 0.52
            and final_join_score >= join_threshold
        )
        if can_join:
            return self._decision(
                signal,
                ResponseStrategy.DELAYED,
                "natural_join",
                final_join_score,
                join_threshold,
                self._delay_for(signal, base=28.0),
                addressing,
                reply_need,
                social,
                fit,
                suppression,
                raw_score=join_score,
                reply_time_coefficient=coefficient,
            )

        score = max(addressing, need_score, join_score)
        final_score = _scale_reply_score(score, coefficient)
        return self._decision(
            signal,
            ResponseStrategy.SILENT,
            "below_participation_threshold",
            final_score,
            min(direct_threshold, need_threshold, join_threshold),
            0.0,
            addressing,
            reply_need,
            social,
            fit,
            suppression,
            raw_score=score,
            reply_time_coefficient=coefficient,
        )

    def _decision(
        self,
        signal: SignalAnalysis,
        strategy: ResponseStrategy,
        reason: str,
        score: float,
        threshold: float,
        delay_seconds: float,
        addressing: float,
        reply_need: float,
        social: float,
        fit: float,
        suppression: float,
        raw_score: float | None = None,
        reply_time_coefficient: float = 1.0,
    ) -> ParticipationDecision:
        return ParticipationDecision(
            strategy=strategy,
            reason=reason,
            score=_clamp_reply_score(score),
            threshold=max(0.0, threshold),
            delay_seconds=delay_seconds,
            addressing_score=addressing,
            reply_need_score=reply_need,
            social_opportunity_score=social,
            conversation_fit_score=fit,
            suppression_score=suppression,
            context={
                "raw_score": round(_clamp(raw_score if raw_score is not None else score), 4),
                "reply_time_coefficient": round(_clamp_coefficient(reply_time_coefficient), 4),
                "final_score": round(_clamp_reply_score(score), 4),
                "urgency_score": signal.urgency_score,
                "directed_score": signal.directed_score,
                "heat_level": signal.heat_level,
                "pace": signal.pace,
                "turn_gap_readiness": signal.turn_gap_readiness,
                "social_intent": signal.social_intent,
            },
        )

    def _addressing_score(self, signal: SignalAnalysis, directed_gate: float) -> float:
        if signal.is_mentioned:
            return 1.0
        score = signal.directed_score
        if signal.directed_score >= max(0.05, directed_gate * 0.75):
            score += 0.10
        if signal.is_question:
            score += 0.08
        if signal.is_imperative:
            score += 0.08
        return _clamp(score)

    def _reply_need_score(self, signal: SignalAnalysis, text: str) -> float:
        intent_base = {
            "help_seeking": 0.58,
            "emotional": 0.34,
            "social": 0.14,
            "silent": 0.02,
        }.get(signal.social_intent, 0.12)
        score = intent_base
        if signal.is_question:
            score += 0.23
        if signal.is_imperative:
            score += 0.18
        if _HELP_RE.search(text):
            score += 0.22
        if _TIME_RE.search(text):
            score += 0.10
        if _IMAGE_RE.search(text):
            score += 0.10
        if signal.image_caption:
            score += 0.10
        score += (signal.urgency_score / 100.0) * 0.22
        score += signal.relevance_score * 0.16
        return _clamp(score)

    def _social_opportunity_score(
        self,
        signal: SignalAnalysis,
        text: str,
        seconds_since_reply: float,
        affinity_score: float,
    ) -> float:
        heat_bonus = {
            "cold": 0.36,
            "warm": 0.24,
            "hot": 0.10,
            "overheated": -0.18,
        }.get(signal.heat_level, 0.12)
        pace_bonus = {
            "silent": 0.30,
            "decelerating": 0.22,
            "steady": 0.12,
            "accelerating": -0.06,
        }.get(signal.pace, 0.06)
        score = heat_bonus + pace_bonus + signal.turn_gap_readiness * 0.28
        if signal.social_intent in {"social", "emotional"}:
            score += 0.10
        if _SOCIAL_JOIN_RE.search(text):
            score += 0.12
        if signal.emotion and abs(signal.emotion.valence) >= 0.45 and signal.emotion.arousal >= 0.45:
            score += 0.08
        if seconds_since_reply >= 90:
            score += 0.08
        score += max(-0.08, min(0.08, affinity_score * 0.08))
        return _clamp(score)

    def _conversation_fit_score(self, signal: SignalAnalysis, text: str, affinity_score: float) -> float:
        score = signal.relevance_score * 0.60
        if signal.social_intent == "help_seeking":
            score += 0.18
        elif signal.social_intent == "emotional":
            score += 0.14
        elif signal.social_intent == "social":
            score += 0.08
        if _HELP_RE.search(text) or _SOCIAL_JOIN_RE.search(text):
            score += 0.12
        if signal.is_question:
            score += 0.08
        if signal.image_caption:
            score += 0.08
        score += max(-0.08, min(0.08, affinity_score * 0.08))
        return _clamp(score)

    def _suppression_score(
        self,
        *,
        signal: SignalAnalysis,
        text: str,
        sender_type: str,
        seconds_since_reply: float,
        cooldown_seconds: float,
        entitlement_threshold: float,
    ) -> float:
        score = 0.0
        stripped = text.strip()
        if signal.heat_level == "overheated":
            score += 0.25
            if signal.burst_detected:
                score += 0.35
        elif signal.heat_level == "hot" and signal.burst_detected:
            score += 0.18

        if cooldown_seconds > 0 and seconds_since_reply < cooldown_seconds and not signal.is_mentioned:
            remaining_ratio = (cooldown_seconds - seconds_since_reply) / cooldown_seconds
            score += 0.25 + remaining_ratio * 0.35

        if not signal.is_mentioned and _LOW_INFO_RE.match(stripped):
            score += 0.38
        if not signal.is_mentioned and len(stripped) <= 2:
            score += 0.20
        if sender_type != "human":
            score += 0.28
        if signal.entitlement_score < entitlement_threshold:
            score += 0.22
        if signal.sarcasm_score >= 0.65 and signal.directed_score < 0.4:
            score += 0.10
        return _clamp(score)

    def _thresholds(
        self,
        *,
        reply_frequency: str,
        directed_gate: float,
    ) -> tuple[float, float, float]:
        direct = max(0.38, directed_gate)
        need = 0.58
        join = 0.50
        if reply_frequency == "high":
            return direct * 0.82, need - 0.08, join - 0.08
        if reply_frequency == "low":
            return direct * 1.15, need + 0.08, join + 0.10
        if reply_frequency == "selective":
            return direct * 1.25, need + 0.12, join + 0.16
        return direct, need, join

    def _delay_for(self, signal: SignalAnalysis, *, base: float) -> float:
        if signal.urgency_score >= 70:
            return 10.0
        if signal.urgency_score >= 50:
            return min(base, 15.0)
        if signal.heat_level == "cold" or signal.pace == "silent":
            return max(18.0, base - 6.0)
        return base


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _clamp_coefficient(value: float) -> float:
    return max(0.0, min(2.0, float(value)))


def _clamp_reply_score(value: float) -> float:
    return max(0.0, min(2.0, float(value)))


def _scale_reply_score(raw_score: float, coefficient: float) -> float:
    return _clamp_reply_score(_clamp(raw_score) * _clamp_coefficient(coefficient))


__all__ = ["ParticipationDecision", "ParticipationPolicy", "get_reply_time_coefficient"]
