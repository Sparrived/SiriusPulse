"""Confirmed person-alias management behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sirius_pulse.core.identity_resolver import IdentityResolver
from sirius_pulse.core.skill_engine_context import SkillEngineContextImpl
from sirius_pulse.memory.evolution.chain import EvolutionChain
from sirius_pulse.memory.user.unified_manager import UnifiedUserManager
from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.skills.builtin import person_alias
from sirius_pulse.skills.data_store import SkillDataStore


def _manager(tmp_path: Path) -> UnifiedUserManager:
    mgr = UnifiedUserManager(db_path=tmp_path / "memory.db")
    mgr.register_user(UnifiedUser(user_id="u1", name="Alice"), group_id="g1")
    mgr.register_user(UnifiedUser(user_id="u2", name="Bob"), group_id="g1")
    return mgr


def test_alias_manager_rejects_generic_address_terms(tmp_path: Path):
    mgr = _manager(tmp_path)

    saved = mgr.register_alias("哥", "u1", "Alice", "g1", source="model_skill", confidence=0.9)
    resolved = mgr.resolve_alias("哥", group_id="g1")

    assert saved is False
    assert resolved == (None, 0.0, [])


def test_alias_manager_keeps_one_owner_per_alias(tmp_path: Path):
    mgr = _manager(tmp_path)

    assert mgr.register_alias("小梨", "u1", "Alice", "g1", source="model_skill", confidence=0.8)
    assert mgr.register_alias("小梨", "u2", "Bob", "g1", source="model_skill", confidence=0.9)

    uid, confidence, others = mgr.resolve_alias("小梨", group_id="g1")
    aliases = mgr.list_alias_entries("g1")

    assert uid == "u2"
    assert confidence == 0.9
    assert others == []
    assert aliases["小梨"]["user_id"] == "u2"


def test_feed_messages_does_not_register_discovered_aliases(tmp_path: Path):
    mgr = _manager(tmp_path)

    mgr.feed_messages(
        user_id="u1",
        name="Alice",
        group_id="g1",
        messages=["Alice 说她也叫阿梨"],
        discovered_aliases=["阿梨"],
    )

    assert mgr.resolve_alias("阿梨", group_id="g1") == (None, 0.0, [])


def test_profile_aliases_do_not_bypass_confirmed_alias_index(tmp_path: Path):
    mgr = UnifiedUserManager(db_path=tmp_path / "memory.db")
    mgr.register_user(
        UnifiedUser(user_id="u1", name="Alice", aliases=["阿梨"]),
        group_id="g1",
    )
    resolver = IdentityResolver()

    direct = mgr.resolve_user_id(speaker="阿梨")
    resolved = resolver.resolve_with_alias(
        SimpleNamespace(
            speaker_name="阿梨",
            user_id=None,
            platform_uid=None,
            platform=None,
            is_developer=False,
        ),
        mgr,
        "g1",
    )

    assert direct is None
    assert resolved.user_id == "阿梨"
    assert resolved.source == "unresolved"


def test_person_alias_skill_rejects_low_confidence_and_generic_alias(tmp_path: Path):
    calls: list[dict] = []

    class EngineContext:
        def manage_person_alias(self, **kwargs):
            calls.append(kwargs)
            return {"success": True}

    low_conf = person_alias.run(
        action="add",
        alias="阿梨",
        target_user_id="u1",
        confidence=0.4,
        engine_context=EngineContext(),
        chat_context={"group_id": "g1"},
    )
    generic = person_alias.run(
        action="add",
        alias="姐姐",
        target_user_id="u1",
        confidence=0.9,
        engine_context=EngineContext(),
        chat_context={"group_id": "g1"},
    )

    assert low_conf["success"] is False
    assert generic["success"] is False
    assert calls == []


def test_person_alias_skill_records_model_self_mapping(tmp_path: Path):
    store = SkillDataStore(tmp_path / "person_alias.json")

    class EngineContext:
        def manage_person_alias(self, **kwargs):
            return {
                "success": True,
                "alias": kwargs["alias"],
                "user_id": kwargs["target_user_id"],
                "user_name": "Alice",
                "confidence": kwargs["confidence"],
            }

    result = person_alias.run(
        action="add",
        alias="阿梨",
        target_user_id="u1",
        confidence=0.82,
        evidence="用户明确说 Alice 也叫阿梨",
        engine_context=EngineContext(),
        chat_context={"group_id": "g1"},
        data_store=store,
    )
    store.save()
    reloaded = SkillDataStore(tmp_path / "person_alias.json")

    assert result["success"] is True
    assert reloaded.get("aliases")["阿梨"]["user_id"] == "u1"
    assert reloaded.get("aliases")["阿梨"]["confidence"] == 0.82


def test_engine_context_person_alias_add_resolves_target_name(tmp_path: Path):
    mgr = _manager(tmp_path)
    engine = SimpleNamespace(
        user_manager=mgr,
        identity_resolver=IdentityResolver(),
        _skill_registry=None,
        _skill_executor=None,
        _group_last_message_at={},
        _current_adapter_type="",
    )
    ctx = SkillEngineContextImpl(engine)

    result = ctx.manage_person_alias(
        action="add",
        alias="阿梨",
        target_name="Alice",
        group_id="g1",
        confidence=0.88,
        evidence="用户明确说明",
    )

    assert result["success"] is True
    assert mgr.resolve_alias("阿梨", group_id="g1")[0] == "u1"


def test_evolution_alias_api_uses_same_guardrails(tmp_path: Path):
    chain = EvolutionChain(tmp_path / "evolution.db")

    assert chain.register_alias("哥", "u1", "Alice", "g1") is False
    assert chain.register_alias("阿梨", "u1", "Alice", "g1") is True
    assert chain.register_alias("阿梨", "u2", "Bob", "g1") is True

    uid, confidence, candidates = chain.resolve_alias("阿梨", group_id="g1")

    assert uid == "u2"
    assert confidence == 0.5
    assert candidates == []
