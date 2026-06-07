from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sirius_pulse.core.rhythm import RhythmAnalyzer
from sirius_pulse.memory.biography.models import UserBiography
from sirius_pulse.memory.cold_detector import ColdDetector, ColdState
from sirius_pulse.memory.gap_detector import GapDetector, GapType, KnowledgeGap
from sirius_pulse.models.emotion import AssistantEmotionState, BasicEmotion, EmotionState


def _ts(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def test_cold_detector_when_heat_and_silence_vary_then_selects_expected_state():
    assert ColdDetector.check(heat=1.0, seconds_since_last=9999) == ColdState.HOT
    assert ColdDetector.check(heat=0.0, seconds_since_last=299) == ColdState.HOT
    assert ColdDetector.check(heat=0.0, seconds_since_last=300) == ColdState.WARM
    assert ColdDetector.check(heat=0.0, seconds_since_last=1800) == ColdState.COLD


def test_cold_detector_when_candidate_counts_are_low_then_triggers_are_suppressed():
    assert ColdDetector.should_extract_situation(0.0, 300, candidate_count=5) is True
    assert ColdDetector.should_extract_situation(0.0, 300, candidate_count=4) is False
    assert ColdDetector.should_extract_situation(0.0, 1800, candidate_count=5) is False
    assert ColdDetector.should_generate_diary(0.0, 1800, situation_count=1) is True
    assert ColdDetector.should_generate_diary(0.0, 1800, situation_count=0) is False
    assert ColdDetector.should_generate_diary(0.0, 300, situation_count=1) is False


def test_gap_detector_when_biography_is_sparse_then_reports_profile_gaps_and_hint():
    bio = UserBiography(
        user_id="u1", short_bio="tiny", uncertain_fact_count=2, superseded_fact_count=4
    )

    gaps = GapDetector.detect(bio)
    hint = GapDetector.build_prompt_hint(gaps)

    assert {gap.domain for gap in gaps} == {"basic_info", "relationships", "identity", "fact"}
    assert any(gap.gap_type == GapType.INFERRED_UNVERIFIED for gap in gaps)
    assert any(gap.gap_type == GapType.UNRESOLVED_CONFLICT for gap in gaps)
    assert hint


def test_gap_detector_when_biography_is_complete_then_no_gap_hint_is_rendered():
    bio = UserBiography(
        short_bio="This biography contains enough detail for profile completeness.",
        relationships=[{"target": "u2", "relation": "friend"}],
        identity_anchors=["developer"],
    )

    gaps = GapDetector.detect(bio)

    assert gaps == []
    assert GapDetector.build_prompt_hint(gaps) == ""
    assert KnowledgeGap("x", "domain", "desc", "low").to_dict() == {
        "gap_type": "x",
        "domain": "domain",
        "description": "desc",
        "importance": "low",
    }


def test_rhythm_analyzer_when_no_messages_then_returns_silent_cold_result():
    result = RhythmAnalyzer().analyze("g1", [])

    assert result.heat_level == "cold"
    assert result.pace == "silent"


def test_rhythm_analyzer_when_messages_are_bursty_then_detects_burst_and_attention_window():
    messages = [
        {"user_id": "u1", "content": "alpha topic one", "timestamp": _ts(12)},
        {"user_id": "u2", "content": "alpha topic two", "timestamp": _ts(11)},
        {"user_id": "u3", "content": "alpha topic three", "timestamp": _ts(10)},
        {"user_id": "u1", "content": "alpha topic four", "timestamp": _ts(9)},
        {"user_id": "u1", "content": "alpha topic five", "timestamp": _ts(8)},
        {"user_id": "u2", "content": "alpha topic six", "timestamp": _ts(7)},
        {"user_id": "u3", "content": "alpha topic seven", "timestamp": _ts(6)},
        {"user_id": "u1", "content": "alpha topic eight", "timestamp": _ts(5)},
        {"user_id": "u1", "content": "alpha topic nine", "timestamp": _ts(4)},
    ]

    result = RhythmAnalyzer().analyze("g1", messages)

    assert result.heat_level in {"hot", "overheated"}
    assert result.burst_detected is True
    assert result.attention_window_open is True
    assert result.conversation_flows >= 1
    assert 0.0 <= result.turn_gap_readiness <= 1.0


def test_rhythm_analyzer_static_helpers_when_intervals_change_then_pace_and_drift_are_reported():
    accelerating = [
        {
            "user_id": "u1",
            "content": "first shared topic",
            "timestamp": "2026-01-01T00:00:00+00:00",
        },
        {
            "user_id": "u2",
            "content": "second shared topic",
            "timestamp": "2026-01-01T00:20:00+00:00",
        },
        {
            "user_id": "u3",
            "content": "third shared topic",
            "timestamp": "2026-01-01T00:21:00+00:00",
        },
        {
            "user_id": "u4",
            "content": "fourth shared topic",
            "timestamp": "2026-01-01T00:21:30+00:00",
        },
        {
            "user_id": "u5",
            "content": "fifth shared topic",
            "timestamp": "2026-01-01T00:21:45+00:00",
        },
    ]
    drifting = [
        {"user_id": "u1", "content": "alpha beta", "timestamp": "2026-01-01T00:00:00+00:00"},
        {"user_id": "u2", "content": "alpha gamma", "timestamp": "2026-01-01T00:01:00+00:00"},
        {"user_id": "u3", "content": "delta epsilon", "timestamp": "2026-01-01T00:02:00+00:00"},
        {"user_id": "u4", "content": "zeta eta", "timestamp": "2026-01-01T00:03:00+00:00"},
    ]

    assert RhythmAnalyzer._compute_pace(accelerating) == "accelerating"
    assert RhythmAnalyzer._compute_topic_drift(drifting) > 0.5
    assert RhythmAnalyzer._compute_gap([{"timestamp": "not-a-time"}]) == 0.0
    assert RhythmAnalyzer._heat_level(0.1) == "cold"
    assert RhythmAnalyzer._heat_level(0.6) == "hot"


def test_emotion_state_when_values_are_out_of_range_then_clamps_and_serializes():
    state = EmotionState(valence=2.0, arousal=-1.0, intensity=2.0, confidence=-1.0)

    assert state.valence == 1.0
    assert state.arousal == 0.0
    assert state.intensity == 1.0
    assert state.confidence == 0.0
    assert state.basic_emotion is not None

    restored = EmotionState.from_dict(state.to_dict())

    assert restored.to_dict() == state.to_dict()


def test_emotion_state_when_basic_emotion_is_supplied_then_uses_named_enum():
    state = EmotionState.from_dict(
        {
            "valence": 0.8,
            "arousal": 0.7,
            "basic_emotion": "JOY",
            "intensity": 0.6,
            "confidence": 0.9,
        }
    )

    assert state.basic_emotion == BasicEmotion.JOY
    assert BasicEmotion.JOY.ref_valence == 0.8


def test_assistant_emotion_when_interactions_and_recovery_happen_then_moves_toward_targets():
    assistant = AssistantEmotionState(
        valence=0.2,
        arousal=0.3,
        inertia_factor=0.5,
        recovery_rate_per_10min=0.1,
        baseline_valence=0.2,
        baseline_arousal=0.3,
        user_bias={"u1": (0.8, 0.8)},
    )

    assistant.update_from_interaction(EmotionState(valence=0.9, arousal=0.8), "u1")

    assert assistant.valence > 0.2
    assert assistant.arousal > 0.3
    assert assistant.last_updated_at

    previous_valence = assistant.valence
    assistant.tick_recovery()

    assert assistant.valence < previous_valence
    assert assistant.arousal >= assistant.baseline_arousal
