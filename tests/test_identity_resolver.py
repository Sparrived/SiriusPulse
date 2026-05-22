"""Tests for identity resolver."""

from __future__ import annotations

from sirius_pulse.core.identity_resolver import IdentityContext, IdentityResolver
from sirius_pulse.memory.user.simple import UserManager, UserProfile


class TestIdentityResolver:
    def test_resolve_by_user_id(self) -> None:
        mgr = UserManager()
        mgr.register_user(UserProfile(user_id="u1", name="Alice"), "g1")
        resolver = IdentityResolver()
        ctx = IdentityContext(speaker_name="Bob", user_id="u1")
        profile = resolver.resolve(ctx, mgr, "g1")
        assert profile.user_id == "u1"
        assert profile.name == "Alice"

    def test_resolve_by_platform_uid(self) -> None:
        mgr = UserManager()
        mgr.register_user(
            UserProfile(user_id="u1", name="Alice", identities={"qq": "12345"}),
            "g1",
        )
        resolver = IdentityResolver()
        ctx = IdentityContext(speaker_name="Alice", platform="qq", platform_uid="12345")
        profile = resolver.resolve(ctx, mgr, "g1")
        assert profile.user_id == "u1"

    def test_resolve_by_speaker_name(self) -> None:
        mgr = UserManager()
        mgr.register_user(UserProfile(user_id="u1", name="Alice"), "g1")
        resolver = IdentityResolver()
        ctx = IdentityContext(speaker_name="Alice")
        profile = resolver.resolve(ctx, mgr, "g1")
        assert profile.user_id == "u1"

    def test_resolve_creates_new_user(self) -> None:
        mgr = UserManager()
        resolver = IdentityResolver()
        ctx = IdentityContext(speaker_name="Charlie", platform="qq", platform_uid="99999")
        profile = resolver.resolve(ctx, mgr, "g1")
        assert profile.name == "Charlie"
        assert profile.identities.get("qq") == "99999"
