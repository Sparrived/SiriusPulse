"""人物身份与画像工具的业务场景回归。"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sirius_pulse.core.prompt_factory import PromptFactory
from sirius_pulse.memory.user.unified_manager import UnifiedUserManager
from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.people import PersonAccess, PersonError, PersonService
from sirius_pulse.tools.executor import ToolExecutor, _coerce_type
from sirius_pulse.tools.models import ToolInvocationContext
from sirius_pulse.tools.registry import ToolRegistry


@pytest.fixture
def people_factory(tmp_path: Path):
    services = []

    def create(**kwargs):
        service = PersonService(tmp_path, **kwargs)
        services.append(service)
        return service

    try:
        yield create
    finally:
        for service in reversed(services):
            service.conn.close()


def test_account_identity_and_scoped_name_updates_survive_reopen(tmp_path: Path) -> None:
    manager = UnifiedUserManager(tmp_path, persona_name="月白")
    people = manager.people
    ref = people.record_evidence(
        group_id="g1",
        user_id="legacy-1",
        platform="qq",
        platform_uid="1001",
        message_id="m1",
        content="叫我阿雀，Sparrived也是我",
    )
    actor = people.get_evidence(ref)["actor_id"]
    access = PersonAccess(actor_id=actor, group_id="g1", evidence_refs=(ref,))
    assert people.lookup_account("QQ", "1001") == actor
    assert (
        people.update_names(
            actor, access, ref, preferred_name="阿雀", add_aliases=["Sparrived"], expected_revision=1
        )["display_name"]
        == "阿雀"
    )
    same = people.record_evidence(
        group_id="g2",
        user_id="legacy-1",
        platform="qq",
        platform_uid="1001",
        message_id="m2",
        content="继续聊",
    )
    assert people.get_evidence(same)["actor_id"] == actor
    manager.close()
    manager2 = UnifiedUserManager(tmp_path, persona_name="月白")
    assert manager2.people.lookup_account("qq", "1001") == actor
    manager2.close()


def test_identical_names_do_not_merge_and_write_requires_current_evidence(people_factory) -> None:
    people = people_factory()
    first = people.record_evidence(
        group_id="g", user_id="a", platform="qq", platform_uid="1", message_id="1", content="hi"
    )
    second = people.record_evidence(
        group_id="g", user_id="b", platform="qq", platform_uid="2", message_id="2", content="hi"
    )
    a, b = people.get_evidence(first)["actor_id"], people.get_evidence(second)["actor_id"]
    assert a != b
    with pytest.raises(PersonError, match="permission_denied"):
        people.require_write(
            a, PersonAccess(actor_id=b, group_id="g", evidence_refs=(second,)), second
        )
    with pytest.raises(PersonError, match="source_ambiguous"):
        people.resolve_target(
            {"ref": "requester"}, PersonAccess(group_id="g", evidence_refs=(first, second))
        )


def test_character_creation_is_idempotent_and_profile_claim_correction_is_traceable(
    people_factory,
) -> None:
    people = people_factory()
    ref = people.record_evidence(
        group_id="g",
        user_id="a",
        platform="qq",
        platform_uid="1",
        message_id="1",
        content="这是我原创的角色小羽",
    )
    actor = people.get_evidence(ref)["actor_id"]
    access = PersonAccess(actor_id=actor, group_id="g", evidence_refs=(ref,))
    character = people.create_character("小羽", access, ref)
    repeated = people.create_character("小羽", access, ref)
    assert character["person_id"] == repeated["person_id"]
    assert character["kind"] == "character"
    people.propose_claim(actor, "preference", "蓝色", access, ref)
    people.propose_claim(actor, "preference", "不喜欢蓝色", access, ref)
    active = people.get_person(actor, access)["claims"]
    assert len(active) == 1 and active[0]["value"] == "不喜欢蓝色"


class ToolContext:
    def __init__(self, people: PersonService, access: PersonAccess) -> None:
        self.people, self.access = people, access

    def get_person_service(self) -> PersonService:
        return self.people

    def get_person_access(self, invocation_context: object) -> PersonAccess:
        return self.access

    def get_visual_service(self) -> None:
        return None


def evidence(
    people: PersonService, group: str, uid: str, message: str, content: str = ""
) -> tuple[str, PersonAccess]:
    ref = people.record_evidence(
        group_id=group,
        user_id="legacy-" + uid,
        platform="qq",
        platform_uid=uid,
        message_id=message,
        content=content,
    )
    actor = people.get_evidence(ref)["actor_id"]
    return ref, PersonAccess(actor_id=actor, group_id=group, evidence_refs=(ref,))


def update(people: PersonService, access: PersonAccess, **params: object) -> dict:
    from sirius_pulse.tools.builtin.person_update import run

    params.setdefault("evidence_ref", access.evidence_refs[0])
    return asyncio.run(
        run(engine_context=ToolContext(people, access), invocation_context=object(), **params)
    )


def query(people: PersonService, access: PersonAccess, **params: object) -> dict:
    from sirius_pulse.tools.builtin.person_query import run

    return asyncio.run(
        run(engine_context=ToolContext(people, access), invocation_context=object(), **params)
    )


def test_scoped_names_claims_and_candidates_never_cross_groups(people_factory) -> None:
    people = people_factory(persona_name="月白")
    ref_a, alice_a = evidence(people, "A", "alice", "a1")
    ref_b, bob_b = evidence(people, "B", "bob", "b1")
    assert update(
        people,
        alice_a,
        action="set_names",
        target={"ref": "requester"},
        preferred_name="PRIVATE_A",
        add_aliases=["ALICE_SECRET"],
    )["success"]
    assert update(
        people,
        alice_a,
        action="propose_claim",
        target={"ref": "requester"},
        field="private",
        value="SECRET_A",
    )["success"]
    _, alice_b = evidence(people, "B", "alice", "a2")
    assert update(
        people, alice_b, action="set_names", target={"ref": "requester"}, preferred_name="PUBLIC_B"
    )["success"]
    assert (
        query(people, bob_b, target={"ref": "name", "value": "PRIVATE_A"})["status"]
        == "not_found_or_not_visible"
    )
    visible = query(
        people, bob_b, target={"ref": "name", "value": "PUBLIC_B"}, sections=["names", "claims"]
    )
    assert "SECRET_A" not in str(visible) and "PRIVATE_A" not in str(visible)
    assert "PUBLIC_B" in str(visible)
    self_view = people.get_person(alice_a.actor_id, alice_b)
    assert self_view["display_name"] == "PUBLIC_B"
    # The platform nickname is not an authoritative name collision across scopes.
    same_a, a_access = evidence(people, "A", "same-a", "same1")
    same_b, b_access = evidence(people, "A", "same-b", "same2")
    update(people, a_access, action="set_names", target={"ref": "requester"}, preferred_name="小白")
    update(people, b_access, action="set_names", target={"ref": "requester"}, preferred_name="小白")
    result = query(people, a_access, target={"ref": "name", "value": "小白"})
    assert result["status"] == "ambiguous" and len(result["candidates"]) == 2
    _, moon = evidence(people, "A", "moon", "moon1")
    update(people, moon, action="set_names", target={"ref": "requester"}, preferred_name="月白")
    assert query(people, a_access, target={"ref": "name", "value": "月白"})["status"] == "ambiguous"
    assert (
        query(people, a_access, target={"ref": "requester"}, sections=["nope"])["error"]
        == "invalid_sections"
    )
    page = query(people, a_access, target={"ref": "name", "value": "小白"}, cursor="forged")
    assert page["error"] == "cursor_invalid"
    assert ref_a != ref_b


def test_claim_proposal_confirmation_correction_and_idempotency(people_factory) -> None:
    people = people_factory()
    ref_b, bob = evidence(people, "G", "bob", "1")
    _, alice = evidence(people, "G", "alice", "2")
    result = update(
        people,
        bob,
        action="propose_claim",
        target={"ref": "person_id", "value": alice.actor_id},
        field="color",
        value="blue",
        source="self_reported",
    )
    assert result["status"] == "pending" and result["claim_id"]
    assert people.get_person(alice.actor_id, alice)["claims"][0]["source"] == "third_party"
    before = people.get_person(alice.actor_id, alice)["revision"]
    confirmed = update(
        people,
        alice,
        action="confirm_change",
        target={"ref": "requester"},
        change_id=result["change_id"],
        expected_revision=before,
    )
    assert (
        confirmed["success"] and people.get_person(alice.actor_id, alice)["revision"] == before + 1
    )
    corrected = update(
        people,
        alice,
        action="correct_claim",
        target={"ref": "requester"},
        claim_id=result["claim_id"],
        value="green",
        expected_revision=before + 1,
    )
    assert corrected["success"]
    assert people.get_person(alice.actor_id, alice)["claims"][0]["value"] == "green"
    assert (
        update(
            people,
            alice,
            action="correct_claim",
            target={"ref": "requester"},
            claim_id=result["claim_id"],
            value="green",
            expected_revision=before + 1,
        )["status"]
        == "already_processed"
    )
    assert (
        update(
            people,
            alice,
            action="correct_claim",
            target={"ref": "requester"},
            claim_id=result["claim_id"],
            value="red",
            expected_revision=before + 1,
        )["error"]
        == "revision_conflict"
    )
    assert (
        update(people, alice, action="retire_claim", target={"ref": "requester"}, value="x")[
            "error"
        ]
        == "invalid_argument"
    )
    assert update(
        people,
        alice,
        action="retire_claim",
        target={"ref": "requester"},
        claim_id=corrected["claim_id"],
    )["success"]
    assert people.get_person(alice.actor_id, alice)["claims"] == []
    _, fresh = evidence(people, "G", "alice", "3")
    first = update(
        people,
        fresh,
        action="set_names",
        target={"ref": "requester"},
        preferred_name="阿雀",
        expected_revision=people.get_person(alice.actor_id, fresh)["revision"],
    )
    second = update(
        people,
        fresh,
        action="set_names",
        target={"ref": "requester"},
        preferred_name="阿雀",
        expected_revision=people.get_person(alice.actor_id, fresh)["revision"],
    )
    assert first["success"] and second["change_id"] == first["change_id"]
    assert second["revision"] == first["revision"]
    _, fourth = evidence(people, "G", "alice", "4")
    assert (
        update(
            people,
            fourth,
            action="set_names",
            target={"ref": "requester"},
            preferred_name="旧值覆盖",
            expected_revision=first["revision"] - 1,
        )["error"]
        == "revision_conflict"
    )
    assert (
        update(
            people,
            fresh,
            action="set_names",
            target={"ref": "requester"},
            preferred_name="阿雀",
            expected_revision=first["revision"],
        )["change_id"]
        == first["change_id"]
    )


def test_unbound_observation_autonomous_access_and_migration_safety(
    tmp_path: Path, people_factory
) -> None:
    people = people_factory()
    ref = people.record_evidence(
        group_id="g", user_id="guest", platform="qq", platform_uid="", message_id="m", content=""
    )
    access = PersonAccess(actor_id="", group_id="g", evidence_refs=(ref,))
    assert people.get_evidence(ref)["account_bound"] is False
    assert (
        update(
            people, access, action="set_names", target={"ref": "requester"}, preferred_name="冒充"
        )["success"]
        is False
    )
    ref2, alice = evidence(people, "g", "alice", "m2")
    autonomous = PersonAccess(
        actor_id=people.persona_id, group_id="g", evidence_refs=(ref2,), self_initiated=True
    )
    assert (
        update(
            people, autonomous, action="set_names", target={"ref": "requester"}, preferred_name="冒充"
        )["success"]
        is False
    )
    assert people.should_ask_name(alice.actor_id)
    people.mark_name_asked(alice.actor_id)
    assert not people.should_ask_name(alice.actor_id)
    people.conn.execute(
        "UPDATE person_ask_state SET last_asked=? WHERE actor_id=?",
        ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(), alice.actor_id),
    )
    assert people.should_ask_name(alice.actor_id)
    people.mark_name_asked(alice.actor_id)
    assert not people.should_ask_name(alice.actor_id, declined=True)
    people.conn.execute(
        "UPDATE person_ask_state SET last_asked=? WHERE actor_id=?",
        ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(), alice.actor_id),
    )
    assert not people.should_ask_name(alice.actor_id)
    manager = UnifiedUserManager(tmp_path, persona_name="月白", conn=people.conn)
    user = manager.get_or_create_user("legacy-only", "旧昵称")
    manager.save_user(user)
    preview = people.migration_dry_run()
    assert preview["users"] == 1 and preview["pending"] == 1
    applied = people.migration_apply()
    assert applied["success"] and Path(applied["backup_path"]).is_file()
    migrated = people.person_id_for_user("legacy-only")
    assert migrated and people.migration_apply()["status"] == "already_processed"
    ref_new, _ = evidence(people, "g", "alice", "m3")
    new_actor = people.get_evidence(ref_new)["actor_id"]
    assert people.person_id_for_user("legacy-only") == migrated
    assert people.migration_rollback(applied)["removed"] == 1
    assert people.person_id_for_user("legacy-only") is None
    assert manager.get_user("legacy-only") is not None
    # The old API remains callable, but is never a destructive bulk rollback.
    assert people.migration_rollback([])["success"] is True


@pytest.mark.asyncio
async def test_builtin_tools_keep_person_enums_and_array_item_contract(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.load_from_directory(tmp_path, auto_install_deps=False, include_builtin=True)
    update = registry.get("person_update")
    query = registry.get("person_query")
    assert update is not None and query is not None
    actions = update.to_tool_schema()["function"]["parameters"]["properties"]["action"]
    assert "correct_claim" in actions["enum"]
    sections = query.to_tool_schema()["function"]["parameters"]["properties"]["sections"]
    assert sections["type"] == "array"
    assert "visual" in sections["items"]["enum"]
    assert _coerce_type('{"ref":"requester"}', "object") == {"ref": "requester"}


@pytest.mark.asyncio
async def test_evidence_markup_is_escaped_and_work_mode_model_drain_has_plain_compatibility() -> None:
    tagged = PromptFactory.tag_message("hello", speaker="阿甲", user_id="100", evidence_ref='ev"bad')
    assert 'evidence_ref="ev&quot;bad"' in tagged
    from sirius_pulse.core.work_mode import WorkModeRun
    from sirius_pulse.models.models import Message

    run = WorkModeRun(group_id="g", goal="test")
    message = Message(
        role="user", content="原文", speaker="阿甲", channel_user_id="100", evidence_ref="ev_1"
    )
    run.stash_message("原文", mentions_persona=True, replay=(message, "u"))
    assert run.take_flushed() == ["原文"]
    assert run.take_flushed_for_model() == [
        tagged.replace("ev&quot;bad", "ev_1").replace("hello", "原文")
    ]
    assert run.flushed_evidence_refs == ["ev_1"]


@pytest.mark.asyncio
async def test_tool_execution_binds_exact_pending_author_without_admin_inheritance(
    tmp_path: Path, people_factory
) -> None:
    registry = ToolRegistry()
    registry.load_from_directory(tmp_path, auto_install_deps=False, include_builtin=True)
    tool = registry.get("person_update")
    assert tool is not None
    people = people_factory()
    refs = []
    actors = []
    for user_id, uid, admin in (("admin", "100", True), ("guest", "200", False)):
        people.conn.execute(
            "CREATE TABLE IF NOT EXISTS group_members(" "group_id TEXT,user_id TEXT,name TEXT)"
        )
        people.conn.execute(
            "INSERT INTO group_members(group_id,user_id,name) VALUES('g',?,?)",
            (user_id, user_id),
        )
        ref = people.record_evidence(
            group_id="g",
            user_id=user_id,
            platform="qq",
            platform_uid=uid,
            message_id=f"m-{user_id}",
            content="叫我自己的名字",
        )
        refs.append(ref)
        actors.append(people.get_evidence(ref)["actor_id"])
    context = ToolInvocationContext(
        caller=UnifiedUser(user_id="admin", name="admin", metadata={"is_developer": True}),
        group_id="g",
        evidence_refs=tuple(refs),
        evidence_callers=(
            (refs[0], UnifiedUser(user_id="admin", name="admin", metadata={"is_developer": True})),
            (refs[1], UnifiedUser(user_id="guest", name="guest")),
        ),
    )

    class Context:
        def get_person_service(self):
            return people

        def get_person_access(self, invocation):
            return PersonAccess(
                actor_id=people.person_id_for_user(invocation.caller_user_id) or "",
                group_id=invocation.group_id,
                evidence_refs=invocation.evidence_refs,
                is_admin=invocation.caller_is_developer,
            )

    executor = ToolExecutor(work_path=tmp_path)
    executor.set_engine_context(Context())
    result = await executor.execute_async(
        tool,
        {
            "action": "set_names",
            "target": {"ref": "requester"},
            "evidence_ref": refs[1],
            "preferred_name": "阿客",
        },
        invocation_context=context,
    )
    assert result.success, result.error
    assert (
        people.get_person(actors[1], PersonAccess(actor_id=actors[1], group_id="g"))["display_name"]
        == "阿客"
    )
    assert (
        people.get_person(actors[0], PersonAccess(actor_id=actors[0], group_id="g"))["display_name"]
        != "阿客"
    )

    forged = await executor.execute_async(
        tool,
        {
            "action": "set_names",
            "target": {"ref": "requester"},
            "evidence_ref": "ev_not_admitted",
            "preferred_name": "伪造",
        },
        invocation_context=context,
    )
    assert not forged.success
    assert "evidence_invalid" in forged.error
