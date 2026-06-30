from __future__ import annotations

from datetime import time

import pytest

from sirius_pulse.core.participation import get_reply_time_coefficient
from sirius_pulse.core.participation import ParticipationPolicy
from sirius_pulse.models.emotion import EmotionState
from sirius_pulse.models.response_strategy import ResponseStrategy
from sirius_pulse.models.signal import SignalAnalysis


def _policy() -> ParticipationPolicy:
    return ParticipationPolicy()


def test_participation_when_mentioned_question_then_immediate():
    signal = SignalAnalysis(
        directed_score=0.9,
        is_mentioned=True,
        is_question=True,
        urgency_score=80,
        relevance_score=0.8,
        social_intent="help_seeking",
    )

    decision = _policy().evaluate(
        signal=signal,
        content="sirius 这个怎么修？",
        is_private=False,
        directed_gate=0.55,
    )

    assert decision.strategy == ResponseStrategy.IMMEDIATE
    assert decision.reason == "addressed"


def test_participation_when_unmentioned_help_request_then_delayed():
    signal = SignalAnalysis(
        directed_score=0.25,
        is_question=True,
        urgency_score=55,
        relevance_score=0.55,
        social_intent="help_seeking",
        heat_level="warm",
        pace="steady",
        turn_gap_readiness=0.45,
    )

    decision = _policy().evaluate(
        signal=signal,
        content="这个报错有没有办法绕过去？",
        is_private=False,
        seconds_since_reply=120,
        cooldown_seconds=30,
        directed_gate=0.55,
    )

    assert decision.strategy == ResponseStrategy.DELAYED
    assert decision.reason == "reply_needed"


def test_participation_when_low_information_laugh_then_silent():
    signal = SignalAnalysis(
        directed_score=0.05,
        urgency_score=5,
        relevance_score=0.1,
        social_intent="silent",
        heat_level="warm",
        pace="steady",
        turn_gap_readiness=0.3,
    )

    decision = _policy().evaluate(
        signal=signal,
        content="哈哈哈",
        is_private=False,
        seconds_since_reply=90,
        cooldown_seconds=30,
        directed_gate=0.55,
    )

    assert decision.strategy == ResponseStrategy.SILENT


def test_participation_when_cold_social_opening_then_natural_join():
    signal = SignalAnalysis(
        directed_score=0.12,
        urgency_score=20,
        relevance_score=0.55,
        social_intent="social",
        heat_level="cold",
        pace="silent",
        turn_gap_readiness=0.9,
        emotion=EmotionState(valence=0.6, arousal=0.5),
    )

    decision = _policy().evaluate(
        signal=signal,
        content="这个感觉还挺有意思的",
        is_private=False,
        seconds_since_reply=180,
        cooldown_seconds=30,
        directed_gate=0.55,
    )

    assert decision.strategy == ResponseStrategy.DELAYED
    assert decision.reason == "natural_join"


def test_participation_when_overheated_burst_then_silent():
    signal = SignalAnalysis(
        directed_score=0.2,
        urgency_score=30,
        relevance_score=0.5,
        social_intent="social",
        heat_level="overheated",
        pace="accelerating",
        burst_detected=True,
        turn_gap_readiness=0.1,
    )

    decision = _policy().evaluate(
        signal=signal,
        content="确实有点离谱",
        is_private=False,
        seconds_since_reply=60,
        cooldown_seconds=30,
        directed_gate=0.55,
    )

    assert decision.strategy == ResponseStrategy.SILENT


def test_reply_time_coefficient_when_between_points_then_interpolates():
    coefficient = get_reply_time_coefficient(
        [
            {"time": "00:00", "coefficient": 0.5},
            {"time": "12:00", "coefficient": 1.5},
        ],
        time(6, 0),
    )

    assert coefficient == 1.0


def test_reply_time_coefficient_when_after_last_point_then_wraps_midnight():
    coefficient = get_reply_time_coefficient(
        [
            {"time": "08:00", "coefficient": 2.0},
            {"time": "20:00", "coefficient": 0.0},
        ],
        time(2, 0),
    )

    assert coefficient == 1.0


def test_participation_when_time_curve_zeroes_score_then_stays_silent():
    signal = SignalAnalysis(
        directed_score=0.9,
        is_mentioned=True,
        is_question=True,
        urgency_score=80,
        relevance_score=0.8,
        social_intent="help_seeking",
    )

    decision = _policy().evaluate(
        signal=signal,
        content="sirius 这个怎么修？",
        is_private=False,
        directed_gate=0.55,
        reply_time_coefficient=0.0,
    )

    assert decision.strategy == ResponseStrategy.SILENT
    assert decision.context["raw_score"] > 0.0
    assert decision.context["reply_time_coefficient"] == 0.0
    assert decision.score == 0.0


def test_participation_when_time_curve_boosts_score_then_can_reply():
    signal = SignalAnalysis(
        directed_score=0.4,
        is_question=True,
        urgency_score=30,
        relevance_score=0.4,
        social_intent="neutral",
    )

    decision = _policy().evaluate(
        signal=signal,
        content="sirius 你怎么看？",
        is_private=False,
        directed_gate=0.55,
        reply_time_coefficient=2.0,
    )

    assert decision.strategy == ResponseStrategy.DELAYED
    assert decision.reason == "addressed"
    assert decision.context["reply_time_coefficient"] == 2.0
    assert decision.score == pytest.approx(decision.context["raw_score"] * 2.0)
