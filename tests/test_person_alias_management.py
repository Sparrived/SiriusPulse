"""Confirmed person-alias management behavior through user persona profiles."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from sirius_pulse.core.identity_resolver import IdentityResolver
from sirius_pulse.memory.profile import UserPersonaProfileManager, UserPersonaProfileStore
from sirius_pulse.memory.user.unified_manager import UnifiedUserManager
from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.skills.builtin import user_profile


def _user_manager(tmp_path: Path) -> UnifiedUserManager:
    mgr = UnifiedUserManager(db_path=tmp_path / "memory.db")
    mgr.register_user(UnifiedUser(user_id="u1", name="Alice"), group_id="g1")
    mgr.register_user(UnifiedUser(user_id="u2", name="Bob"), group_id="g1")
    return mgr


def _profile_manager(tmp_path: Path, user_manager: UnifiedUserManager) -> UserPersonaProfileManager:
    conn = sqlite3.connect(tmp_path / "profiles.db")
    store = UserPersonaProfileStore(conn=conn)
    return UserPersonaProfileManager(store, persona_name="sirius", user_manager=user_manager)


def test_profile_alias_manager_keeps_one_owner_per_alias(tmp_path: Path):
    users = _user_manager(tmp_path)
    profiles = _profile_manager(tmp_path, users)

    assert profiles.register_alias(alias="阿梨", user_id="u1", user_name="Alice", group_id="g1")["success"]
    assert profiles.register_alias(alias="小梨", user_id="u2", user_name="Bob", group_id="g1")["success"]

    uid, confidence, others = profiles.resolve_alias("阿梨", group_id="g1")
    aliases = profiles.list_alias_entries("g1")

    assert uid == "u1"
    assert confidence >= 0.5
    assert others == []
    assert aliases["阿梨"]["user_id"] == "u1"


def test_profile_aliases_are_used_by_identity_resolver(tmp_path: Path):
    users = _user_manager(tmp_path)
    profiles = _profile_manager(tmp_path, users)
    profiles.register_alias(alias="阿梨", user_id="u1", user_name="Alice", group_id="g1", confidence=0.9)
    resolver = IdentityResolver()

    resolved = resolver.resolve_with_alias(
        SimpleNamespace(
            speaker_name="阿梨",
            user_id=None,
            platform_uid=None,
            platform=None,
            is_developer=False,
        ),
        users,
        "g1",
        profile_manager=profiles,
    )

    assert resolved.user_id == "u1"
    assert resolved.source == "alias_exact"


def test_user_profile_tool_updates_alias_section(tmp_path: Path):
    users = _user_manager(tmp_path)
    profiles = _profile_manager(tmp_path, users)
    engine_context = SimpleNamespace(profile_manager=profiles)

    result = user_profile.run(
        action="update",
        target_user_id="u1",
        display_name="Alice",
        updates_json='[{"section":"aliases","key":"阿梨","value":"阿梨","confidence":0.82,"evidence":"用户明确说 Alice 也叫阿梨"}]',
        reason="用户明确说明别称",
        engine_context=engine_context,
        chat_context={"group_id": "g1", "user_id": "u1"},
    )

    assert result["success"] is True
    assert profiles.resolve_alias("阿梨", group_id="g1")[0] == "u1"


def test_user_profile_tool_can_reject_alias_item(tmp_path: Path):
    users = _user_manager(tmp_path)
    profiles = _profile_manager(tmp_path, users)
    profiles.register_alias(alias="阿梨", user_id="u1", user_name="Alice", group_id="g1")
    engine_context = SimpleNamespace(profile_manager=profiles)

    result = user_profile.run(
        action="mark",
        target_user_id="u1",
        section="aliases",
        key="阿梨",
        status="rejected",
        reason="用户纠正这个称呼不对",
        engine_context=engine_context,
        chat_context={"group_id": "g1", "user_id": "u1"},
    )

    assert result["success"] is True
    assert profiles.resolve_alias("阿梨", group_id="g1")[0] is None
