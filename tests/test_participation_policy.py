from __future__ import annotations

from datetime import time
from types import SimpleNamespace

import pytest

from sirius_pulse.core.cognition import CognitionAnalyzer, topic_similarity
from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase
from sirius_pulse.core.participation import (
    ParticipationPolicy,
    get_group_reply_strategy,
    get_reply_time_coefficient,
)
from sirius_pulse.core.pipeline import Pipeline
from sirius_pulse.core.rhythm import RhythmAnalyzer
from sirius_pulse.models.emotion import EmotionState
from sirius_pulse.models.models import Message
from sirius_pulse.models.response_strategy import ResponseStrategy
from sirius_pulse.models.signal import SignalAnalysis


def _policy() -> ParticipationPolicy:
    return ParticipationPolicy()


def test_persona_topic_similarity_uses_recent_assistant_turn():
    analyzer = CognitionAnalyzer(ai_name="Luna")
    signal = analyzer.compute_signal(
        "夜拍参数先调快门还是 ISO？",
        "u1",
        "g1",
        context_messages=[
            {"user_id": "assistant", "content": "夜拍参数应该优先调整什么？"},
        ],
    )

    assert topic_similarity("夜拍参数应该优先调整什么？", "夜拍参数先调快门还是 ISO？") > 0.18
    assert signal.topic_similarity_score > 0.18


def test_topic_similarity_boosts_persona_participation_score():
    common = {
        "is_question": True,
        "urgency_score": 30,
        "relevance_score": 0.4,
        "social_intent": "social",
        "heat_level": "warm",
        "pace": "steady",
        "turn_gap_readiness": 0.5,
    }
    without_context = _policy().evaluate(
        signal=SignalAnalysis(**common, topic_similarity_score=0.0),
        content="夜拍参数先调快门还是 ISO？",
        is_private=False,
        seconds_since_reply=120,
    )
    with_context = _policy().evaluate(
        signal=SignalAnalysis(**common, topic_similarity_score=1.0),
        content="夜拍参数先调快门还是 ISO？",
        is_private=False,
        seconds_since_reply=120,
    )

    assert with_context.reply_need_score > without_context.reply_need_score
    assert with_context.conversation_fit_score > without_context.conversation_fit_score
    assert with_context.score > without_context.score


def test_platform_direct_address_has_same_score_as_at_persona():
    engine = SimpleNamespace(
        _helpers=SimpleNamespace(get_recent_messages=lambda group_id, n: []),
        rhythm_analyzer=RhythmAnalyzer(),
        cognition_analyzer=CognitionAnalyzer(ai_name="Luna"),
    )
    pipeline = Pipeline(engine)

    at_signal = pipeline.compute_signal(
        "@Luna",
        "u1",
        "g1",
        persist=False,
    )
    poke_signal = pipeline.compute_signal(
        "戳了一下 Luna",
        "u1",
        "g1",
        explicitly_addressed=True,
        persist=False,
    )

    at_decision = _policy().evaluate(
        signal=at_signal,
        content="@Luna",
        is_private=False,
        directed_gate=0.55,
    )
    poke_decision = _policy().evaluate(
        signal=poke_signal,
        content="戳了一下 Luna",
        is_private=False,
        directed_gate=0.55,
    )

    assert at_signal.is_mentioned is True
    assert poke_signal.is_mentioned is True
    assert at_signal.directed_score == pytest.approx(1.0)
    assert poke_signal.directed_score == pytest.approx(at_signal.directed_score)
    assert poke_decision.score == pytest.approx(at_decision.score)
    assert poke_decision.strategy == at_decision.strategy == ResponseStrategy.IMMEDIATE


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


def test_keyword_group_rejects_message_without_name_or_alias():
    engine = SimpleNamespace(
        config={"group_reply_strategies": {"keyword-group": "keyword"}},
        cognition_analyzer=CognitionAnalyzer(ai_name="Luna", ai_aliases=["月白"]),
    )
    pipeline = Pipeline(engine)
    signal = SignalAnalysis()

    result = pipeline.pre_filter(signal, "大家今天吃什么？", "u1", "keyword-group")

    assert result == "reject"
    assert signal.participation["reason"] == "keyword_not_mentioned"
    assert signal.participation["context"]["keyword_mentioned"] is False


@pytest.mark.parametrize("content", ["Luna 你怎么看？", "月白，帮我看看"])
def test_keyword_group_passes_message_with_name_or_alias(content):
    engine = SimpleNamespace(
        config={"group_reply_strategies": {"keyword-group": "keyword"}},
        cognition_analyzer=CognitionAnalyzer(ai_name="Luna", ai_aliases=["月白"]),
    )
    pipeline = Pipeline(engine)
    signal = SignalAnalysis()

    result = pipeline.pre_filter(signal, content, "u1", "keyword-group")

    assert result == "pass"
    assert signal.is_mentioned is True
    assert signal.participation["reason"] == "keyword_mentioned"
    assert signal.participation["context"]["keyword_mentioned"] is True


def test_unconfigured_group_keeps_smart_participation_mode():
    assert get_group_reply_strategy({}, "smart-group") == "smart"
    assert (
        get_group_reply_strategy(
            {"group_reply_strategies": {"keyword-group": "keyword"}},
            "smart-group",
        )
        == "smart"
    )


def test_keyword_group_preview_does_not_promote_platform_only_mention():
    engine = object.__new__(_EmotionalGroupChatEngineBase)
    engine.config = {"group_reply_strategies": {"keyword-group": "keyword"}}
    engine.persona = SimpleNamespace(name="Luna", aliases=["月白"])
    engine._compute_signal = lambda *args, **kwargs: SignalAnalysis(is_mentioned=True)
    engine._pre_filter = lambda *args, **kwargs: "reject"

    candidate = engine.preview_dispatch(
        Message(role="user", content="你好", mentions_current_bot=True),
        [SimpleNamespace(user_id="u1", is_developer=False)],
        "keyword-group",
    )

    assert candidate["should_reply"] is False
    assert candidate["score"] == 0.0


@pytest.mark.parametrize(
    ("ai_name", "aliases", "content"),
    [
        ("月白", ["Sirius"], "Sirius 你怎么看？"),
        ("月白", ["Sirius"], "sirius 帮我看看"),
        ("月白", ["Sirius"], "月白，帮我看看"),
        ("月白", ["Sirius"], "今天月白真好看"),
        ("Luna", ["月白"], "Luna 你怎么看？"),
    ],
)
def test_message_naming_persona_is_a_full_name_match(ai_name, aliases, content):
    """点名（英文别名按词边界、中文名按子串）应记满分，而不是退化成弱子串匹配。"""
    analyzer = CognitionAnalyzer(ai_name=ai_name, ai_aliases=aliases)

    scores = analyzer._compute_directed_scores(content, "u1", None)

    assert scores["name_match_score"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("lunatic rambling", 0.6),  # 名字嵌在别的词里，只能算弱指向
        ("aSiriusb", 0.6),
        ("完全无关的闲聊", 0.0),
    ],
)
def test_name_embedded_in_another_word_stays_a_weak_match(content, expected):
    """词边界保护的业务含义：别人说 lunatic 不等于在叫 Luna。"""
    analyzer = CognitionAnalyzer(ai_name="Luna", ai_aliases=["Sirius"])

    scores = analyzer._compute_directed_scores(content, "u1", None)

    assert scores["name_match_score"] == pytest.approx(expected)


def test_peer_ai_naming_persona_by_alias_is_treated_as_addressed():
    """同群另一个 AI 用别名点名她时，衰减后仍应认出这是在叫她。"""
    engine = SimpleNamespace(
        _helpers=SimpleNamespace(get_recent_messages=lambda group_id, n: []),
        rhythm_analyzer=RhythmAnalyzer(),
        cognition_analyzer=CognitionAnalyzer(ai_name="月白", ai_aliases=["Sirius"]),
    )

    signal = Pipeline(engine).compute_signal(
        "Sirius 你怎么看？", "u1", "g1", persist=False, sender_type="other_ai"
    )

    assert signal.is_mentioned is True
