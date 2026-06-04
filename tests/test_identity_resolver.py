"""Tests for platform-agnostic identity resolution."""

from __future__ import annotations

from sirius_pulse.core.identity_resolver import IdentityContext, IdentityResolver
from sirius_pulse.memory.user.unified_manager import UnifiedUserManager
from sirius_pulse.memory.user.unified_models import UnifiedUser


def test_resolve_when_platform_identity_matches_then_returns_existing_user(tmp_path):
    manager = UnifiedUserManager(db_path=tmp_path / "persona.db")
    manager.register_user(
        UnifiedUser(
            user_id="u1",
            name="Alice",
            identities={"qq": "10001"},
        ),
        group_id="g1",
    )

    user = IdentityResolver().resolve(
        IdentityContext(speaker_name="Different Name", platform="qq", platform_uid="10001"),
        manager,
        "g1",
    )

    assert user.user_id == "u1"
    assert user.name == "Alice"


def test_resolve_when_user_is_new_then_registers_identity_and_developer_flag(tmp_path):
    manager = UnifiedUserManager(db_path=tmp_path / "persona.db")

    user = IdentityResolver().resolve(
        IdentityContext(
            speaker_name="Cara",
            user_id="u3",
            platform="discord",
            platform_uid="abc",
            is_developer=True,
        ),
        manager,
        "g1",
    )

    assert user.user_id == "u3"
    assert user.identities == {"discord": "abc"}
    assert user.metadata["is_developer"] is True
    assert manager.resolve_user_id(platform="discord", external_uid="abc") == "u3"


def test_resolve_with_alias_when_manual_alias_exists_then_reports_exact_source(tmp_path):
    manager = UnifiedUserManager(db_path=tmp_path / "persona.db")
    manager.register_user(UnifiedUser(user_id="u1", name="Alice"), group_id="g1")
    manager.register_alias("ally", "u1", "Alice", "g1", source="manual")

    resolution = IdentityResolver().resolve_with_alias(
        IdentityContext(speaker_name="ally"),
        manager,
        "g1",
    )

    assert resolution.user_id == "u1"
    assert resolution.source == "alias_exact"
    assert resolution.confidence > 0.5


def test_resolve_with_alias_when_speaker_is_bot_then_returns_assistant(tmp_path):
    manager = UnifiedUserManager(db_path=tmp_path / "persona.db")

    resolution = IdentityResolver().resolve_with_alias(
        IdentityContext(speaker_name="assistant"),
        manager,
        "g1",
    )

    assert resolution.user_id == "assistant"
    assert resolution.source == "bot_identity"
    assert resolution.confidence == 1.0


def test_similarity_when_one_label_contains_the_other_then_scores_high():
    resolver = IdentityResolver()

    score = resolver._compute_similarity("alice", "alice smith")

    assert score > 0.8
