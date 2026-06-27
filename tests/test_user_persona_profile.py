from __future__ import annotations

import sqlite3

from sirius_pulse.memory.profile import UserPersonaProfileManager, UserPersonaProfileStore


class DummySemanticProfile:
    engagement_rate = 0.4

    def compute_familiarity(self) -> float:
        return 0.7


class DummySemanticMemory:
    def get_user_profile(self, group_id: str, user_id: str) -> DummySemanticProfile:
        return DummySemanticProfile()


class DummyUser:
    name = "Alice"


class DummyUserManager:
    def get_user(self, user_id: str, group_id: str = "default") -> DummyUser | None:
        return DummyUser()

    def get_global_user(self, user_id: str) -> DummyUser | None:
        return DummyUser()


def make_manager() -> UserPersonaProfileManager:
    conn = sqlite3.connect(":memory:")
    store = UserPersonaProfileStore(conn=conn)
    return UserPersonaProfileManager(
        store,
        persona_name="sirius",
        user_manager=DummyUserManager(),
        semantic_memory=DummySemanticMemory(),
    )


def test_profile_update_renders_profile_card() -> None:
    manager = make_manager()

    result = manager.update_profile(
        group_id="g1",
        user_id="u1",
        display_name="Alice",
        short_impression="偏好直接深入的技术讨论",
        updates=[
            {
                "section": "preferences",
                "key": "response_style",
                "value": "喜欢少废话、直接给结论和方案",
                "confidence": 0.9,
                "evidence": "用户明确表达",
            }
        ],
        reason="长期影响回复风格",
    )

    assert result["success"] is True
    card = result["profile_card"]
    assert "Alice" in card
    assert "偏好直接深入" in card
    assert "喜欢少废话" in card


def test_aliases_are_stored_and_resolved_in_profile() -> None:
    manager = make_manager()

    result = manager.register_alias(
        alias="小爱",
        user_id="u1",
        user_name="Alice",
        group_id="g1",
        confidence=0.85,
        evidence="用户明确说小爱是 Alice",
    )

    assert result["success"] is True
    resolved, confidence, candidates = manager.resolve_alias("小爱", group_id="g1")
    assert resolved == "u1"
    assert confidence == 0.85
    assert candidates == []
    assert manager.get_aliases_for_group("g1") == {"小爱": "Alice"}


def test_delete_alias_marks_profile_item_rejected() -> None:
    manager = make_manager()
    manager.register_alias(alias="小爱", user_id="u1", user_name="Alice", group_id="g1")

    assert manager.delete_alias("小爱", "u1", "g1") is True
    resolved, confidence, _ = manager.resolve_alias("小爱", group_id="g1")

    assert resolved is None
    assert confidence == 0.0
