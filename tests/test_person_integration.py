"""Independent SP-PRD-PERSON-001 acceptance through user messages and real tools.

Only the image backend and absent engagement profiles are substitutes. Identity,
pending-source binding, tool execution, SQLite, management handlers and queues are real.
No sockets, external model calls or paid generations are needed.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request
from PIL import Image

from sirius_pulse.core.bg_tasks_delayed import DelayedQueueTasks
from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.core.persona_db import PersonaDatabase
from sirius_pulse.core.prompt_factory import StyleAdapter
from sirius_pulse.core.tool_engine_context import ToolEngineContextImpl
from sirius_pulse.memory.user.unified_manager import UnifiedUserManager
from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision
from sirius_pulse.people import PersonAccess
from sirius_pulse.tools.builtin import image_generate
from sirius_pulse.tools.data_store import ToolDataStore
from sirius_pulse.tools.executor import ToolExecutor
from sirius_pulse.tools.models import ToolInvocationContext
from sirius_pulse.tools.pending_sources import pending_evidence_callers
from sirius_pulse.tools.registry import ToolRegistry
from sirius_pulse.visual import VisualService
from sirius_pulse.webui.server import WebUIServer


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (40, 100, 170)).save(buffer, format="PNG")
    return buffer.getvalue()


class Conversation:
    """Admit platform messages, then use the production source/auth/tool path."""

    def __init__(self, root: Path, database: PersonaDatabase) -> None:
        self.root = root
        self.users = UnifiedUserManager(root, persona_name="月白", conn=database.conn)
        self.people = self.users.people
        self.visual = VisualService(root, people=self.people, conn=database.conn)
        self.registry = ToolRegistry()
        self.registry.load_from_directory(
            root / "tools", auto_install_deps=False, include_builtin=True
        )
        self.executor = ToolExecutor(work_path=root)
        self.engine = SimpleNamespace(
            identity_resolver=None,
            user_manager=self.users,
            person_service=self.people,
            visual_service=self.visual,
            _tool_executor=self.executor,
            config={"pending_message_threshold": 0},
        )
        self.context = ToolEngineContextImpl(self.engine)
        self.executor.set_engine_context(self.context)
        self.sequence = 0

    def message(
        self,
        user: str,
        content: str,
        *,
        group: str = "g",
        admin: bool = False,
        attachments: tuple[Path, ...] = (),
        nickname: str = "",
    ) -> str:
        self.sequence += 1
        self.users.register_user(
            UnifiedUser(
                user_id=user,
                name=nickname or f"平台昵称-{user}",
                identities={"qq": f"qq-{user}"},
                metadata={"is_developer": admin},
            ),
            group,
        )
        return self.people.record_evidence(
            group_id=group,
            user_id=user,
            platform="qq",
            platform_uid=f"qq-{user}",
            adapter_route_id="qa-napcat-route",
            message_id=f"incoming-{self.sequence}",
            content=content,
            attachments=[str(path) for path in attachments],
        )

    def invocation(self, *refs: str, group: str = "g") -> ToolInvocationContext:
        authors = pending_evidence_callers(self.engine, group, list(refs))
        assert len(authors) == len(refs), "Every pending message needs its real account author"
        return ToolInvocationContext(
            caller=authors[0][1] if authors else None,
            developer_profiles=[user for _, user in authors if user.metadata.get("is_developer")],
            group_id=group,
            adapter_type="napcat",
            evidence_refs=tuple(refs),
            evidence_callers=authors,
        )

    async def call(self, tool_name: str, invocation: ToolInvocationContext, **params):
        tool = self.registry.get(tool_name)
        assert tool is not None, f"Required built-in {tool_name} must be registered"
        return await self.executor.execute_async(tool, params, invocation_context=invocation)

    async def update(self, ref: str, *, group: str = "g", **params):
        params.setdefault("target", {"ref": "requester"})
        return await self.call(
            "person_update", self.invocation(ref, group=group), evidence_ref=ref, **params
        )

    async def query(self, ref: str, target: dict, *, group: str = "g", sections=None):
        result = await self.call(
            "person_query",
            self.invocation(ref, group=group),
            target=target,
            sections=sections or ["names"],
        )
        assert result.success, result.error
        return result.data

    def actor(self, ref: str) -> str:
        return self.people.get_evidence(ref)["actor_id"]

    def photo(self, name: str) -> Path:
        path = self.root / f"{name}.png"
        path.write_bytes(_png())
        return path


@pytest.fixture
def conversation(tmp_path: Path):
    database = PersonaDatabase(tmp_path / "persona.db")
    try:
        yield Conversation(tmp_path, database)
    finally:
        # All services borrow this one connection; close even after a failed assertion.
        database.close()


@pytest.mark.asyncio
async def test_third_party_fact_becomes_active_only_after_new_owner_confirmation(conversation):
    chat = conversation
    owner_ref = chat.message("alice", "今天去公园了")
    proposal_ref = chat.message("bob", "甲喜欢喝乌龙茶，你可以问问她")
    owner_id = chat.actor(owner_ref)
    proposed = await chat.update(
        proposal_ref,
        action="propose_claim",
        target={"ref": "person_id", "value": owner_id},
        field="drink_preference",
        value="乌龙茶",
        source="third_party",
    )
    assert proposed.success, proposed.error
    assert proposed.data["status"] == "pending"
    before = await chat.query(owner_ref, {"ref": "requester"}, sections=["claims"])
    assert [
        (claim["value"], claim["source"], claim["status"]) for claim in before["sections"]["claims"]
    ] == [("乌龙茶", "third_party", "pending")]

    confirmation_ref = chat.message("alice", "是的，我喜欢乌龙茶，确认这条资料")
    confirmation = chat.invocation(confirmation_ref)
    confirmation_params = {
        "action": "confirm_change",
        "target": {"ref": "requester"},
        "evidence_ref": confirmation_ref,
        "change_id": proposed.data["change_id"],
        "expected_revision": before["revision"],
    }
    confirmed = await chat.call("person_update", confirmation, **confirmation_params)
    assert confirmed.success and confirmed.data["status"] == "applied", confirmed.data
    after = await chat.query(confirmation_ref, {"ref": "requester"}, sections=["claims"])
    repeated = await chat.call("person_update", confirmation, **confirmation_params)
    assert repeated.success and repeated.data["status"] == "already_processed", repeated.data
    assert repeated.data["change_id"] == confirmed.data["change_id"]
    final = await chat.query(confirmation_ref, {"ref": "requester"}, sections=["claims"])
    assert final["revision"] == after["revision"] == before["revision"] + 1
    assert final["sections"]["claims"] == after["sections"]["claims"]
    assert len(after["sections"]["claims"]) == 1
    assert after["sections"]["claims"][0]["status"] == "active", after
    assert after["sections"]["claims"][0]["value"] == "乌龙茶"
    observer = await chat.query(
        proposal_ref, {"ref": "person_id", "value": owner_id}, sections=["claims"]
    )
    assert [(claim["value"], claim["status"]) for claim in observer["sections"]["claims"]] == [
        ("乌龙茶", "active")
    ]


def _policy_request(body: dict, *, role: str = "admin"):
    payload = StreamReader(Mock(), limit=65536)
    payload.feed_data(json.dumps(body).encode())
    payload.feed_eof()
    request = make_mocked_request(
        "POST",
        "/api/persona/people/policy",
        headers={"Content-Type": "application/json"},
        payload=payload,
        match_info={"operation": "policy"},
    )
    request["auth_role"] = role
    request["auth_user"] = "qa-admin" if role == "admin" else "qa-viewer"
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stage", "activate"])
async def test_configured_uuid_controller_can_maintain_persona_but_other_members_cannot(
    conversation, operation
):
    chat = conversation
    (chat.root / "persona.json").write_text(json.dumps({"name": "月白"}), encoding="utf-8")
    controller_ref = chat.message("controller", "我来维护月白的设定图")
    controller_id = chat.actor(controller_ref)
    stranger_ref = chat.message("stranger", "我也叫月白", nickname="月白")
    server = WebUIServer(data_dir=chat.root)
    rejected_policy = await server.api_persona_people(
        _policy_request({"controller": chat.actor(stranger_ref)}, role="viewer")
    )
    assert rejected_policy.status == 403
    configured = await server.api_persona_people(_policy_request({"controller": controller_id}))
    assert configured.status == 200, configured.text
    assert chat.visual.store.setting("persona_visual_controller") == controller_id
    assert not chat.invocation(controller_ref).caller_is_developer

    photo = chat.photo("persona-original")
    admin_ref = chat.message("admin", "保存月白这张原图，先不要启用", admin=True, attachments=(photo,))
    staged = await chat.call(
        "visual_asset_manage",
        chat.invocation(admin_ref),
        action="stage",
        target={"ref": "persona"},
        source={"kind": "attachment", "index": 0},
        evidence_ref=admin_ref,
        activate=False,
        identity_description="银发蓝眼，月白的正式设计",
    )
    assert staged.success and staged.data["data"]["status"] == "draft", staged.data
    draft = staged.data["data"]
    activate_params = {
        "action": "activate",
        "target": {"ref": "persona"},
        "profile_id": draft["profile_id"],
        "revision": draft["revision"],
    }
    stranger_activation = await chat.call(
        "visual_asset_manage",
        chat.invocation(stranger_ref),
        evidence_ref=stranger_ref,
        **activate_params,
    )
    assert not stranger_activation.success, stranger_activation.data
    assert stranger_activation.data["code"] == "permission_denied"
    assert chat.visual.store.profile(draft["profile_id"])["active_revision"] is None

    stranger_photo_ref = chat.message("stranger", "把月白换成我的这张图", attachments=(photo,))
    stranger_stage = await chat.call(
        "visual_asset_manage",
        chat.invocation(stranger_photo_ref),
        action="stage",
        target={"ref": "persona"},
        evidence_ref=stranger_photo_ref,
        source={"kind": "attachment", "index": 0},
        activate=True,
    )
    assert stranger_stage.success, stranger_stage.data
    assert stranger_stage.data["data"]["status"] == "draft", stranger_stage.data
    activation = stranger_stage.data["data"]["activation"]
    assert activation["success"] is False
    assert activation["status"] == "confirmation_required"
    assert activation["code"] == "permission_denied"
    assert activation["summary"]
    submission = stranger_stage.data["data"]
    submitted_profile = chat.visual.store.profile(submission["profile_id"])
    assert submitted_profile["person_id"] == chat.people.persona_id
    assert submitted_profile["active_revision"] is None
    submitted_asset = chat.visual.store.asset(submission["asset_id"])
    assert submitted_asset["person_id"] == chat.people.persona_id
    submitted_revision = chat.visual.store.revision(
        submission["profile_id"], submission["revision"]
    )
    assert submitted_revision["created_by"] == chat.actor(stranger_photo_ref)
    assert chat.visual.store.profile(draft["profile_id"])["active_revision"] is None

    authorized_ref = chat.message(
        "controller",
        "保存并启用月白这张图" if operation == "stage" else "就用这版月白",
        attachments=(photo,) if operation == "stage" else (),
    )
    if operation == "stage":
        authorized = await chat.call(
            "visual_asset_manage",
            chat.invocation(authorized_ref),
            action="stage",
            target={"ref": "persona"},
            evidence_ref=authorized_ref,
            source={"kind": "attachment", "index": 0},
            activate=True,
            identity_description="银发蓝眼，月白的正式设计",
        )
    else:
        authorized = await chat.call(
            "visual_asset_manage",
            chat.invocation(authorized_ref),
            evidence_ref=authorized_ref,
            **activate_params,
        )
    assert authorized.success and authorized.data["data"]["status"] == "active", authorized.data
    profile_id = authorized.data["data"]["profile_id"]
    profile = chat.visual.store.profile(profile_id)
    assert profile["person_id"] == chat.people.persona_id
    assert profile["active_revision"] == authorized.data["data"]["revision"]


@pytest.mark.asyncio
async def test_private_and_retired_names_do_not_follow_accounts_into_another_group(conversation):
    chat = conversation
    secret_ref = chat.message("alice", "这里叫我私密旧称，暗号是松果", group="private")
    assert (
        await chat.update(
            secret_ref,
            group="private",
            action="set_names",
            preferred_name="私密旧称",
            add_aliases=["只有私聊知道的阿甲"],
        )
    ).success
    assert (
        await chat.update(
            secret_ref,
            group="private",
            action="propose_claim",
            field="private_note",
            value="松果",
        )
    ).success
    renamed_ref = chat.message("alice", "别叫我私密旧称了，改叫私密新称", group="private")
    assert (
        await chat.update(
            renamed_ref,
            group="private",
            action="set_names",
            preferred_name="私密新称",
            retire_names=["私密旧称"],
        )
    ).success
    public_ref = chat.message("alice", "群里请叫我阿甲", group="public")
    assert chat.actor(public_ref) == chat.actor(secret_ref)
    assert (
        await chat.update(
            public_ref,
            group="public",
            action="set_names",
            preferred_name="阿甲",
        )
    ).success
    second_ref = chat.message("bob", "叫我私密旧称", group="public", nickname="私密旧称")
    assert chat.actor(second_ref) != chat.actor(secret_ref)
    assert (
        await chat.update(
            second_ref,
            group="public",
            action="set_names",
            preferred_name="私密旧称",
        )
    ).success
    for secret in ("私密新称", "只有私聊知道的阿甲"):
        result = await chat.query(second_ref, {"ref": "name", "value": secret}, group="public")
        assert result["status"] == "not_found_or_not_visible", result
    public_view = await chat.query(
        second_ref,
        {"ref": "person_id", "value": chat.actor(secret_ref)},
        group="public",
        sections=["names", "claims"],
    )
    assert public_view["display_name"] == "阿甲"
    assert public_view["sections"]["claims"] == []
    assert all(
        secret not in json.dumps(public_view, ensure_ascii=False)
        for secret in (
            "私密旧称",
            "私密新称",
            "只有私聊知道的阿甲",
            "松果",
        )
    )
    old_name = await chat.query(renamed_ref, {"ref": "name", "value": "私密旧称"}, group="private")
    assert old_name["status"] == "not_found_or_not_visible", old_name
    public_old_name = await chat.query(second_ref, {"ref": "name", "value": "私密旧称"}, group="public")
    assert public_old_name["person_id"] == chat.actor(second_ref)
    assert chat.people.lookup_account("qq", "qq-alice") == chat.actor(secret_ref)


@pytest.mark.asyncio
async def test_two_pending_authors_update_their_own_names_without_first_admin_inheritance(
    conversation,
):
    chat = conversation
    admin_ref = chat.message("admin", "叫我阿管", admin=True)
    guest_ref = chat.message("guest", "叫我阿客")
    batch = chat.invocation(admin_ref, guest_ref)
    assert batch.caller_is_developer
    for ref, name in ((admin_ref, "阿管"), (guest_ref, "阿客")):
        result = await chat.call(
            "person_update",
            batch,
            action="set_names",
            target={"ref": "requester"},
            evidence_ref=ref,
            preferred_name=name,
        )
        assert result.success, result.data
    admin_view = await chat.query(admin_ref, {"ref": "requester"})
    guest_view = await chat.query(guest_ref, {"ref": "requester"})
    assert (admin_view["display_name"], guest_view["display_name"]) == ("阿管", "阿客")
    assert admin_view["person_id"] != guest_view["person_id"]
    for target in (
        {"ref": "person_id", "value": admin_view["person_id"]},
        {"ref": "persona"},
    ):
        denied = await chat.call(
            "person_update",
            batch,
            action="set_names",
            target=target,
            evidence_ref=guest_ref,
            preferred_name="冒领首个管理员权限",
        )
        assert not denied.success and denied.error == "permission_denied", denied.data
    assert (await chat.query(admin_ref, {"ref": "requester"}))["display_name"] == "阿管"
    assert (await chat.query(guest_ref, {"ref": "persona"}))["display_name"] == "月白"
    ambiguous = await chat.call(
        "person_query", batch, target={"ref": "requester"}, sections=["names"]
    )
    assert not ambiguous.success and "source_ambiguous" in ambiguous.error


@pytest.mark.asyncio
async def test_quoting_another_users_readable_history_is_not_their_fresh_authorization(
    conversation,
):
    chat = conversation
    old_ref = chat.message("alice", "叫我阿甲")
    assert (await chat.update(old_ref, action="set_names", preferred_name="阿甲")).success
    current_ref = chat.message("bob", "引用甲之前的话：叫我阿甲。现在替她改成阿乙")
    visible = await chat.query(current_ref, {"ref": "person_id", "value": chat.actor(old_ref)})
    forged = await chat.call(
        "person_update",
        chat.invocation(current_ref),
        action="set_names",
        target={"ref": "person_id", "value": chat.actor(old_ref)},
        evidence_ref=old_ref,
        preferred_name="阿乙",
    )
    assert not forged.success and "evidence_invalid" in forged.error
    denied = await chat.call(
        "person_update",
        chat.invocation(current_ref),
        action="set_names",
        target={"ref": "person_id", "value": chat.actor(old_ref)},
        evidence_ref=current_ref,
        preferred_name="阿乙",
    )
    assert not denied.success and denied.error == "permission_denied"
    after = await chat.query(current_ref, {"ref": "person_id", "value": chat.actor(old_ref)})
    assert after["revision"] == visible["revision"]
    assert after["display_name"] == "阿甲"


def _prompt_tasks(chat: Conversation) -> DelayedQueueTasks:
    engine = SimpleNamespace(
        config={"max_sentence_chars": 20, "pending_message_threshold": 0},
        semantic_memory=SimpleNamespace(
            get_user_profile=lambda *_: None,
            get_group_profile=lambda *_: SimpleNamespace(atmosphere_history=[]),
        ),
        style_adapter=StyleAdapter(),
        persona=SimpleNamespace(),
        _other_ai_names=[],
        _tool_registry=chat.registry,
        _plugin_registry=None,
        person_service=chat.people,
    )
    return DelayedQueueTasks(engine)


def _delayed_message(chat: Conversation, user: str, content: str, *, group: str = "g"):
    ref = chat.message(user, content, group=group)
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        group_id=group,
        user_id=user,
        channel_user_id=f"qq-{user}",
        message_content=content,
        speaker_name=f"平台昵称-{user}",
        evidence_ref=ref,
        strategy_decision=StrategyDecision(strategy=ResponseStrategy.DELAYED),
    )
    assert item.evidence_refs == [ref]
    return item, ref


def test_first_inbound_without_engagement_gets_one_optional_name_hint_not_repeated(conversation):
    chat = conversation
    tasks = _prompt_tasks(chat)
    item, ref = _delayed_message(chat, "newcomer", "你好，周末有什么推荐？")
    assert tasks._engine.semantic_memory.get_user_profile("g", "newcomer") is None
    first = tasks._build_delayed_prompt(item, "g")
    assert "周末有什么推荐" in first.user_content
    assert ref in first.user_content and ref in first.dynamic_context
    assert "首选称呼未确认" in first.dynamic_context
    assert "不阻塞回答" in first.dynamic_context
    followup, _ = _delayed_message(chat, "newcomer", "先推荐几个公园吧")
    second = tasks._build_delayed_prompt(followup, "g")
    assert "推荐几个公园" in second.user_content
    assert "首选称呼未确认" not in second.dynamic_context
    assert not chat.people.should_ask_name(chat.actor(ref))


def test_refusing_to_give_name_preserves_chat_and_suppresses_followup_prompt(conversation):
    chat = conversation
    tasks = _prompt_tasks(chat)
    item, ref = _delayed_message(chat, "private-user", "别再问我名字，聊风景就好")
    refusal = tasks._build_delayed_prompt(item, "g")
    assert "聊风景就好" in refusal.user_content
    assert "首选称呼未确认" not in refusal.dynamic_context
    followup, _ = _delayed_message(chat, "private-user", "山里的枫叶现在红了吗？")
    continued = tasks._build_delayed_prompt(followup, "g")
    assert "枫叶" in continued.user_content
    assert "首选称呼未确认" not in continued.dynamic_context
    assert not chat.people.should_ask_name(chat.actor(ref))


@pytest.mark.asyncio
@pytest.mark.parametrize("public_name", ["", "群内阿甲"])
async def test_delayed_prompt_uses_current_group_name_never_private_chat_name(
    conversation, public_name
):
    chat = conversation
    private_ref = chat.message("alice", "私聊叫我保密称呼", group="private")
    saved = await chat.update(
        private_ref, group="private", action="set_names", preferred_name="保密称呼"
    )
    assert saved.success, saved.data
    item, public_ref = _delayed_message(chat, "alice", "大家好，聊聊周末吧")
    if public_name:
        saved_public = await chat.update(public_ref, action="set_names", preferred_name=public_name)
        assert saved_public.success, saved_public.data
    bundle = _prompt_tasks(chat)._build_delayed_prompt(item, "g")
    assert "大家好，聊聊周末吧" in bundle.user_content
    assert "保密称呼" not in bundle.dynamic_context, bundle.dynamic_context
    if public_name:
        assert public_name in bundle.dynamic_context
        assert "首选称呼未确认" not in bundle.dynamic_context
    else:
        assert "preferred_name" not in bundle.dynamic_context


class LocalImageBackend:
    """Exercise the existing queue with generated bytes, never a network provider."""

    def __init__(self, visual: VisualService) -> None:
        self.visual = visual
        self.calls: list[dict] = []
        self.callbacks: list[dict] = []

    def get_visual_service(self):
        return self.visual

    async def generate_image(self, **request):
        self.calls.append(request)
        return {"image_bytes": _png(), "mime_type": "image/png"}

    async def handle_background_tool_result(self, **result):
        self.callbacks.append(result)
        return {"status": "completed", "text": "已生成，尚未外发"}


@pytest.mark.asyncio
async def test_explicit_empty_subjects_scenery_needs_no_person_evidence_and_uses_existing_queue(
    conversation,
):
    chat = conversation
    invocation = ToolInvocationContext(
        caller=UnifiedUser(user_id="scenery-guest", name="游客"),
        group_id="g",
        adapter_type="napcat",
        evidence_refs=(),
        evidence_callers=(),
    )
    initial_people = chat.people.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    submitted = await chat.call(
        "image_generate",
        invocation,
        prompt="画一片没有人的湖泊与远山",
        subjects=[],
    )
    assert submitted.success, submitted.data
    assert submitted.data["data"]["status"] == "pending"
    store = chat.executor.get_data_store("image_generate")
    reopened = ToolDataStore(store.store_path)
    jobs = reopened.get("jobs", [])
    assert len(jobs) == 1
    assert jobs[0]["id"] == submitted.data["data"]["job_id"]
    assert jobs[0]["references"] == [] and "visual" not in jobs[0]
    assert jobs[0]["user_id"] == "scenery-guest"
    backend = LocalImageBackend(chat.visual)
    await image_generate._process_once(backend, store, recover_interrupted=False)
    assert len(backend.calls) == 1
    assert backend.calls[0]["prompt"] == "画一片没有人的湖泊与远山"
    assert backend.calls[0]["reference_images"] == []
    assert len(backend.callbacks) == 1 and backend.callbacks[0]["outcome"] == "success"
    reopened.reload()
    completed = reopened.get("jobs", [])[0]
    assert completed["status"] == "done"
    assert Path(completed["image_path"]).is_file()
    assert completed["callback_state"] == "completed"
    assert chat.people.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == initial_people
    assert not chat.visual.store.profiles_for(chat.people.persona_id)


@pytest.fixture
def memory_conversation(conversation):
    from sirius_pulse.memory.units import MemoryUnitFileStore, MemoryUnitManager

    chat = conversation
    manager = MemoryUnitManager(chat.root, embedding_client=None)
    chat.engine.memory_unit_manager = manager
    chat.people.memory_units = manager
    return chat, MemoryUnitFileStore(chat.root)


def _person_memory(unit_id: str, identity: str, summary: str, *, group: str = "g", **fields):
    from datetime import datetime, timezone

    from sirius_pulse.memory.units import MemoryUnit

    values = {
        "unit_id": unit_id,
        "group_id": group,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "scope": "group",
        "scope_id": group,
        "participants": [identity],
        "identity_aliases": [identity],
        "salience": 0.9,
        "confidence": 0.9,
    }
    values.update(fields)
    return MemoryUnit(**values)


async def _query_memories(chat: Conversation, ref: str, person_id: str, query: str):
    result = await chat.call(
        "person_query",
        chat.invocation(ref),
        target={"ref": "person_id", "value": person_id},
        sections=["memories"],
        query=query,
    )
    assert result.success, result.error
    assert result.data["status"] == "found", result.data
    assert result.data["person_id"] == person_id
    return result.data["sections"]["memories"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("天文观星", ["alice-observatory"]),
        ("酸面包烘焙", ["alice-bread"]),
        ("深潜训练", []),
    ],
)
async def test_person_memory_query_selects_the_requested_topic_after_cold_load(
    memory_conversation, query, expected
):
    chat, store = memory_conversation
    ref = chat.message("alice", f"你还记得我有关{query}的经历吗？")
    person_id = chat.actor(ref)
    units = [
        _person_memory("alice-observatory", "alice", "甲去北山天文观星，使用牛顿反射望远镜。"),
        _person_memory("alice-bread", "alice", "甲学习酸面包烘焙，练习天然酵种发酵。"),
    ]
    store.save("g", units)
    before = {path: path.read_bytes() for path in store.base_dir.rglob("*") if path.is_file()}
    assert len(store.load("g", with_embeddings=False)) == 2
    memories = await _query_memories(chat, ref, person_id, query)
    assert memories["status"] == ("available" if expected else "empty"), memories
    assert [item["unit_id"] for item in memories["items"]] == expected, memories
    expected_summaries = [unit.summary for unit in units if unit.unit_id in expected]
    assert [item["summary"] for item in memories["items"]] == expected_summaries
    assert all(item["scope"] == "group" for item in memories["items"])
    assert before == {
        path: path.read_bytes() for path in store.base_dir.rglob("*") if path.is_file()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("private_scope", ["group", "user", "persona"])
async def test_person_memory_query_never_brings_another_groups_same_account_history(
    memory_conversation, private_scope
):
    chat, store = memory_conversation
    private_ref = chat.message("alice", "私聊记录露营路线保密坐标", group="private")
    public_ref = chat.message("alice", "本群记录露营路线集合地点")
    observer_ref = chat.message("bob", "看看甲在本群讨论的露营路线")
    assert chat.actor(private_ref) == chat.actor(public_ref)
    store.save(
        "private",
        [
            _person_memory(
                "private-camp",
                "alice",
                "甲的露营路线保密坐标：HIDDEN_PRIVATE_CAMP。",
                group="private",
                scope=private_scope,
                scope_id="alice" if private_scope == "user" else "private",
            )
        ],
    )
    store.save("g", [_person_memory("public-camp", "alice", "甲在本群约定露营路线集合地点为东门。")])
    memories = await _query_memories(chat, observer_ref, chat.actor(public_ref), "露营路线")
    assert memories["status"] == "available", memories
    assert [item["unit_id"] for item in memories["items"]] == ["public-camp"], memories
    assert "HIDDEN_PRIVATE_CAMP" not in json.dumps(memories, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_form", ["uuid", "legacy", "platform"])
@pytest.mark.parametrize("query", ["陶艺", ""], ids=["matching-query", "no-query-summary"])
async def test_same_named_accounts_memory_query_uses_exact_identity_not_shared_nickname(
    memory_conversation, identity_form, query
):
    chat, store = memory_conversation
    alice_ref = chat.message("alice", "叫我小白，我在做陶艺", nickname="小白")
    bob_ref = chat.message("bob", "我也叫小白，我也在做陶艺", nickname="小白")
    for ref in (alice_ref, bob_ref):
        assert (await chat.update(ref, action="set_names", preferred_name="小白")).success
    alice_id, bob_id = chat.actor(alice_ref), chat.actor(bob_ref)
    assert alice_id != bob_id
    ambiguous = await chat.query(bob_ref, {"ref": "name", "value": "小白"})
    assert ambiguous["status"] == "ambiguous"
    assert {item["person_id"] for item in ambiguous["candidates"]} == {alice_id, bob_id}
    identities = {
        "uuid": (alice_id, bob_id),
        "legacy": ("alice", "bob"),
        "platform": ("qq-alice", "qq-bob"),
    }
    alice_key, bob_key = identities[identity_form]
    store.save(
        "g",
        [
            _person_memory(
                "alice-pottery",
                alice_key,
                "小白完成陶艺青瓷杯：ALICE_ONLY_CUP。",
                identity_aliases=[alice_key, "小白"],
            ),
            _person_memory(
                "bob-pottery",
                bob_key,
                "小白完成陶艺红泥壶：BOB_ONLY_POT。",
                identity_aliases=[bob_key, "小白"],
            ),
        ],
    )
    memories = await _query_memories(chat, bob_ref, alice_id, query)
    assert memories["status"] == "available", memories
    assert [item["unit_id"] for item in memories["items"]] == ["alice-pottery"], memories
    assert memories["items"][0]["summary"] == "小白完成陶艺青瓷杯：ALICE_ONLY_CUP。"
    assert "BOB_ONLY_POT" not in json.dumps(memories, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inactive_fields",
    [
        pytest.param({"should_prompt": False}, id="retired"),
        pytest.param({"valid_until": "2000-01-01T00:00:00+00:00"}, id="expired"),
    ],
)
async def test_person_memory_query_excludes_retired_and_expired_facts(
    memory_conversation, inactive_fields
):
    chat, store = memory_conversation
    ref = chat.message("alice", "告诉我当前报名的登山计划")
    store.save(
        "g",
        [
            _person_memory("current-hike", "alice", "甲的登山计划是本周前往北峰。"),
            _person_memory("inactive-hike", "alice", "甲的登山计划旧口令为INACTIVE_HIKE。", **inactive_fields),
        ],
    )
    memories = await _query_memories(chat, ref, chat.actor(ref), "登山计划")
    assert memories["status"] == "available", memories
    assert [item["unit_id"] for item in memories["items"]] == ["current-hike"], memories
    assert "INACTIVE_HIKE" not in json.dumps(memories, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller", "target", "allowed"),
    [
        pytest.param("alice", "alice", True, id="owner-can-query-own-user-scope"),
        pytest.param("bob", "alice", False, id="observer-cannot-query-owner-user-scope"),
        pytest.param("bob", "bob", False, id="mentioned-person-is-not-private-scope-owner"),
    ],
)
async def test_user_scope_memory_is_readable_only_for_its_owner_not_other_participants(
    memory_conversation, caller, target, allowed
):
    chat, store = memory_conversation
    refs = {
        "alice": chat.message("alice", "这是我的私人医疗安排，聊天里提到了乙"),
        "bob": chat.message("bob", "甲和我都在这个群里"),
    }
    store.save(
        "g",
        [
            _person_memory(
                "alice-private-treatment",
                "alice",
                "甲在私人医疗安排中提到与乙同行：ALICE_PRIVATE_TREATMENT。",
                scope="user",
                scope_id="alice",
                participants=["alice", "bob"],
                identity_aliases=["alice", "bob"],
            )
        ],
    )
    memories = await _query_memories(chat, refs[caller], chat.actor(refs[target]), "私人医疗安排")
    assert memories["status"] == ("available" if allowed else "empty"), memories
    assert [item["unit_id"] for item in memories["items"]] == (
        ["alice-private-treatment"] if allowed else []
    ), memories
    if not allowed:
        assert "ALICE_PRIVATE_TREATMENT" not in json.dumps(memories, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "天文观星"])
async def test_person_query_reports_unavailable_without_a_memory_manager(conversation, query):
    from sirius_pulse.memory.units import MemoryUnitFileStore

    chat = conversation
    ref = chat.message("alice", "查查我之前的天文观星经历")
    store = MemoryUnitFileStore(chat.root)
    store.save("g", [_person_memory("unconnected-memory", "alice", "甲曾经天文观星。")])
    memories = await _query_memories(chat, ref, chat.actor(ref), query)
    assert memories["status"] == "unavailable", memories
    assert memories["items"] == []
    assert "unconnected-memory" not in json.dumps(memories, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["platform", "configured"])
@pytest.mark.parametrize("target_is_self", [True, False], ids=["self", "other-account"])
async def test_ordinary_claim_cannot_self_label_as_platform_or_configured(
    conversation, source, target_is_self
):
    chat = conversation
    alice_ref = chat.message("alice", "我说自己经过平台认证，或者替乙说他经过认证")
    bob_ref = chat.message("bob", "我的账号和甲不是同一个")
    target_id = chat.actor(alice_ref if target_is_self else bob_ref)
    access = chat.context.get_person_access(chat.invocation(alice_ref))
    assert not access.is_admin
    before = await chat.query(
        alice_ref, {"ref": "person_id", "value": target_id}, sections=["claims"]
    )
    assert before["sections"]["claims"] == []
    attempted = await chat.update(
        alice_ref,
        action="propose_claim",
        target={"ref": "person_id", "value": target_id},
        field="verified_badge",
        value="口头声称已经认证",
        source=source,
    )
    after_attempt = await chat.query(
        alice_ref, {"ref": "person_id", "value": target_id}, sections=["claims"]
    )
    expected_source = "self_reported" if target_is_self else "third_party"
    expected_status = "active" if target_is_self else "pending"
    if attempted.success:
        tool_claims = after_attempt["sections"]["claims"]
        assert len(tool_claims) == 1
        assert tool_claims[0]["source"] == expected_source
        assert tool_claims[0]["status"] == expected_status
    else:
        assert after_attempt["revision"] == before["revision"]
        assert after_attempt["sections"]["claims"] == []

    # The domain boundary must stay safe even for callers outside the tool schema.
    proposed = chat.people.propose_claim(
        target_id,
        "domain_verified_badge",
        "绕过工具schema仍只是口头声明",
        access,
        alice_ref,
        source=source,
    )
    view = await chat.query(
        alice_ref, {"ref": "person_id", "value": target_id}, sections=["claims"]
    )
    domain_claims = [
        claim for claim in view["sections"]["claims"] if claim["field"] == "domain_verified_badge"
    ]
    assert len(domain_claims) == 1
    assert domain_claims[0]["claim_id"] == proposed["claim_id"]
    assert domain_claims[0]["source"] == expected_source
    assert domain_claims[0]["status"] == expected_status
    assert not any(
        claim["source"] in {"platform", "configured"} for claim in view["sections"]["claims"]
    )
    assert not chat.invocation(alice_ref).caller_is_developer


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["inferred", "third_party"])
async def test_unconfirmed_claim_source_stays_pending_even_when_owner_is_the_actor(
    conversation, source
):
    chat = conversation
    owner_ref = chat.message("alice", "这条爱好只是别人的说法或猜测，我还没有确认")
    person_id = chat.actor(owner_ref)
    proposed = await chat.update(
        owner_ref,
        action="propose_claim",
        field="suggested_hobby",
        value="可能喜欢极地摄影",
        source=source,
    )
    assert proposed.success, proposed.data
    assert proposed.data["status"] == "pending", proposed.data
    access = chat.context.get_person_access(chat.invocation(owner_ref))
    domain_proposal = chat.people.propose_claim(
        person_id,
        "domain_suggested_hobby",
        "领域入口收到的同一未确认说法",
        access,
        owner_ref,
        source=source,
    )
    assert domain_proposal["status"] == "pending", domain_proposal
    owner_view = await chat.query(owner_ref, {"ref": "requester"}, sections=["claims"])
    assert {claim["claim_id"] for claim in owner_view["sections"]["claims"]} == {
        proposed.data["claim_id"],
        domain_proposal["claim_id"],
    }
    assert all(
        claim["source"] == source and claim["status"] == "pending"
        for claim in owner_view["sections"]["claims"]
    ), owner_view
    observer_ref = chat.message("bob", "甲有什么已确认的爱好？")
    observer_view = await chat.query(
        observer_ref, {"ref": "person_id", "value": person_id}, sections=["claims"]
    )
    assert observer_view["sections"]["claims"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("first_is_admin", [False, True], ids=["owner", "owner-admin"])
@pytest.mark.parametrize("second_author", ["bob", "alice"], ids=["mixed-authors", "same-author"])
async def test_read_image_visible_material_is_shared_without_inheriting_first_authors_identity(
    conversation, first_is_admin, second_author
):
    chat = conversation
    photo = chat.photo("alice-private-image")
    owner_ref = chat.message("alice", "把这张图保存为我的本群形象", admin=first_is_admin, attachments=(photo,))
    staged = await chat.call(
        "visual_asset_manage",
        chat.invocation(owner_ref),
        action="stage",
        target={"ref": "requester"},
        source={"kind": "attachment", "index": 0},
        evidence_ref=owner_ref,
        activate=True,
    )
    assert staged.success and staged.data["data"]["status"] == "active", staged.data
    asset_id = staged.data["data"]["asset_id"]
    next_ref = chat.message(
        second_author,
        "看一下甲刚保存的图",
        admin=first_is_admin and second_author == "alice",
    )
    batch = chat.invocation(owner_ref, next_ref)
    access = chat.context.get_person_access(batch)
    assert access.actor_id == (chat.actor(owner_ref) if second_author == "alice" else "")
    assert access.is_admin is (first_is_admin and second_author == "alice")
    viewed = await chat.call("read_image", batch, assets=[asset_id], focus="确认围巾颜色")
    assert viewed.success, viewed.data
    assert len(viewed.multimodal_blocks) == 1
    block = viewed.multimodal_blocks[0]
    assert block.type == "image"
    assert Path(block.value).read_bytes() == photo.read_bytes()


@pytest.mark.asyncio
async def test_private_profile_and_visual_values_persist_but_not_in_general_logs_or_telemetry(
    conversation, caplog
):
    import logging
    import uuid

    chat = conversation
    secret = f"PRIVATE_PERSON_DATA_{uuid.uuid4().hex}"
    photo = chat.photo("private-telemetry-image")
    with caplog.at_level(logging.INFO):
        ref = chat.message("alice", f"我的私有称呼和偏好都是{secret}，也将它作为这张图的说明", attachments=(photo,))
        named = await chat.update(ref, action="set_names", preferred_name=secret)
        assert named.success and named.data["display_name"] == secret, named.data
        claimed = await chat.update(
            ref, action="propose_claim", field="private_preference", value=secret
        )
        assert claimed.success, claimed.data
        queried = await chat.query(
            ref, {"ref": "name", "value": secret}, sections=["names", "claims"]
        )
        assert queried["display_name"] == secret
        assert queried["sections"]["claims"][0]["value"] == secret
        staged = await chat.call(
            "visual_asset_manage",
            chat.invocation(ref),
            action="stage",
            target={"ref": "requester"},
            source={"kind": "attachment", "index": 0},
            evidence_ref=ref,
            identity_description=secret,
            activate=False,
        )
        assert staged.success and staged.data["data"]["status"] == "draft", staged.data

    person_id = chat.actor(ref)
    names = chat.people.conn.execute(
        "SELECT name FROM person_names WHERE person_id=? AND name_type='preferred' AND active=1",
        (person_id,),
    ).fetchall()
    assert [row["name"] for row in names] == [secret]
    claim = chat.people.conn.execute(
        "SELECT value FROM person_claims WHERE claim_id=?", (claimed.data["claim_id"],)
    ).fetchone()
    assert claim["value"] == secret
    draft = staged.data["data"]
    revision = chat.visual.store.revision(draft["profile_id"], draft["revision"])
    assert revision["identity_description"] == secret

    tool_names = {"person_query", "person_update", "visual_asset_manage"}
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in logs
    assert "<redacted>" in logs
    assert all(f"execute done: {name}" in logs for name in tool_names)
    telemetry_path = chat.root / "tool_data" / ".telemetry.jsonl"
    telemetry_text = telemetry_path.read_text(encoding="utf-8")
    assert secret not in telemetry_text
    records = [json.loads(line) for line in telemetry_text.splitlines() if line.strip()]
    assert len(records) == 4
    assert {record["tool_name"] for record in records} == tool_names
    for record in records:
        assert record["success"] is True
        assert record["params"] and set(record["params"].values()) == {"<redacted>"}
        assert record["result_summary"] in {"", "<redacted>"}


def test_real_memory_initialization_injects_persisted_units_into_person_service(tmp_path):
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase
    from sirius_pulse.memory.units import MemoryUnitFileStore
    from sirius_pulse.models.persona import PersonaProfile

    database = PersonaDatabase(tmp_path / "persona.db")
    try:
        engine = SimpleNamespace(
            work_path=tmp_path,
            _persona_db_conn=database.conn,
            _embedding_client=None,
            persona=PersonaProfile(name="月白"),
        )
        _EmotionalGroupChatEngineBase._init_memory_system(engine)
        engine.user_manager.register_user(
            UnifiedUser(user_id="alice", name="甲", identities={"qq": "qq-alice"}), "g"
        )
        engine.person_service.record_evidence(
            group_id="g",
            user_id="alice",
            platform="qq",
            platform_uid="qq-alice",
            message_id="startup-user-message",
            content="请回忆我之前说过的露营路线",
        )
        person_id = engine.person_service.person_id_for_user("alice")
        assert person_id
        MemoryUnitFileStore(tmp_path).save(
            "g", [_person_memory("startup-camping", "alice", "甲在本群介绍了露营路线。")]
        )
        memories = engine.person_service.memory_summary(
            person_id, PersonAccess(actor_id=person_id, group_id="g"), "露营路线"
        )
        assert memories["status"] == "available", memories
        assert [item["unit_id"] for item in memories["items"]] == ["startup-camping"]
        assert engine.person_service.memory_units is engine.memory_unit_manager
        assert engine.person_service.conn is engine.visual_service.store.conn is database.conn
    finally:
        database.close()


@pytest.mark.parametrize("declined", [True, False], ids=["refusal-persists", "ordinary-cooldown"])
def test_optional_name_hint_after_eight_days_respects_prior_refusal(conversation, declined):
    from datetime import datetime, timedelta, timezone

    chat = conversation
    tasks = _prompt_tasks(chat)
    content = "别再问我名字，先介绍公园" if declined else "你好，先介绍公园"
    first_item, ref = _delayed_message(chat, "name-private-user", content)
    first = tasks._build_delayed_prompt(first_item, "g")
    assert "介绍公园" in first.user_content
    assert ("首选称呼未确认" in first.dynamic_context) is (not declined)
    actor_id = chat.actor(ref)
    row = chat.people.conn.execute(
        "SELECT declined FROM person_ask_state WHERE actor_id=?", (actor_id,)
    ).fetchone()
    assert row is not None and bool(row["declined"]) is declined
    old_time = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    chat.people.conn.execute(
        "UPDATE person_ask_state SET last_asked=? WHERE actor_id=?", (old_time, actor_id)
    )
    chat.people.conn.commit()

    later_item, later_ref = _delayed_message(chat, "name-private-user", "八天后了，公园怎么去？")
    later = tasks._build_delayed_prompt(later_item, "g")
    assert "公园怎么去" in later.user_content
    assert later_ref in later.user_content
    assert ("首选称呼未确认" in later.dynamic_context) is (not declined)
    final_row = chat.people.conn.execute(
        "SELECT declined,last_asked FROM person_ask_state WHERE actor_id=?", (actor_id,)
    ).fetchone()
    assert bool(final_row["declined"]) is declined
    if declined:
        assert final_row["last_asked"] == old_time
    else:
        assert datetime.fromisoformat(final_row["last_asked"]) > datetime.fromisoformat(old_time)


def _people_request(
    method: str,
    operation: str,
    *,
    body: dict | None = None,
    role: str | None = "admin",
    key: str = "operation",
) -> object:
    payload = StreamReader(Mock(), limit=65536)
    payload.feed_data(json.dumps(body or {}).encode())
    payload.feed_eof()
    request = make_mocked_request(
        method,
        f"/api/persona/people/{operation}",
        headers={"Content-Type": "application/json"},
        payload=payload,
        match_info={key: operation},
    )
    if role is not None:
        request["auth_role"] = role
        request["auth_user"] = role
    return request


def _people_server(tmp_path: Path) -> WebUIServer:
    (tmp_path / "persona.json").write_text(json.dumps({"name": "月白"}), encoding="utf-8")
    return WebUIServer(data_dir=tmp_path)


@pytest.mark.asyncio
async def test_sensitive_person_get_requires_admin_without_opening_database(
    tmp_path: Path,
) -> None:
    server = _people_server(tmp_path)
    for role in (None, "viewer"):
        for target in ("migration", "state", "some-person"):
            response = await server.api_persona_people(
                _people_request("GET", target, role=role, key="person_id")
            )
            assert response.status == 403
            assert json.loads(response.text)["code"] == "permission_denied"
    assert not (tmp_path / "persona.db").exists()

    response = await server.api_persona_people(
        _people_request("POST", "policy", body={"total_quota": 1}, role="viewer")
    )
    assert response.status == 403


@pytest.mark.asyncio
async def test_admin_migration_has_backup_conflict_report_and_safe_rollback(
    tmp_path: Path,
) -> None:
    server = _people_server(tmp_path)
    with PersonaDatabase(tmp_path / "persona.db") as database:
        users = UnifiedUserManager(tmp_path, persona_name="月白", conn=database.conn)
        users.register_user(UnifiedUser(user_id="old", name="旧用户"), "g")
        users.register_user(
            UnifiedUser(user_id="bound", name="已绑定", identities={"qq": "shared"}), "g"
        )
        ref = users.people.record_evidence(
            group_id="g",
            user_id="bound",
            platform="qq",
            platform_uid="shared",
            message_id="m1",
            content="真实账号已绑定",
        )
        bound_id = users.people.get_evidence(ref)["actor_id"]
        users.register_user(
            UnifiedUser(user_id="conflict", name="同名账号", identities={"qq": "shared"}), "g"
        )
        database.conn.commit()

    preview = await server.api_persona_people(_people_request("GET", "migration", key="person_id"))
    assert preview.status == 200
    assert json.loads(preview.text)["pending"] == 2

    applied_response = await server.api_persona_people(_people_request("POST", "migration-apply"))
    applied = json.loads(applied_response.text)
    assert applied_response.status == 200, applied
    assert applied["status"] == "applied"
    assert Path(applied["backup_path"]).is_file()
    assert applied["created"] == 2
    assert any(row["user_id"] == "conflict" for row in applied["conflicts"])

    with PersonaDatabase(tmp_path / "persona.db") as database:
        people = UnifiedUserManager(tmp_path, conn=database.conn).people
        assert people.lookup_account("qq", "shared") == bound_id
        assert people.person_id_for_user("old")
        conflict_id = people.person_id_for_user("conflict")
        assert conflict_id and conflict_id != bound_id
        assert (
            people.conn.execute(
                "SELECT COUNT(*) FROM person_accounts WHERE person_id=?", (conflict_id,)
            ).fetchone()[0]
            == 0
        )
        database.conn.commit()

    rolled_back = json.loads(
        (
            await server.api_persona_people(
                _people_request(
                    "POST", "migration-rollback", body={"migration_id": applied["migration_id"]}
                )
            )
        ).text
    )
    assert rolled_back["status"] == "rolled_back"
    with PersonaDatabase(tmp_path / "persona.db") as database:
        people = UnifiedUserManager(tmp_path, conn=database.conn).people
        assert people.person_id_for_user("old") is None
        assert people.person_id_for_user("conflict") is None
        assert people.lookup_account("qq", "shared") == bound_id
        assert (
            people.conn.execute(
                "SELECT COUNT(*) FROM users WHERE user_id IN ('old','conflict')"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.asyncio
async def test_admin_quota_and_two_step_delete_use_shared_persona_database(
    tmp_path: Path,
) -> None:
    server = _people_server(tmp_path)
    photo = tmp_path / "original.png"
    buffer = io.BytesIO()
    Image.new("RGB", (12, 12), (20, 100, 200)).save(buffer, format="PNG")
    photo.write_bytes(buffer.getvalue())
    with PersonaDatabase(tmp_path / "persona.db") as database:
        people = UnifiedUserManager(tmp_path, persona_name="月白", conn=database.conn).people
        visual = VisualService(tmp_path, people=people, conn=database.conn)
        ref = people.record_evidence(
            group_id="g",
            user_id="owner",
            platform="qq",
            platform_uid="100",
            message_id="m1",
            content="登记我的原图",
            attachments=[str(photo)],
        )
        person_id = people.get_evidence(ref)["actor_id"]
        access = PersonAccess(actor_id=person_id, group_id="g", evidence_refs=(ref,))
        staged = await visual.manage(
            "stage",
            access=access,
            evidence_ref=ref,
            target={"ref": "requester"},
            source={"kind": "attachment", "index": 0},
            activate=True,
        )
        assert staged["success"], staged
        asset_path = (
            tmp_path / "visual_assets" / visual.store.asset(staged["data"]["asset_id"])["rel_path"]
        )
        assert asset_path.is_file()
        database.conn.commit()

    response = await server.api_persona_people(
        _people_request("POST", "policy", body={"total_quota": 2, "auto_enabled": True})
    )
    assert response.status == 200, response.text
    state = json.loads(
        (await server.api_persona_people(_people_request("GET", "state", key="person_id"))).text
    )
    assert state["data"]["deployment"]["total_quota"] == 2
    assert state["data"]["deployment"]["auto_enabled"] is True
    assert state["data"]["asset_root"] == "visual_assets"
    assert str(tmp_path) not in json.dumps(state, ensure_ascii=False)

    prepared = json.loads(
        (
            await server.api_persona_people(
                _people_request("POST", "deletion-preview", body={"person_id": person_id})
            )
        ).text
    )
    assert prepared["status"] == "confirm_required"
    assert prepared["data"]["preview"]["counts"]["originals"] == 1
    assert asset_path.is_file()
    deleted = json.loads(
        (
            await server.api_persona_people(
                _people_request(
                    "POST",
                    "deletion-confirm",
                    body={
                        "confirm_token": prepared["data"]["confirm_token"],
                    },
                )
            )
        ).text
    )
    assert deleted["status"] == "deleted"
    assert deleted["data"]["generation_blocked"] is True
    assert not asset_path.exists()
    replay = await server.api_persona_people(
        _people_request(
            "POST",
            "deletion-confirm",
            body={
                "confirm_token": prepared["data"]["confirm_token"],
            },
        )
    )
    assert replay.status == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["human", "character", "persona"])
async def test_third_party_submission_binds_visible_target_without_inheriting_control(
    conversation, target_kind
):
    chat = conversation
    owner_ref = chat.message("alice", "这是我本人，也登记我控制的角色小羽")
    if target_kind == "character":
        created = await chat.update(owner_ref, action="create_character", name="小羽")
        assert created.success, created.data
        target_id = created.data["person_id"]
    else:
        target_id = chat.actor(owner_ref) if target_kind == "human" else chat.people.persona_id
    target = {"ref": "person_id", "value": target_id}
    before = await chat.query(owner_ref, target, sections=["names", "claims"])
    admin_photo = chat.photo("first-admin-unrelated")
    Image.new("RGB", (16, 16), (190, 40, 60)).save(admin_photo)
    uploaded_photo = chat.photo("contributed-target-image")
    admin_ref = chat.message("admin", "管理员只处理自己的消息", admin=True, attachments=(admin_photo,))
    upload_ref = chat.message(
        "bob",
        "把我的附件作为指定人物的参考图，不改对方账号和资料；我自己的称呼请用乙的自选称呼",
        attachments=(uploaded_photo,),
    )
    batch = chat.invocation(admin_ref, upload_ref)
    assert batch.caller_is_developer
    staged = await chat.call(
        "visual_asset_manage",
        batch,
        action="stage",
        target=target,
        evidence_ref=upload_ref,
        source={"kind": "attachment", "index": 0},
        activate=True,
        identity_description="由乙投稿的蓝色围巾参考",
    )
    assert staged.success and staged.data["data"]["status"] == "draft", staged.data
    draft = staged.data["data"]
    assert draft["activation"]["success"] is False
    assert draft["activation"]["status"] == "confirmation_required"
    assert draft["activation"]["code"] == "permission_denied"
    assert draft["activation"]["summary"]
    profile = chat.visual.store.profile(draft["profile_id"])
    revision = chat.visual.store.revision(draft["profile_id"], draft["revision"])
    asset = chat.visual.store.asset(draft["asset_id"])
    uploader_id = chat.actor(upload_ref)
    assert profile["person_id"] == revision["person_id"] == asset["person_id"] == target_id
    assert profile["active_revision"] is None
    assert revision["created_by"] == asset["uploader_id"] == asset["actor_id"] == uploader_id
    assert asset["source_ref"] == upload_ref
    assert (
        chat.root / "visual_assets" / asset["rel_path"]
    ).read_bytes() == uploaded_photo.read_bytes()
    assert uploaded_photo.read_bytes() != admin_photo.read_bytes()

    for action, extra in (
        ("activate", {"profile_id": draft["profile_id"], "revision": draft["revision"]}),
        ("retire", {"asset_id": draft["asset_id"]}),
    ):
        denied = await chat.call(
            "visual_asset_manage",
            batch,
            action=action,
            target=target,
            evidence_ref=upload_ref,
            **extra,
        )
        assert not denied.success and denied.data["code"] == "permission_denied", denied.data
    deletion = await chat.visual.prepare_deletion(
        target, chat.context.get_person_access(chat.invocation(upload_ref)), evidence_ref=upload_ref
    )
    assert deletion["success"] is False and deletion["code"] == "permission_denied", deletion
    name_change = await chat.call(
        "person_update",
        batch,
        action="set_names",
        target=target,
        evidence_ref=upload_ref,
        preferred_name="投稿不能重命名他人",
    )
    assert not name_change.success and name_change.error == "permission_denied", name_change.data
    after = await chat.query(owner_ref, target, sections=["names", "claims"])
    assert after == before
    assert chat.visual.store.profile(draft["profile_id"])["active_revision"] is None
    assert chat.visual.store.asset(draft["asset_id"])["status"] == "available"

    own_name = await chat.call(
        "person_update",
        batch,
        action="set_names",
        target={"ref": "requester"},
        evidence_ref=upload_ref,
        preferred_name="乙的自选称呼",
    )
    assert own_name.success and own_name.data["person_id"] == uploader_id, own_name.data
    assert own_name.data["display_name"] == "乙的自选称呼"
    assert chat.people.lookup_account("qq", "qq-bob") == uploader_id
    assert chat.people.lookup_account("qq", "qq-admin") == chat.actor(admin_ref)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_source", ["history", "cross-group", "forged", "other-author-attachment"]
)
async def test_shared_library_submission_still_requires_its_own_admitted_attachment(
    conversation, bad_source
):
    chat = conversation
    target_ref = chat.message("alice", "我在本群的资料是可见的")
    target = {"ref": "person_id", "value": chat.actor(target_ref)}
    photo = chat.photo("untrusted-source-image")
    historical_ref = chat.message("bob", "这是过去的附件，不是当前指令", attachments=(photo,))
    private_ref = chat.message("bob", "这是另一群的附件", group="private", attachments=(photo,))
    admin_ref = chat.message("admin", "附件属于我的消息", admin=True, attachments=(photo,))
    current_ref = chat.message("bob", "我本轮没有发送任何图片")
    refs = {
        "history": historical_ref,
        "cross-group": private_ref,
        "forged": "ev_forged_attachment_instruction",
        "other-author-attachment": current_ref,
    }
    before = chat.people.conn.execute("SELECT COUNT(*) FROM vs_asset").fetchone()[0]
    staged = await chat.call(
        "visual_asset_manage",
        chat.invocation(admin_ref, current_ref),
        action="stage",
        target=target,
        evidence_ref=refs[bad_source],
        source={"kind": "attachment", "index": 0},
        activate=False,
    )
    assert not staged.success, staged.data
    if bad_source == "other-author-attachment":
        assert staged.data["code"] == "asset_invalid", staged.data
    else:
        assert "evidence_invalid" in staged.error, staged.data
    assert chat.people.conn.execute("SELECT COUNT(*) FROM vs_asset").fetchone()[0] == before
    assert chat.visual.store.profiles_for(chat.actor(target_ref)) == []
