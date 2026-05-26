"""ThresholdEngine 动态阈值计算测试。"""
from __future__ import annotations

from sirius_pulse.core.threshold_engine import ThresholdEngine
from sirius_pulse.memory.semantic.models import UserSemanticProfile


class TestThresholdEngineDefaults:
    """默认参数下的阈值计算。"""

    def test_default_sensitivity_produces_mid_range(self):
        engine = ThresholdEngine()
        result = engine.compute(sensitivity=0.5, heat_level="warm", hour_of_day=12)
        assert 0.3 <= result <= 0.7

    def test_high_sensitivity_lowers_threshold(self):
        engine = ThresholdEngine()
        high = engine.compute(sensitivity=1.0, heat_level="warm", hour_of_day=12)
        low = engine.compute(sensitivity=0.0, heat_level="warm", hour_of_day=12)
        assert high < low

    def test_result_clamped_to_valid_range(self):
        engine = ThresholdEngine(base_low=0.0, base_high=0.0)
        result = engine.compute(sensitivity=0.5, heat_level="cold", hour_of_day=3)
        assert result >= 0.1

    def test_result_clamped_upper_bound(self):
        engine = ThresholdEngine(base_low=2.0, base_high=2.0)
        result = engine.compute(
            sensitivity=0.0,
            heat_level="overheated",
            messages_per_minute=10,
            hour_of_day=3,
            sender_type="other_ai",
        )
        assert result <= 0.9


class TestActivityFactor:
    """热度等级对阈值的影响。"""

    def test_cold_heat_reduces_threshold(self):
        engine = ThresholdEngine()
        cold = engine.compute(sensitivity=0.5, heat_level="cold", hour_of_day=12)
        warm = engine.compute(sensitivity=0.5, heat_level="warm", hour_of_day=12)
        assert cold < warm

    def test_hot_heat_raises_threshold(self):
        engine = ThresholdEngine()
        hot = engine.compute(sensitivity=0.5, heat_level="hot", hour_of_day=12)
        warm = engine.compute(sensitivity=0.5, heat_level="warm", hour_of_day=12)
        assert hot > warm

    def test_overheated_raises_threshold_further(self):
        engine = ThresholdEngine()
        hot = engine.compute(sensitivity=0.5, heat_level="hot", hour_of_day=12)
        overheated = engine.compute(sensitivity=0.5, heat_level="overheated", hour_of_day=12)
        assert overheated > hot

    def test_high_message_rate_raises_threshold(self):
        engine = ThresholdEngine()
        fast = engine.compute(
            sensitivity=0.5, heat_level="warm", messages_per_minute=10, hour_of_day=12
        )
        slow = engine.compute(
            sensitivity=0.5, heat_level="warm", messages_per_minute=0.1, hour_of_day=12
        )
        assert fast > slow


class TestEngagementFactor:
    """用户画像对阈值的影响。"""

    def test_no_profile_returns_baseline(self):
        engine = ThresholdEngine()
        no_profile = engine.compute(sensitivity=0.5, hour_of_day=12, user_profile=None)
        assert no_profile > 0

    def test_new_user_low_engagement_factor(self):
        engine = ThresholdEngine()
        profile = UserSemanticProfile(
            engagement_rate=0.8, first_interaction_at="", interaction_count=0
        )
        result = engine.compute(sensitivity=0.5, hour_of_day=12, user_profile=profile)
        baseline = engine.compute(sensitivity=0.5, hour_of_day=12, user_profile=None)
        assert result < baseline

    def test_high_engagement_user_lowers_threshold(self):
        engine = ThresholdEngine()
        profile = UserSemanticProfile(
            engagement_rate=0.7, first_interaction_at="2026-01-01", interaction_count=50
        )
        result = engine.compute(sensitivity=0.5, hour_of_day=12, user_profile=profile)
        baseline = engine.compute(sensitivity=0.5, hour_of_day=12, user_profile=None)
        assert result < baseline

    def test_low_engagement_user_raises_threshold(self):
        engine = ThresholdEngine()
        profile = UserSemanticProfile(
            engagement_rate=0.05, first_interaction_at="2026-01-01", interaction_count=5
        )
        result = engine.compute(sensitivity=0.5, hour_of_day=12, user_profile=profile)
        baseline = engine.compute(sensitivity=0.5, hour_of_day=12, user_profile=None)
        assert result > baseline


class TestTimeFactor:
    """时段对阈值的影响。"""

    def test_night_raises_threshold(self):
        engine = ThresholdEngine()
        night = engine.compute(sensitivity=0.5, hour_of_day=3)
        day = engine.compute(sensitivity=0.5, hour_of_day=12)
        assert night > day

    def test_evening_lowers_threshold(self):
        engine = ThresholdEngine()
        evening = engine.compute(sensitivity=0.5, hour_of_day=20)
        day = engine.compute(sensitivity=0.5, hour_of_day=12)
        assert evening < day


class TestPeerFactor:
    """AI 发送者门槛提升。"""

    def test_other_ai_raises_threshold(self):
        engine = ThresholdEngine()
        ai = engine.compute(sensitivity=0.5, hour_of_day=12, sender_type="other_ai")
        human = engine.compute(sensitivity=0.5, hour_of_day=12, sender_type="human")
        assert ai > human

    def test_other_ai_factor_is_1_3(self):
        engine = ThresholdEngine()
        ai = engine.compute(sensitivity=0.5, hour_of_day=12, sender_type="other_ai")
        human = engine.compute(sensitivity=0.5, hour_of_day=12, sender_type="human")
        ratio = ai / human
        assert abs(ratio - 1.3) < 0.01
