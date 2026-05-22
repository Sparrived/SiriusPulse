"""Tests for CognitionAnalyzer: unified emotion + intent analysis."""

from __future__ import annotations

import pytest

from sirius_pulse.core.cognition import CognitionAnalyzer
from sirius_pulse.models.emotion import EmotionState
from sirius_pulse.models.intent_v3 import SocialIntent, IntentAnalysisV3


class TestCognitionAnalyzerRules:
    @pytest.mark.asyncio
    async def test_positive_emotion_detected(self):
        ca = CognitionAnalyzer()
        emotion, intent, empathy = await ca.analyze("太开心了！", "u1", "g1")
        assert emotion.valence > 0.3
        assert emotion.basic_emotion is not None

    @pytest.mark.asyncio
    async def test_negative_emotion_detected(self):
        ca = CognitionAnalyzer()
        emotion, intent, empathy = await ca.analyze("好难过，崩溃了", "u1", "g1")
        assert emotion.valence < -0.3

    @pytest.mark.asyncio
    async def test_help_seeking_intent(self):
        ca = CognitionAnalyzer()
        emotion, intent, empathy = await ca.analyze("怎么安装Python？", "u1", "g1")
        assert intent.social_intent == SocialIntent.HELP_SEEKING

    @pytest.mark.asyncio
    async def test_emotional_intent(self):
        ca = CognitionAnalyzer()
        emotion, intent, empathy = await ca.analyze("感觉好孤独", "u1", "g1")
        assert intent.social_intent == SocialIntent.EMOTIONAL

    @pytest.mark.asyncio
    async def test_silent_intent_short_message(self):
        ca = CognitionAnalyzer()
        emotion, intent, empathy = await ca.analyze("哈哈", "u1", "g1")
        assert intent.social_intent == SocialIntent.SILENT

    @pytest.mark.asyncio
    async def test_empathy_strategy_for_negative(self):
        ca = CognitionAnalyzer()
        emotion, intent, empathy = await ca.analyze("愤怒！完全无法接受！", "u1", "g1")
        # Negative valence -> cognitive or confirm_action (both negative strategies)
        assert empathy.strategy_type in ("cognitive", "confirm_action")
        assert empathy.priority <= 2

    @pytest.mark.asyncio
    async def test_empathy_strategy_for_positive(self):
        ca = CognitionAnalyzer()
        emotion, intent, empathy = await ca.analyze("太棒了！超级开心！", "u1", "g1")
        assert empathy.strategy_type == "share_joy"


class TestCognitionAnalyzerContext:
    @pytest.mark.asyncio
    async def test_trajectory_tracking(self):
        ca = CognitionAnalyzer()
        await ca.analyze("还行", "u1", "g1")
        await ca.analyze("不错", "u1", "g1")
        assert "u1" in ca.trajectories
        assert len(ca.trajectories["u1"]) == 2

    @pytest.mark.asyncio
    async def test_group_sentiment_update(self):
        ca = CognitionAnalyzer()
        emotion, intent, empathy = await ca.analyze("好开心", "u1", "g1")
        ca.update_group_sentiment("g1", emotion)
        assert "g1" in ca.group_cache
        assert ca.group_cache["g1"].valence > 0


class TestCognitionAnalyzerUrgency:
    @pytest.mark.asyncio
    async def test_high_urgency_keywords(self):
        ca = CognitionAnalyzer()
        emotion, intent, empathy = await ca.analyze("崩溃了，救命啊！", "u1", "g1")
        assert intent.urgency_score >= 25

    @pytest.mark.asyncio
    async def test_emotion_boosts_urgency(self):
        ca = CognitionAnalyzer()
        emotion, intent, empathy = await ca.analyze("绝望了，怎么办", "u1", "g1")
        # Negative valence + high arousal should boost urgency
        assert intent.urgency_score > 0


class TestCognitionAnalyzerFusion:
    @pytest.mark.asyncio
    async def test_empty_message(self):
        ca = CognitionAnalyzer()
        emotion, intent, empathy = await ca.analyze("", "u1", "g1")
        assert emotion.confidence == 0.5
        assert intent.social_intent == SocialIntent.SILENT
