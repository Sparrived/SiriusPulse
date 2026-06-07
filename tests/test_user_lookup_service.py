from __future__ import annotations

from types import SimpleNamespace

from sirius_pulse.core.user_lookup import UserLookupService


class FakeResolver:
    def __init__(self, resolution: SimpleNamespace | None = None, *, fail: bool = False) -> None:
        self.resolution = resolution or SimpleNamespace(
            user_id="u1",
            source="alias_exact",
            confidence=0.95,
            display_name="Fallback",
        )
        self.fail = fail
        self.calls: list[tuple[object, str]] = []

    def resolve_with_alias(self, ctx, user_manager, group_id: str):
        self.calls.append((ctx, group_id))
        if self.fail:
            raise RuntimeError("resolver failed")
        return self.resolution


class FakeUserManager:
    def __init__(self) -> None:
        self.users = {
            ("default", "u1"): SimpleNamespace(
                user_id="u1",
                name="Alice",
                aliases=["A"],
                identities={"qq": "1001"},
                is_developer=True,
            ),
            ("group-2", "u2"): SimpleNamespace(
                user_id="u2",
                name="Bob",
                aliases=[],
                identities={},
                is_developer=False,
            ),
        }
        self.fail = False
        self.requested_groups: list[str] = []

    def get_user(self, user_id: str, group_id: str):
        self.requested_groups.append(group_id)
        if self.fail:
            raise RuntimeError("manager failed")
        return self.users.get((group_id, user_id))

    def list_users(self, group_id: str):
        self.requested_groups.append(group_id)
        if self.fail:
            raise RuntimeError("manager failed")
        return [user for (gid, _), user in self.users.items() if gid == group_id]


def test_user_lookup_when_platform_uid_resolves_then_returns_profile_name_and_source():
    resolver = FakeResolver()
    manager = FakeUserManager()
    service = UserLookupService(resolver, manager)

    result = service.find_by_platform_uid("qq", "1001")

    assert result == {
        "user_id": "u1",
        "name": "Alice",
        "confidence": 0.95,
        "source": "alias_exact",
    }
    ctx, group_id = resolver.calls[0]
    assert ctx.platform == "qq"
    assert ctx.platform_uid == "1001"
    assert group_id == "default"


def test_user_lookup_when_name_resolves_without_profile_then_uses_display_name():
    resolver = FakeResolver(
        SimpleNamespace(
            user_id="u-missing", source="alias_fuzzy", confidence=0.6, display_name="Display"
        )
    )
    manager = FakeUserManager()
    service = UserLookupService(resolver, manager)

    result = service.find_by_name("Display", group_id="group-2")

    assert result == {
        "user_id": "u-missing",
        "name": "Display",
        "confidence": 0.6,
        "source": "alias_fuzzy",
    }
    assert resolver.calls[0][1] == "group-2"


def test_user_lookup_when_resolution_is_unresolved_or_input_empty_then_returns_none():
    unresolved = SimpleNamespace(user_id="", source="unresolved", confidence=0.0, display_name="")
    service = UserLookupService(FakeResolver(unresolved), FakeUserManager())

    assert service.find_by_name("") is None
    assert service.find_by_name("Nobody") is None
    assert service.find_by_platform_uid("qq", "missing") is None


def test_user_lookup_when_manager_lists_and_reads_users_then_shapes_public_dicts():
    service = UserLookupService(FakeResolver(), FakeUserManager())

    assert service.get_info("u1") == {
        "user_id": "u1",
        "name": "Alice",
        "aliases": ["A"],
        "identities": {"qq": "1001"},
        "is_developer": True,
    }
    assert service.list_users("group-2") == [
        {"user_id": "u2", "name": "Bob", "aliases": [], "is_developer": False}
    ]
    assert service.get_self_id() == "assistant"


def test_user_lookup_when_dependencies_raise_then_returns_safe_defaults():
    manager = FakeUserManager()
    manager.fail = True
    service = UserLookupService(FakeResolver(fail=True), manager)

    assert service.find_by_name("Alice") is None
    assert service.find_by_platform_uid("qq", "1001") is None
    assert service.get_info("u1") is None
    assert service.list_users() == []


def test_user_lookup_when_engine_has_bot_uids_then_resolves_specific_current_or_first():
    engine = SimpleNamespace(
        _bot_platform_uids={"qq": "bot-qq", "discord": "bot-discord"},
        _current_adapter_type="discord",
    )
    service = UserLookupService(FakeResolver(), FakeUserManager(), engine=engine)

    assert service.get_bot_platform_uid("qq") == "bot-qq"
    assert service.get_bot_platform_uid() == "bot-discord"
    assert service.get_bot_platform_uids() == {"qq": "bot-qq", "discord": "bot-discord"}

    engine._current_adapter_type = ""

    assert service.get_bot_platform_uid() == "bot-qq"
    assert UserLookupService(FakeResolver(), FakeUserManager()).get_bot_platform_uid() is None
