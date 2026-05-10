"""Tests for SemanticMemoryManager and SemanticProfileStore."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from sirius_chat.memory.semantic.manager import SemanticMemoryManager
from sirius_chat.memory.semantic.models import (
    AtmosphereSnapshot,
    GroupSemanticProfile,
    ResponseRecord,
    UserSemanticProfile,
)


@pytest.fixture
def manager():
    with tempfile.TemporaryDirectory() as tmp:
        yield SemanticMemoryManager(tmp)


# ==================================================================
# Group profile persistence
# ==================================================================

class TestGroupProfilePersistence:
    def test_ensure_group_profile_creates_default(self, manager):
        profile = manager.ensure_group_profile("g1")
        assert isinstance(profile, GroupSemanticProfile)
        assert profile.group_id == "g1"

    def test_group_profile_persists_to_disk(self, manager):
        profile = manager.ensure_group_profile("g1")
        profile.group_norms["test_key"] = "test_value"
        manager.save_group_profile("g1")

        manager2 = SemanticMemoryManager(manager._store._base.parent.parent)
        loaded = manager2.ensure_group_profile("g1")
        assert loaded.group_norms.get("test_key") == "test_value"

    def test_atmosphere_history_limit(self, manager):
        for i in range(110):
            manager.record_atmosphere("g1", valence=0.1, arousal=0.2, active_participants=3)
        profile = manager.ensure_group_profile("g1")
        assert len(profile.atmosphere_history) == 100
        assert profile.atmosphere_history[-1].group_valence == 0.1


# ==================================================================
# User profile persistence
# ==================================================================

class TestUserProfilePersistence:
    def test_get_user_profile_creates_default(self, manager):
        profile = manager.get_user_profile("g1", "u1")
        assert isinstance(profile, UserSemanticProfile)
        assert profile.user_id == "u1"

    def test_user_profile_persists_to_disk(self, manager):
        profile = manager.get_user_profile("g1", "u1")
        profile.name = "test_user"
        manager.save_user_profile("g1", "u1")

        manager2 = SemanticMemoryManager(manager._store._base.parent.parent)
        loaded = manager2.get_user_profile("g1", "u1")
        assert loaded.name == "test_user"

    def test_list_group_user_profiles(self, manager):
        manager.get_user_profile("g1", "u1")
        manager.get_user_profile("g1", "u2")
        manager.save_user_profile("g1", "u1")
        manager.save_user_profile("g1", "u2")

        profiles = manager.list_group_user_profiles("g1")
        assert len(profiles) == 2
        user_ids = {p.user_id for p in profiles}
        assert user_ids == {"u1", "u2"}


# ==================================================================
# Passive learning (group norms)
# ==================================================================

class TestPassiveLearning:
    def test_learn_message_increments_count(self, manager):
        manager.learn_from_message("g1", "hello world", social_intent="chat")
        profile = manager.ensure_group_profile("g1")
        assert profile.group_norms.get("message_count") == 1

    def test_learn_multiple_messages(self, manager):
        for i in range(5):
            manager.learn_from_message("g1", f"msg {i}", social_intent="chat")
        profile = manager.ensure_group_profile("g1")
        assert profile.group_norms.get("message_count") == 5

    def test_emoji_detection(self, manager):
        manager.learn_from_message("g1", "hello 😊", social_intent="chat")
        manager.learn_from_message("g1", "no emoji", social_intent="chat")
        profile = manager.ensure_group_profile("g1")
        assert profile.group_norms.get("emoji_total") == 1
        assert profile.group_norms.get("emoji_usage_rate") == 0.5

    def test_mention_detection(self, manager):
        manager.learn_from_message("g1", "@user hello", social_intent="chat")
        manager.learn_from_message("g1", "plain text", social_intent="chat")
        profile = manager.ensure_group_profile("g1")
        assert profile.group_norms.get("mention_total") == 1
        assert profile.group_norms.get("mention_rate") == 0.5

    def test_topic_switch_tracking(self, manager):
        manager.learn_from_message("g1", "a", social_intent="chat")
        manager.learn_from_message("g1", "b", social_intent="help")
        manager.learn_from_message("g1", "c", social_intent="chat")
        profile = manager.ensure_group_profile("g1")
        assert profile.group_norms.get("topic_switches") == 2

    def test_keyword_extraction_no_longer_in_passive_learning(self, manager):
        for _ in range(5):
            manager.learn_from_message("g1", "python asyncio", social_intent="chat")
        profile = manager.ensure_group_profile("g1")
        assert profile.interest_topics == []
        assert profile.dominant_topic == ""


# ==================================================================
# Atmosphere recording
# ==================================================================

class TestAtmosphereRecording:
    def test_record_atmosphere_appends(self, manager):
        manager.record_atmosphere("g1", valence=0.5, arousal=0.3, active_participants=2)
        profile = manager.ensure_group_profile("g1")
        assert len(profile.atmosphere_history) == 1
        snap = profile.atmosphere_history[0]
        assert isinstance(snap, AtmosphereSnapshot)
        assert snap.group_valence == 0.5
        assert snap.group_arousal == 0.3
        assert snap.active_participants == 2


# ==================================================================
# User interaction recording
# ==================================================================

class TestUserInteraction:
    def test_record_interaction_increments_count(self, manager):
        manager.record_user_interaction("g1", "u1")
        manager.record_user_interaction("g1", "u1")
        profile = manager.get_user_profile("g1", "u1")
        assert profile.interaction_count == 2
        assert profile.first_interaction_at != ""
        assert profile.last_interaction_at != ""

    def test_compute_familiarity_from_count(self, manager):
        profile = manager.get_user_profile("g1", "u1")
        assert profile.compute_familiarity() == 0.0
        profile.interaction_count = 50
        assert profile.compute_familiarity() == pytest.approx(1.0, abs=0.01)


# ==================================================================
# Response feedback tracking
# ==================================================================

class TestResponseFeedback:
    def test_record_response_sent_creates_pending(self, manager):
        manager.record_response_sent("g1", "u1", topic_hint="test", response_length=20)
        profile = manager.get_user_profile("g1", "u1")
        assert len(profile.pending_responses) == 1
        rec = profile.pending_responses[0]
        assert rec.target_user_id == "u1"
        assert rec.topic_hint == "test"
        assert rec.response_length == 20

    def test_resolve_engages_with_directed_score(self, manager):
        manager.record_response_sent("g1", "u1", response_length=10)
        profile = manager.get_user_profile("g1", "u1")
        assert len(profile.pending_responses) == 1

        # directed_score >= 0.3 才算真正的指向 AI 的回应
        manager.resolve_pending_feedback("g1", "u1", directed_score=0.5)
        profile = manager.get_user_profile("g1", "u1")
        assert len(profile.pending_responses) == 0
        assert profile.engagement_rate > 0

    def test_resolve_ignores_non_directed(self, manager):
        manager.record_response_sent("g1", "u1", response_length=10)
        # directed_score < 0.3 → 不结算（群聊噪音）
        manager.resolve_pending_feedback("g1", "u1", directed_score=0.1)
        profile = manager.get_user_profile("g1", "u1")
        # 记录仍留在 pending 中
        assert len(profile.pending_responses) == 1
        assert profile.engagement_rate == 0.0

    def test_pending_limit(self, manager):
        for _ in range(25):
            manager.record_response_sent("g1", "u1", response_length=10)
        profile = manager.get_user_profile("g1", "u1")
        assert len(profile.pending_responses) == 20

    def test_group_level_engagement(self, manager):
        manager.record_response_sent("g1", "u1", response_length=10)
        group = manager.get_group_profile("g1")
        assert len(group.pending_ai_responses) == 1

        manager.resolve_pending_feedback("g1", "u1", directed_score=0.5)
        group = manager.get_group_profile("g1")
        assert len(group.pending_ai_responses) == 0
        assert group.response_engagement_rate > 0

    def test_engagement_rate_persistence(self, manager):
        manager.record_response_sent("g1", "u1", response_length=10)
        manager.resolve_pending_feedback("g1", "u1", directed_score=0.5)
        manager.save_user_profile("g1", "u1")

        manager2 = SemanticMemoryManager(manager._store._base.parent.parent)
        loaded = manager2.get_user_profile("g1", "u1")
        assert loaded.engagement_rate > 0


# ==================================================================
# Integration: proactive topic selection
# ==================================================================

class TestProactiveTopicSelection:
    def test_pick_topic_from_interest_topics(self, manager):
        profile = manager.ensure_group_profile("g1")
        profile.interest_topics = ["gaming", "music"]
        manager.save_group_profile("g1")

        group_profile = manager.get_group_profile("g1")
        candidates = list(group_profile.interest_topics)
        assert "gaming" in candidates

    def test_pick_topic_from_dominant_topic(self, manager):
        profile = manager.ensure_group_profile("g1")
        profile.dominant_topic = "artificial intelligence"
        manager.save_group_profile("g1")
        loaded = manager.ensure_group_profile("g1")
        assert loaded.dominant_topic == "artificial intelligence"

    def test_user_level_interests(self, manager):
        profile = manager.get_user_profile("g1", "u1")
        profile.interest_graph = [{"topic": "coding", "participation": 0.5}]
        manager.save_user_profile("g1", "u1")

        loaded = manager.get_user_profile("g1", "u1")
        assert len(loaded.interest_graph) == 1
