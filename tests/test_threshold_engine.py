"""动态参与阈值的业务行为测试。"""

from __future__ import annotations

from sirius_pulse.core.threshold_engine import ThresholdEngine
from sirius_pulse.memory.semantic.models import UserSemanticProfile


def test_threshold_when_admin_sets_bot_more_expressive_then_reply_bar_gets_lower():
    engine = ThresholdEngine()

    quiet = engine.compute(sensitivity=0.0, heat_level="warm", hour_of_day=12)
    expressive = engine.compute(sensitivity=1.0, heat_level="warm", hour_of_day=12)

    assert expressive < quiet


def test_threshold_when_group_is_hot_then_bot_requires_stronger_reason_to_reply():
    engine = ThresholdEngine()

    warm = engine.compute(sensitivity=0.5, heat_level="warm", hour_of_day=12)
    hot = engine.compute(sensitivity=0.5, heat_level="hot", hour_of_day=12)
    overheated = engine.compute(sensitivity=0.5, heat_level="overheated", hour_of_day=12)

    assert warm < hot < overheated


def test_threshold_when_group_is_quiet_then_bot_can_join_more_easily():
    engine = ThresholdEngine()

    cold = engine.compute(sensitivity=0.5, heat_level="cold", hour_of_day=12)
    warm = engine.compute(sensitivity=0.5, heat_level="warm", hour_of_day=12)

    assert cold < warm


def test_threshold_when_messages_are_arriving_too_fast_then_reply_bar_rises():
    engine = ThresholdEngine()

    slow = engine.compute(
        sensitivity=0.5,
        heat_level="warm",
        messages_per_minute=0.1,
        hour_of_day=12,
    )
    fast = engine.compute(
        sensitivity=0.5,
        heat_level="warm",
        messages_per_minute=10,
        hour_of_day=12,
    )

    assert fast > slow


def test_threshold_when_user_often_engages_then_bot_can_reply_more_readily():
    engine = ThresholdEngine()
    familiar = UserSemanticProfile(
        user_id="u1",
        engagement_rate=0.7,
        interaction_count=50,
        first_interaction_at="2026-01-01T00:00:00+00:00",
    )

    baseline = engine.compute(sensitivity=0.5, hour_of_day=12)
    familiar_threshold = engine.compute(sensitivity=0.5, hour_of_day=12, user_profile=familiar)

    assert familiar_threshold < baseline


def test_threshold_when_user_rarely_responds_then_bot_becomes_more_conservative():
    engine = ThresholdEngine()
    quiet_user = UserSemanticProfile(
        user_id="u1",
        engagement_rate=0.05,
        interaction_count=5,
        first_interaction_at="2026-01-01T00:00:00+00:00",
    )

    baseline = engine.compute(sensitivity=0.5, hour_of_day=12)
    quiet_user_threshold = engine.compute(sensitivity=0.5, hour_of_day=12, user_profile=quiet_user)

    assert quiet_user_threshold > baseline


def test_threshold_when_user_is_new_then_bot_is_welcoming():
    engine = ThresholdEngine()
    new_user = UserSemanticProfile(user_id="new", engagement_rate=0.8, interaction_count=0)

    baseline = engine.compute(sensitivity=0.5, hour_of_day=12)
    new_user_threshold = engine.compute(sensitivity=0.5, hour_of_day=12, user_profile=new_user)

    assert new_user_threshold < baseline


def test_threshold_when_it_is_late_night_then_bot_replies_less_often():
    engine = ThresholdEngine()

    noon = engine.compute(sensitivity=0.5, hour_of_day=12)
    late_night = engine.compute(sensitivity=0.5, hour_of_day=3)

    assert late_night > noon


def test_threshold_when_evening_chat_time_arrives_then_bot_can_be_more_active():
    engine = ThresholdEngine()

    workday = engine.compute(sensitivity=0.5, hour_of_day=12)
    evening = engine.compute(sensitivity=0.5, hour_of_day=20)

    assert evening < workday


def test_threshold_when_sender_is_other_ai_then_bar_is_higher_than_for_human():
    engine = ThresholdEngine()

    human = engine.compute(sensitivity=0.5, hour_of_day=12, sender_type="human")
    peer_ai = engine.compute(sensitivity=0.5, hour_of_day=12, sender_type="other_ai")

    assert peer_ai > human


def test_threshold_when_extreme_inputs_arrive_then_result_stays_in_safe_range():
    low = ThresholdEngine(base_low=0.0, base_high=0.0).compute(
        sensitivity=0.5,
        heat_level="cold",
        hour_of_day=3,
    )
    high = ThresholdEngine(base_low=2.0, base_high=2.0).compute(
        sensitivity=0.0,
        heat_level="overheated",
        messages_per_minute=10,
        hour_of_day=3,
        sender_type="other_ai",
    )

    assert low == 0.1
    assert high == 0.9
