"""共享人物素材库的业务验收（含证据、版本、自动候选与删除回归）。

可见素材的手动查看/读图/生图/发送无需逐人物 grant；控制权和持续自动付费仍独立。
这些用例通过登记原图 → 真实工具/队列 → 回调 → 停用/删除，检查字节、持久化与回执。
"""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.people import PersonAccess, PersonService
from sirius_pulse.tools.builtin import group_file_exec, image_generate, person_query, read_image
from sirius_pulse.tools.builtin.visual_asset_manage import run as manage_tool_run
from sirius_pulse.tools.data_store import ToolDataStore
from sirius_pulse.tools.models import ToolInvocationContext
from sirius_pulse.visual import VisualError, VisualService, policies

pytestmark = pytest.mark.asyncio

PNG_MIME = "image/png"


def _png(color: tuple[int, int, int] = (200, 30, 30), size: tuple[int, int] = (48, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeQueue:
    """记录领域服务交给现有生图队列的派生任务。"""

    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def enqueue_visual(self, **kwargs) -> None:
        self.jobs.append(kwargs)


class Env:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.work = root / "persona"
        self.work.mkdir(parents=True, exist_ok=True)
        self.chat_dir = root / "chat_cache"
        self.chat_dir.mkdir()
        self.people = PersonService(None, persona_name="月白")
        self.service = VisualService(self.work, people=self.people, conn=self.people.conn)
        self._seq = 0
        self._admin: tuple[PersonAccess, str] | None = None

    # ------------------------------------------------------------------
    def photo(self, name: str = "photo", color=(200, 30, 30)) -> Path:
        path = self.chat_dir / f"{name}.png"
        path.write_bytes(_png(color))
        return path

    def access(
        self,
        user: str = "u1",
        *,
        group: str = "g1",
        name: str = "阿甲",
        admin: bool = False,
        attachments: list[Path] | None = None,
        self_initiated: bool = False,
    ) -> tuple[PersonAccess, str, str]:
        """返回 (PersonAccess, evidence_ref, person_id)，并为该用户建群内称呼。"""
        self._seq += 1
        ref = self.people.record_evidence(
            group_id=group,
            user_id=user,
            platform="napcat",
            platform_uid=f"qq_{user}",
            message_id=f"m{self._seq}",
            content="请按我的明确要求处理形象",
            attachments=[str(path) for path in attachments or []],
        )
        person_id = self.people.person_id_for_user(user) or ""
        access = PersonAccess(
            actor_id=person_id,
            group_id=group,
            evidence_refs=(ref,),
            is_admin=admin,
            self_initiated=self_initiated,
        )
        existing = self.people.conn.execute(
            "SELECT 1 FROM person_names WHERE person_id=? AND active=1", (person_id,)
        ).fetchone()
        if not existing:
            self.people.update_names(person_id, access, ref, preferred_name=name)
        return access, ref, person_id

    def admin(self) -> tuple[PersonAccess, str, str]:
        if self._admin is None:
            access, ref, person_id = self.access("admin", name="管理员", admin=True)
            self._admin = (access, ref, person_id)
        return self._admin

    def invocation(
        self, access: PersonAccess, ref: str, *, user: str = "u1", background_event_id: str = ""
    ) -> ToolInvocationContext:
        return ToolInvocationContext(
            caller=UnifiedUser(user_id=user, name=user),
            group_id=access.group_id,
            evidence_refs=(ref,),
            background_event_id=background_event_id,
        )

    def store(self) -> ToolDataStore:
        path = self.work / "tool_data" / "image_generate.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return ToolDataStore(path)

    async def stage(
        self,
        access: PersonAccess,
        ref: str,
        *,
        target: dict | None = None,
        file: Path | None = None,
        activate: bool = True,
        description: str = "黑发红围巾，校服",
        outfit: str = "校服",
        view: str = "正面",
    ) -> dict:
        """按用户"记一下我的样子"的真实操作登记原图。"""
        return await self.service.manage(
            "stage",
            access=access,
            evidence_ref=ref,
            target=target or {"ref": "requester"},
            source={"kind": "attachment", "index": 0},
            identity_description=description,
            default_outfit=outfit,
            view=view,
            activate=activate,
        )

    def asset_file(self, asset_id: str) -> Path:
        asset = self.service.store.asset(asset_id)
        assert asset is not None
        return self.work / "visual_assets" / str(asset["rel_path"])


class FakeEngine:
    """最小引擎上下文：可观察生图请求、回调与真实数据落盘。"""

    def __init__(self, env: Env, access: PersonAccess, store: ToolDataStore) -> None:
        self.env = env
        self.access = access
        self.store = store
        self.generated: list[dict] = []
        self.callbacks: list[dict] = []
        self.image_bytes = _png((20, 120, 220))
        self.callback_result = {"status": "completed", "text": "已处理"}
        self.accepted = True

    def get_person_service(self):
        return self.env.people

    def get_visual_service(self):
        return self.env.service

    def get_person_access(self, invocation_context):
        return self.access

    def get_data_store(self, name: str):
        return self.store

    async def generate_image(self, *, prompt, reference_images, timeout_seconds):
        self.generated.append(
            {
                "prompt": prompt,
                "references": [(name, image) for name, image in reference_images],
            }
        )
        return {"image_bytes": self.image_bytes, "mime_type": PNG_MIME}

    async def handle_background_tool_result(self, **kwargs):
        self.callbacks.append(kwargs)
        return self.callback_result

    async def dispatch_proactive_message(self, **kwargs):
        return self.accepted


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


async def _submit_person_job(env: Env, access: PersonAccess, ref: str, engine: FakeEngine):
    return await image_generate.run(
        "画我在雪地里",
        subjects=[{"ref": "requester"}],
        source_ref=ref,
        data_store=engine.store,
        chat_context={
            "group_id": access.group_id,
            "chat_type": "group",
            "chat_id": access.group_id,
        },
        invocation_context=env.invocation(access, ref),
        engine_context=engine,
    )


# ----------------------------------------------------------------------
# AT-14 / AT-17：原图长期保存，且不进入旧人格自图扫描树
# ----------------------------------------------------------------------
async def test_stage_original_survives_chat_cache_and_stays_out_of_legacy_assets(env):
    source = env.photo("selfie")
    access, ref, person_id = env.access(attachments=[source])
    staged = await env.stage(access, ref, description="黑发红围巾，左眼下有泪痣")
    assert staged["success"] and staged["status"] == "active"
    revision = staged["data"]["revision"]
    asset_id = staged["data"]["asset_id"]
    original = env.asset_file(asset_id)
    assert original.is_file() and original.read_bytes() == source.read_bytes()
    assert "visual_assets" in original.as_posix()
    assert not (env.work / "assets").exists()  # 旧人格自图扫描树不收录用户图

    source.unlink()  # 聊天缓存被清理后仍可用
    plan = await env.service.compile_generation(
        access=access,
        subjects=[{"ref": "requester", "revision": revision}],
        prompt="画我在雪地里",
    )
    assert len(plan["images"]) == 1
    assert plan["images"][0]["bytes"] == original.read_bytes()

    summary = await env.service.get_summary(person_id, access)
    assert summary["visual"]["state"] == "active"
    assert summary["visual"]["active"][0]["identity_description"].startswith("黑发红围巾")
    # 结果只给稳定 ID/摘要，不泄露存储绝对路径。
    assert str(env.work) not in json.dumps(summary, ensure_ascii=False)


# ----------------------------------------------------------------------
# AT-16：伪图片、超大图、越界路径、内网 URL 都不得入库
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "source_factory",
    [
        lambda env: {"kind": "attachment", "index": 0},  # 伪图片字节
        lambda env: {"kind": "url", "url": "http://127.0.0.1:8080/internal"},
        lambda env: {"kind": "path", "path": "C:/Windows/win.ini"},
    ],
)
async def test_invalid_source_is_rejected(env, source_factory):
    fake = env.chat_dir / "fake.png"
    fake.write_bytes(b"not-a-real-image")
    access, ref, person_id = env.access(attachments=[fake])
    result = await env.service.manage(
        "stage",
        access=access,
        evidence_ref=ref,
        target={"ref": "requester"},
        source=source_factory(env),
    )
    assert result["success"] is False
    assert result["code"] == "asset_invalid"
    assert env.service.store.assets_for(person_id) == []
    assert not (env.work / "visual_assets" / person_id).exists()


async def test_oversized_image_is_rejected_before_storage(env):
    oversized = env.chat_dir / "huge.png"
    oversized.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (20 * 1024 * 1024 + 1))
    access, ref, person_id = env.access(attachments=[oversized])
    result = await env.stage(access, ref)
    assert result["success"] is False and result["code"] == "asset_invalid"
    assert env.service.store.assets_for(person_id) == []


# ----------------------------------------------------------------------
# AT-18 / AT-19 / AT-20：双人编译、服装覆盖、参考上限不截断必填人物
# ----------------------------------------------------------------------
async def test_compile_two_subjects_with_outfit_and_reference_budget(env, tmp_path):
    admin, admin_ref, _ = env.admin()
    persona_photo = env.photo("persona", color=(240, 240, 240))
    admin, admin_ref, _ = env.access("admin", name="管理员", admin=True, attachments=[persona_photo])
    persona_stage = await env.stage(
        admin,
        admin_ref,
        target={"ref": "persona"},
        description="银发红瞳，白色长裙",
        outfit="白色长裙",
    )
    assert persona_stage["success"], persona_stage

    self_photo = env.photo("self")
    user, ref, user_id = env.access(attachments=[self_photo])
    self_stage = await env.stage(user, ref, description="黑发红围巾")
    assert self_stage["success"]

    extras = []
    for index in range(3):
        extra = tmp_path / f"extra_{index}.png"
        extra.write_bytes(_png((index * 20, 100, 100)))
        extras.append(str(extra))

    plan = await env.service.compile_generation(
        access=user,
        subjects=[
            {"ref": "requester"},
            {"ref": "persona", "outfit": "红色礼服"},
        ],
        prompt="画我和月白在雪地里并肩站着",
        extra_references=extras,
    )
    assert len(plan["subjects"]) == 2
    assert [item["display_name"] for item in plan["subjects"]] == ["阿甲", "月白"]
    assert "红色礼服" in plan["prompt"]
    assert "月白" in plan["prompt"]
    # 每个人物都必须保留，可选附加图先被裁掉，不允许偷偷少一个人。
    required = [image for image in plan["images"] if image["purpose"] != "extra"]
    assert len(required) == 2
    assert len(plan["images"]) == 4
    assert plan["dropped_optional"] == 1

    persona_summary = await env.service.get_summary(env.people.persona_id, user)
    persona_active = persona_summary["visual"]["active"][0]
    assert persona_active["default_outfit"] == "白色长裙"  # 本次换装不写回正式形象

    five: list[dict] = [{"ref": "requester"}]
    for index in range(4):
        created = await env.service.manage(
            "create_character",
            access=user,
            evidence_ref=ref,
            target={"ref": "requester"},
            name=f"角色{index}",
        )
        assert created["success"], created
        character_id = created["data"]["person_id"]
        character_photo = env.photo(f"character_{index}", color=(index * 30, 50, 50))
        char_access, char_ref, _ = env.access("u1", name="阿甲", attachments=[character_photo])
        staged_char = await env.stage(
            char_access,
            char_ref,
            target={"ref": "person_id", "value": character_id},
        )
        assert staged_char["success"], staged_char
        five.append({"ref": "person_id", "value": character_id})
    with pytest.raises(VisualError) as excinfo:
        await env.service.compile_generation(
            access=user,
            subjects=five,
            prompt="五个人的合影",
        )
    assert excinfo.value.code == "reference_limit_exceeded"


async def test_compile_shared_person_without_grant_still_preserves_subject_and_bytes(env):
    photo = env.photo("shared_compile")
    owner, owner_ref, owner_id = env.access("u1", attachments=[photo])
    other, _, _ = env.access("u2", name="阿乙")
    staged = await env.stage(owner, owner_ref)
    assert staged["success"]
    plan = await env.service.compile_generation(
        access=other, subjects=[{"ref": "person_id", "value": owner_id}], prompt="画阿甲"
    )
    assert [subject["person_id"] for subject in plan["subjects"]] == [owner_id]
    assert [image["bytes"] for image in plan["images"]] == [photo.read_bytes()]
    assert plan["manifest"]["subjects"][0]["grant_id"] == ""
    assert env.service.store.grants_for(owner_id) == []


# ----------------------------------------------------------------------
# AT-21：必填参考图缺失时在计费/上传前明确失败
# ----------------------------------------------------------------------
async def test_missing_required_reference_fails_before_generation(env):
    photo = env.photo("self")
    access, ref, _ = env.access(attachments=[photo])
    staged = await env.stage(access, ref)
    assert staged["success"]
    env.asset_file(staged["data"]["asset_id"]).unlink()

    with pytest.raises(VisualError) as excinfo:
        await env.service.compile_generation(
            access=access, subjects=[{"ref": "requester"}], prompt="画我"
        )
    assert excinfo.value.code == "required_asset_missing"

    engine = FakeEngine(env, access, env.store())
    result = await _submit_person_job(env, access, ref, engine)
    assert result["success"] is False
    assert engine.generated == []
    assert image_generate._load_jobs(engine.store) == []


async def test_reference_deleted_after_enqueue_blocks_backend_read(env):
    photo = env.photo("self")
    access, ref, _ = env.access(attachments=[photo])
    staged = await env.stage(access, ref)
    assert staged["success"]
    engine = FakeEngine(env, access, env.store())
    submitted = await _submit_person_job(env, access, ref, engine)
    assert submitted["success"], submitted

    job_id = submitted["data"]["job_id"]
    reference_dir = engine.store.artifact_dir / "references" / job_id
    for item in reference_dir.iterdir():
        item.unlink()

    await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert engine.generated == []
    job = next(job for job in image_generate._load_jobs(engine.store) if job["id"] == job_id)
    assert job["status"] == "failed"
    assert env.service.store.job(job_id)["queue_state"] in {"failed", "cancelled"}


# ----------------------------------------------------------------------
# AT-22：普通风景生图与旧参数调用不自动加人物
# ----------------------------------------------------------------------
async def test_plain_landscape_generation_stays_out_of_person_branch(env):
    user, ref, _ = env.access()
    engine = FakeEngine(env, user, env.store())
    submitted = await image_generate.run(
        "画一片没有人的雪原",
        data_store=engine.store,
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        invocation_context=env.invocation(user, ref),
        engine_context=engine,
    )
    assert submitted["success"], submitted
    job = next(
        item
        for item in image_generate._load_jobs(engine.store)
        if item["id"] == submitted["data"]["job_id"]
    )
    assert "visual" not in job
    await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert len(engine.generated) == 1
    assert engine.generated[0]["references"] == []  # 没加任何人物参考图
    assert not any((env.work / "visual_assets").iterdir())  # 没建任何人物目录


# ----------------------------------------------------------------------
# AT-27 / AT-28 / AT-29：启用必须 CAS，非控制者不能启用
# ----------------------------------------------------------------------
async def test_activation_uses_cas_and_rejects_non_owner(env):
    photo = env.photo("v1")
    access, ref, person_id = env.access(attachments=[photo])
    v1 = await env.stage(access, ref, description="第一版")
    second_photo = env.photo("v2", color=(10, 10, 200))
    access, ref2, person_id = env.access("u1", name="阿甲", attachments=[second_photo])
    v2 = await env.stage(access, ref2, activate=False, description="第二版")
    assert v2["data"]["status"] == "draft"
    profile_id = v1["data"]["profile_id"]

    activated = await env.service.manage(
        "activate",
        access=access,
        evidence_ref=ref2,
        target={"ref": "requester"},
        profile_id=profile_id,
        revision=v2["data"]["revision"],
        expected_revision=1,
    )
    assert activated["success"] and activated["status"] == "active"

    stale = await env.service.manage(
        "activate",
        access=access,
        evidence_ref=ref2,
        target={"ref": "requester"},
        profile_id=profile_id,
        revision=1,
        expected_revision=1,
    )
    assert stale["success"] is False and stale["code"] == "revision_conflict"
    assert env.service.store.profile(profile_id)["active_revision"] == v2["data"]["revision"]

    # 重新查询后按当前 CAS 可以恢复旧修订（没有被撤权）。
    restored = await env.service.manage(
        "activate",
        access=access,
        evidence_ref=ref2,
        target={"ref": "requester"},
        profile_id=profile_id,
        revision=1,
        expected_revision=v2["data"]["revision"],
    )
    assert restored["success"] and restored["status"] == "active"

    other, other_ref, _ = env.access("u2", name="阿乙")
    denied = await env.service.manage(
        "activate",
        access=other,
        evidence_ref=other_ref,
        target={"ref": "person_id", "value": person_id},
        profile_id=profile_id,
        revision=2,
        expected_revision=1,
    )
    assert denied["success"] is False and denied["code"] == "permission_denied"


# ----------------------------------------------------------------------
# 共享素材能力不再使用逐人物许可；控制权与自动候选授权仍独立。
# ----------------------------------------------------------------------
async def test_shared_manual_use_does_not_authorize_maintenance_or_automation(env):
    photo = env.photo("shared_capabilities")
    owner, owner_ref, owner_id = env.access("u1", attachments=[photo])
    staged = await env.stage(owner, owner_ref)
    asset_id = staged["data"]["asset_id"]
    viewer, ref, _ = env.access("u2", name="阿乙")
    overview = await env.service.get_summary(owner_id, viewer)
    assert overview["visual"]["state"] == "active" and "consents" not in overview
    assert (await env.service.open_asset_for_read(asset_id, viewer))["bytes"] == photo.read_bytes()
    guard = await env.service.guard_path(env.asset_file(asset_id), viewer, "external_send")
    assert guard["managed"] and guard["allowed"]
    for action in ("set_policy", "retire"):
        denied = await env.service.manage(
            action,
            access=viewer,
            evidence_ref=ref,
            target={"ref": "person_id", "value": owner_id},
            profile_id=staged["data"]["profile_id"],
            revision=1,
            mode="auto_candidate",
            capabilities=["upload_generate"],
        )
        assert denied["success"] is False and denied["code"] == "permission_denied"
    queue = FakeQueue()
    assert (await env.service.auto_candidates(queue))["submitted"] == []
    assert queue.jobs == [] and env.service.store.grants_for(owner_id) == []


# ----------------------------------------------------------------------
# AT-41 / AT-45：旧入口拿裸路径也受同一权限约束
# ----------------------------------------------------------------------
async def test_legacy_tools_guard_managed_paths_and_task_copies(env):
    photo = env.photo("self")
    owner, owner_ref, owner_id = env.access("u1", name="阿甲", attachments=[photo])
    staged = await env.stage(owner, owner_ref)
    asset_path = env.asset_file(staged["data"]["asset_id"])
    env.access("u1", group="g2", name="阿甲")  # 身份可见不等于跨群开放私有素材。
    other, other_ref, _ = env.access("u2", group="g2", name="阿乙")

    engine = FakeEngine(env, other, env.store())
    denied_read = await read_image.run(
        paths=[str(asset_path)],
        engine_context=engine,
        invocation_context=env.invocation(other, other_ref, user="u2"),
        data_store=env.store(),
    )
    assert denied_read["success"] is False
    assert denied_read["code"] == "consent_required"

    owner_engine = FakeEngine(env, owner, env.store())
    allowed_read = await read_image.run(
        paths=[str(asset_path)],
        engine_context=owner_engine,
        invocation_context=env.invocation(owner, owner_ref),
        data_store=env.store(),
    )
    assert allowed_read["success"] is True

    asset_id = staged["data"]["asset_id"]
    asset_read = await read_image.run(
        assets=[asset_id],
        engine_context=owner_engine,
        invocation_context=env.invocation(owner, owner_ref),
        data_store=env.store(),
    )
    assert asset_read["success"] is True
    denied_asset = await read_image.run(
        assets=[asset_id],
        engine_context=engine,
        invocation_context=env.invocation(other, other_ref, user="u2"),
        data_store=env.store(),
    )
    assert denied_asset["success"] is False

    denied_generate = await image_generate.run(
        "把这张图改一下",
        reference_images=[str(asset_path)],
        data_store=env.store(),
        invocation_context=env.invocation(other, other_ref, user="u2"),
        engine_context=engine,
    )
    assert denied_generate["success"] is False
    assert denied_generate["code"] == "consent_required"

    class Bridge:
        adapter = None

        def __init__(self):
            class Adapter:
                async def send_group_msg(self, target_id, message):
                    return {"data": {"message_id": 1}}

            self.adapter = Adapter()

    denied_send = await group_file_exec.run(
        "image",
        image_path=str(asset_path),
        bridge=Bridge(),
        chat_context={"group_id": "g2", "chat_type": "group", "chat_id": "g2"},
        engine_context=engine,
        invocation_context=env.invocation(other, other_ref, user="u2"),
        data_store=env.store(),
    )
    assert denied_send["success"] is False
    assert denied_send["data"]["code"] == "consent_required"

    # 任务副本同样按任务绑定的主体鉴权，不能靠 "裸路径" 绕过。
    job = await _submit_person_job(env, owner, owner_ref, owner_engine)
    assert job["success"], job
    copy_dir = owner_engine.store.artifact_dir / "references" / job["data"]["job_id"]
    copy = next(copy_dir.iterdir())
    denied_copy = await read_image.run(
        paths=[str(copy)],
        engine_context=engine,
        invocation_context=env.invocation(other, other_ref, user="u2"),
        data_store=env.store(),
    )
    assert denied_copy["success"] is False

    # 旧队列里没有绑定、但内容与受管素材相同的任务：不能验证授权 → 隔离。
    legacy_dir = owner_engine.store.artifact_dir / "references" / "img_legacy01"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "01_copy.png").write_bytes(asset_path.read_bytes())
    jobs = image_generate._load_jobs(owner_engine.store)
    jobs.append(
        {
            "id": "img_legacy01",
            "status": "pending",
            "prompt": "旧任务",
            "references": ["01_copy.png"],
            "group_id": "g1",
            "user_id": "u1",
            "adapter_type": "napcat",
            "callback_state": "pending",
            "callback_attempts": 0,
            "notified": False,
            "delivery_version": 3,
        }
    )
    image_generate._save_jobs(owner_engine.store, jobs)
    await image_generate._process_once(owner_engine, owner_engine.store, recover_interrupted=True)
    # 合法的自有人物任务可以继续；旧任务不能借同样的素材绕过绑定。
    assert all("旧任务" not in item["prompt"] for item in owner_engine.generated)
    legacy = next(
        item
        for item in image_generate._load_jobs(owner_engine.store)
        if item["id"] == "img_legacy01"
    )
    assert legacy["status"] == "cancelled"
    assert legacy["isolated"] is True


# ----------------------------------------------------------------------
# AT-23..AT-26 / AT-42：自动候选要有明确授权、预算、幂等且只产候选
# ----------------------------------------------------------------------
async def test_auto_candidate_requires_grant_budget_and_is_idempotent(env, monkeypatch):
    photo = env.photo("self")
    user, ref, person_id = env.access(attachments=[photo])
    staged = await env.stage(user, ref)
    assert staged["success"]

    engine = FakeEngine(env, user, env.store())
    queue = image_generate.build_visual_queue(engine.store)
    no_grant = await env.service.auto_candidates(queue)
    assert no_grant["submitted"] == [] and image_generate._load_jobs(engine.store) == []
    direct = await env.service.manage(
        "derive", access=user, evidence_ref=ref, target={"ref": "requester"}, queue=queue, auto=True
    )
    assert direct["success"] is False and direct["code"] == "consent_required"

    someone, someone_ref, _ = env.access("u2", name="阿乙")
    denied_policy = await env.service.manage(
        "set_policy",
        access=someone,
        evidence_ref=someone_ref,
        target={"ref": "person_id", "value": person_id},
        mode="auto_candidate",
        capabilities=["upload_generate"],
        expires_at="2999-01-01T00:00:00+00:00",
    )
    assert denied_policy["success"] is False and denied_policy["code"] == "permission_denied"
    denied_admin = await env.service.set_deployment_policy(someone, total_quota=99)
    assert denied_admin["success"] is False and denied_admin["code"] == "permission_denied"

    admin, admin_ref, _ = env.admin()
    policy = await env.service.set_deployment_policy(
        admin, total_quota=5, auto_enabled=True, per_person_daily=1
    )
    assert policy["success"], policy

    grant = await env.service.manage(
        "set_policy",
        access=user,
        evidence_ref=ref,
        target={"ref": "requester"},
        mode="auto_candidate",
        capabilities=["upload_generate"],
        expires_at="2999-01-01T00:00:00+00:00",
        scope="group:g1",
        max_uses=1,
    )
    assert grant["success"], grant
    grant_id = grant["data"]["grant_id"]
    disabled = await env.service.set_deployment_policy(admin, auto_enabled=False)
    assert disabled["success"]
    assert (await env.service.auto_candidates(queue))["submitted"] == []
    assert image_generate._load_jobs(engine.store) == []
    enabled = await env.service.set_deployment_policy(admin, auto_enabled=True)
    assert enabled["success"]

    first = await env.service.auto_candidates(queue)
    assert len(first["submitted"]) == len(image_generate._load_jobs(engine.store)) == 1
    job_id = first["submitted"][0]["data"]["job_id"]
    assert env.service.store.grant(grant_id)["used_count"] == 1
    summary = await env.service.get_summary(person_id, user)
    assert summary["visual"]["active"][0]["revision"] == 1  # 正式指针不变

    second = await env.service.auto_candidates(queue)
    assert second["submitted"] == [] and len(image_generate._load_jobs(engine.store)) == 1
    state = policies.deployment_state(env.service.store)
    assert state["used"] == 1  # 没有重复扣费

    with monkeypatch.context() as next_day:
        next_day.setattr(policies, "today_local", lambda: "2099-01-02")
        assert env.service.store.quota_used(f"auto:person:{person_id}", "2099-01-02") == 0
        await image_generate._process_once(engine, engine.store, recover_interrupted=False)
        assert len(engine.generated) == 1, image_generate._load_jobs(engine.store)
        assert [data for _, data in engine.generated[0]["references"]] == [photo.read_bytes()]
        completed = env.service.store.job(job_id)
        assert completed["queue_state"] == "done" and not completed["isolated"]
        candidate = env.service.store.asset(completed["result_asset_id"])
        assert candidate["source_ref"] == job_id
        assert env.asset_file(candidate["asset_id"]).read_bytes() == engine.image_bytes
        assert env.service.store.grant(grant_id)["used_count"] == 1
        assert policies.deployment_state(env.service.store)["used"] == 1
        summary = await env.service.get_summary(person_id, user)
        assert summary["visual"]["active"][0]["revision"] == 1
        assert [item["revision"] for item in summary["visual"]["candidates"]] == [2]
        historical = await read_image.run(
            assets=[candidate["asset_id"]],
            engine_context=engine,
            data_store=engine.store,
            invocation_context=env.invocation(user, ref),
        )
        assert historical["success"], historical
        assert Path(historical["multimodal_blocks"][0]["value"]).read_bytes() == engine.image_bytes

    third = await env.service.auto_candidates(queue)
    assert third["submitted"] == [] and len(image_generate._load_jobs(engine.store)) == 1
    exhausted = await env.service.manage(
        "derive",
        access=user,
        evidence_ref=ref,
        target={"ref": "requester"},
        queue=queue,
        auto=True,
        redo=True,
    )
    assert exhausted["success"] is False and exhausted["code"] == "consent_required"
    renewed = await _grant(
        env,
        user,
        ref,
        mode="auto_candidate",
        scope="group:g1",
        max_uses=2,
        expires_at="2999-01-01T00:00:00+00:00",
    )
    assert renewed["success"]
    extra = await env.service.manage(
        "derive",
        access=user,
        evidence_ref=ref,
        target={"ref": "requester"},
        queue=queue,
        auto=True,
        redo=True,
        grant_id=renewed["data"]["grant_id"],
    )
    assert extra["success"] is False and extra["code"] == "quota_exceeded"
    assert env.service.store.grant(renewed["data"]["grant_id"])["used_count"] == 0
    assert len(image_generate._load_jobs(engine.store)) == 1

    revoke = await env.service.manage(
        "revoke_consent",
        access=user,
        evidence_ref=ref,
        target={"ref": "requester"},
        grant_id=grant["data"]["grant_id"],
    )
    assert revoke["success"]
    after_revoke = await env.service.auto_candidates(queue)
    assert after_revoke["submitted"] == []


# ----------------------------------------------------------------------
# AT-31 / AT-32 / AT-33：撤权边界、已提交结果隔离、未知结果不重放
# ----------------------------------------------------------------------
async def test_revocation_before_permit_cancels_and_after_submit_isolates(env):
    photo = env.photo("self")
    user, ref, person_id = env.access(attachments=[photo])
    staged = await env.stage(user, ref)
    profile_id = staged["data"]["profile_id"]

    engine = FakeEngine(env, user, env.store())
    first = await _submit_person_job(env, user, ref, engine)
    assert first["success"], first
    job_id = first["data"]["job_id"]
    # 撤权发生在领取提交许可之前：任务取消，不调用上游。
    retired = await env.service.manage(
        "retire",
        access=user,
        evidence_ref=ref,
        target={"ref": "requester"},
        profile_id=profile_id,
        revision=1,
    )
    assert retired["success"]
    await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert engine.generated == []
    job = next(item for item in image_generate._load_jobs(engine.store) if item["id"] == job_id)
    assert job["status"] == "cancelled"

    # 重新登记并走到外部提交边界之后，撤权只能隔离结果。
    photo2 = env.photo("self2", color=(30, 30, 30))
    user, ref, person_id = env.access("u1", name="阿甲", attachments=[photo2])
    staged2 = await env.stage(user, ref, description="第二版")
    assert staged2["success"]
    engine2 = FakeEngine(env, user, env.store())
    engine2.image_bytes = photo2.read_bytes()  # 与已有正式原图同字节，验证隔离不污染正式资产
    second = await _submit_person_job(env, user, ref, engine2)
    assert second["success"], second
    submit_id = second["data"]["job_id"]
    preflight = await env.service.preflight_generation(submit_id)
    assert preflight["allow"] is True
    permit = await env.service.acquire_generation_permit(submit_id)
    assert permit["allow"] is True
    started = await env.service.mark_generation_submit_started(submit_id)
    assert started["allow"] is True

    retired = await env.service.manage(
        "retire",
        access=user,
        evidence_ref=ref,
        target={"ref": "requester"},
        profile_id=staged2["data"]["profile_id"],
        revision=staged2["data"]["revision"],
    )
    assert retired["success"]
    completed = await env.service.complete_generation(
        submit_id, image_bytes=_png((9, 9, 9)), mime_type=PNG_MIME
    )
    assert completed["isolated"] is True
    assert completed["deliverable"] is False and completed["enabled"] is False
    assert env.service.store.job(submit_id)["isolated"] == 1
    isolated_asset = env.service.store.asset(completed["asset_id"])
    assert isolated_asset["status"] == "isolated"
    assert completed["asset_id"] != staged2["data"]["asset_id"]
    formal = env.service.store.asset(staged2["data"]["asset_id"])
    assert formal["status"] == "available"

    blocked_job = next(
        item for item in image_generate._load_jobs(engine2.store) if item["id"] == submit_id
    )
    blocked_job.update(
        status="done",
        isolated=True,
        artifact_suppressed=True,
        image_path=str(env.asset_file(completed["asset_id"])),
        callback_state="pending",
        callback_attempts=0,
    )
    image_generate._save_jobs(engine2.store, image_generate._load_jobs(engine2.store))
    await image_generate._deliver_notification(engine2, engine2.store, blocked_job, "g1")
    assert engine2.callbacks[-1]["artifact_path"] == ""
    assert engine2.callbacks[-1]["artifact_delivery_blocked"] is True


async def test_unknown_outcome_after_restart_is_not_replayed(env):
    photo = env.photo("self")
    user, ref, person_id = env.access(attachments=[photo])
    staged = await env.stage(user, ref)
    assert staged["success"]
    admin, admin_ref, _ = env.admin()
    await env.service.set_deployment_policy(admin, total_quota=10)

    engine = FakeEngine(env, user, env.store())
    submitted = await _submit_person_job(env, user, ref, engine)
    assert submitted["success"], submitted
    job_id = submitted["data"]["job_id"]
    await env.service.preflight_generation(job_id)
    await env.service.acquire_generation_permit(job_id)
    await env.service.mark_generation_submit_started(job_id)
    used_before = policies.deployment_state(env.service.store)["used"]

    recovered = await env.service.recover_generation(job_id)
    assert recovered["code"] == "outcome_uncertain"
    assert policies.deployment_state(env.service.store)["used"] == used_before
    assert env.service.store.job(job_id)["queue_state"] == "outcome_uncertain"

    jobs = image_generate._load_jobs(engine.store)
    for job in jobs:
        if job["id"] == job_id:
            job["status"] = "running"
    image_generate._save_jobs(engine.store, jobs)
    await image_generate._process_once(engine, engine.store, recover_interrupted=True)
    assert engine.generated == []
    job = next(item for item in image_generate._load_jobs(engine.store) if item["id"] == job_id)
    assert job["status"] == "failed"
    assert "未知" in job["error"]


# ----------------------------------------------------------------------
# AT-30：任务按提交快照执行，之后换正式形象不影响图序与哈希
# ----------------------------------------------------------------------
async def test_job_uses_submitted_snapshot_after_new_revision(env):
    photo = env.photo("v1")
    user, ref, person_id = env.access(attachments=[photo])
    staged = await env.stage(user, ref)
    assert staged["success"]
    engine = FakeEngine(env, user, env.store())
    submitted = await _submit_person_job(env, user, ref, engine)
    assert submitted["success"], submitted
    job_id = submitted["data"]["job_id"]

    second_photo = env.photo("v2", color=(0, 0, 255))
    user, ref2, person_id = env.access("u1", name="阿甲", attachments=[second_photo])
    new_version = await env.stage(user, ref2, description="新版本")
    assert new_version["success"] and new_version["status"] == "active"

    await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert len(engine.generated) == 1
    sent = engine.generated[0]["references"]
    assert len(sent) == 1
    sent_bytes = sent[0][1]
    assert sent_bytes == photo.read_bytes()  # 仍用提交时的 v1，不换新图
    job = next(item for item in image_generate._load_jobs(engine.store) if item["id"] == job_id)
    assert job["status"] == "done"
    manifest = job["visual"]
    assert manifest["subjects"][0]["revision"] == 1
    assert job["reference_sha256"] == [manifest["references"][0]["sha256"]]


# ----------------------------------------------------------------------
# AT-34：完成只回调；外发由 Agent 显式决定，平台 ACK 独立记录
# ----------------------------------------------------------------------
async def test_completion_callback_and_explicit_send_receipt(env):
    photo = env.photo("self")
    user, ref, person_id = env.access(attachments=[photo])
    staged = await env.stage(user, ref)
    assert staged["success"]
    engine = FakeEngine(env, user, env.store())
    submitted = await _submit_person_job(env, user, ref, engine)
    assert submitted["success"]
    await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert len(engine.callbacks) == 1
    callback = engine.callbacks[0]
    assert callback["group_id"] == "g1"
    assert callback["artifact_path"]
    job_path = callback["artifact_path"]

    class Bridge:
        def __init__(self, fail: bool = False):
            outer = self

            class Adapter:
                async def send_group_msg(self, target_id, message):
                    if outer.fail:
                        raise RuntimeError("平台拒绝")
                    return {"data": {"message_id": 123}}

            self.adapter = Adapter()
            self.fail = fail

    sent = await group_file_exec.run(
        "image",
        image_path=job_path,
        bridge=Bridge(),
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        engine_context=engine,
        invocation_context=env.invocation(user, ref),
        data_store=engine.store,
    )
    assert sent["success"] is True
    receipts = env.service.store.fetchall("SELECT * FROM vs_delivery")
    assert len(receipts) == 1 and receipts[0]["success"] == 1
    assert receipts[0]["job_id"] == submitted["data"]["job_id"]

    failed_send = await group_file_exec.run(
        "image",
        image_path=job_path,
        bridge=Bridge(fail=True),
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        engine_context=engine,
        invocation_context=env.invocation(user, ref),
        data_store=engine.store,
    )
    assert failed_send["success"] is False
    receipts = env.service.store.fetchall("SELECT * FROM vs_delivery ORDER BY created_at")
    assert len(receipts) == 2 and receipts[-1]["success"] == 0


# ----------------------------------------------------------------------
# AT-36 / AT-44：删除预览、执行范围与"删除后不能再生成"
# ----------------------------------------------------------------------
async def test_deletion_preview_executes_and_blocks_generation(env):
    photo = env.photo("self")
    user, ref, person_id = env.access(attachments=[photo])
    staged = await env.stage(user, ref)
    assert staged["success"]
    asset_path = env.asset_file(staged["data"]["asset_id"])
    assert asset_path.is_file()

    engine = FakeEngine(env, user, env.store())
    submitted = await _submit_person_job(env, user, ref, engine)
    assert submitted["success"]

    prepared = await env.service.prepare_deletion(
        {"ref": "requester"}, user, scope="person", evidence_ref=ref
    )
    assert prepared["success"], prepared
    preview = prepared["data"]["preview"]
    assert preview["counts"]["assets"] >= 1
    token = prepared["data"]["confirm_token"]
    other, other_ref, _ = env.access("u2", name="阿乙")
    stolen = await env.service.execute_deletion(token, other, evidence_ref=other_ref)
    assert stolen["success"] is False and stolen["code"] == "permission_denied"
    other_admin = PersonAccess(actor_id="web:other-admin", group_id=user.group_id, is_admin=True)
    other_admin_attempt = await env.service.execute_deletion(token, other_admin)
    assert other_admin_attempt["success"] is False
    assert other_admin_attempt["code"] == "permission_denied"

    executed = await env.service.execute_deletion(token, user, evidence_ref=ref)
    assert executed["success"], executed
    assert executed["data"]["generation_blocked"] is True
    assert executed["data"]["cleaned"]["assets"] >= 1
    assert not asset_path.exists()
    assert env.service.store.job(submitted["data"]["job_id"])["queue_state"] in {
        "cancelled",
        "isolated",
    }

    with pytest.raises(VisualError) as excinfo:
        await env.service.compile_generation(
            access=user, subjects=[{"ref": "requester"}], prompt="再画我"
        )
    assert excinfo.value.code == "permission_denied"
    summary = await env.service.get_summary(person_id, user)
    assert summary["visual"]["state"] in {"missing", "candidate_only"}

    replay = await env.service.execute_deletion(token, user, evidence_ref=ref)
    assert replay["success"] is False and replay["code"] == "confirm_invalid"


# ----------------------------------------------------------------------
# AT-43：新建另一个 OC 不改变本人；重放不重复建角色
# ----------------------------------------------------------------------
async def test_creating_character_is_idempotent_and_does_not_touch_self(env):
    photo = env.photo("self")
    user, ref, person_id = env.access(attachments=[photo])
    staged = await env.stage(user, ref)
    assert staged["success"]

    first = await env.service.manage(
        "create_character",
        access=user,
        evidence_ref=ref,
        target={"ref": "requester"},
        name="小雀",
    )
    assert first["success"] and first["data"]["kind"] == "character"
    character_id = first["data"]["person_id"]
    assert character_id != person_id
    assert env.service.store.control(character_id) == user.actor_id

    replay = await env.service.manage(
        "create_character",
        access=user,
        evidence_ref=ref,
        target={"ref": "requester"},
        name="小雀",
    )
    assert replay["success"] and replay["status"] == "already_processed"
    assert replay["data"]["person_id"] == character_id
    assert env.service.store.control(character_id) == user.actor_id

    other = await env.service.manage(
        "create_character",
        access=user,
        evidence_ref=ref,
        target={"ref": "requester"},
        name="小鸦",
    )
    assert other["success"] and other["data"]["person_id"] != character_id
    summary = await env.service.get_summary(person_id, user)
    assert summary["visual"]["active"][0]["revision"] == 1


# ----------------------------------------------------------------------
# 工具层：服务未接入/自主回合不静默降级
# ----------------------------------------------------------------------
async def test_manage_tool_refuses_unavailable_service_and_autonomous_writes(env):
    photo = env.photo("self")
    user, ref, _ = env.access(attachments=[photo])
    unavailable = await manage_tool_run("stage")
    assert unavailable["success"] is False and unavailable["code"] == "service_unavailable"

    autonomous, autonomous_ref, _ = env.access(
        "u1", name="阿甲", attachments=[photo], self_initiated=True
    )
    engine = FakeEngine(env, autonomous, env.store())
    blocked = await manage_tool_run(
        "stage",
        target={"ref": "requester"},
        source={"kind": "attachment", "index": 0},
        evidence_ref=autonomous_ref,
        engine_context=engine,
        invocation_context=env.invocation(autonomous, autonomous_ref),
    )
    assert blocked["success"] is False and blocked["code"] == "permission_denied"


# ======================================================================
# 第二轮：越权/绕过/重放的独立回归场景
# ======================================================================


def _sql(env: Env, statement: str, params: tuple = ()) -> None:
    env.service.store.conn.execute(statement, params)
    env.service.store.conn.commit()


async def _auto_policy(env: Env, *, per_person: int = 5) -> None:
    admin, _, _ = env.admin()
    policy = await env.service.set_deployment_policy(
        admin, total_quota=20, auto_enabled=True, per_person_daily=per_person, per_persona_daily=10
    )
    assert policy["success"], policy


async def _grant(env: Env, owner, ref: str, *, target=None, **fields) -> dict:
    result = await env.service.manage(
        "set_policy",
        access=owner,
        evidence_ref=ref,
        target=target or {"ref": "requester"},
        mode=fields.pop("mode", "manual"),
        capabilities=fields.pop("capabilities", ["upload_generate"]),
        **fields,
    )
    assert result["success"], result
    return result


# ----------------------------------------------------------------------
# AUTH-03：A 群的正式/候选描述不能因为 B 群有一条粗粒度授权就整包返回
# ----------------------------------------------------------------------
async def test_summary_does_not_leak_other_group_profiles(env):
    secret = env.photo("secret", color=(10, 20, 30))
    owner_g2, ref_g2, person_id = env.access("u1", group="g2", attachments=[secret])
    staged_g2 = await env.stage(owner_g2, ref_g2, description="g2 绝密档案描述")
    assert staged_g2["success"], staged_g2

    admin, admin_ref, _ = env.admin()
    await _auto_policy(env)
    derived = await env.service.manage(
        "derive",
        access=owner_g2,
        evidence_ref=ref_g2,
        target={"ref": "requester"},
        queue=FakeQueue(),
    )
    assert derived["success"], derived
    completed = await env.service.complete_generation(
        derived["data"]["job_id"], image_bytes=_png((5, 5, 5)), mime_type=PNG_MIME
    )
    assert completed["candidate"] is True

    public = env.photo("public", color=(220, 220, 10))
    owner_g1, ref_g1, same_person = env.access("u1", group="g1", attachments=[public])
    assert same_person == person_id
    env.people.update_names(person_id, owner_g1, ref_g1, preferred_name="阿甲")
    staged_g1 = await env.stage(owner_g1, ref_g1, description="g1 公开档案描述")
    assert staged_g1["success"], staged_g1

    viewer, _, _ = env.access("u2", group="g1", name="阿乙")
    peer, peer_ref, _ = env.access("u3", group="g1", name="阿丙")
    # 无逐人物授权也共享当前群素材，但不得顺带公开其他群的私有描述。
    shared = await env.service.get_summary(person_id, peer)
    assert shared["restricted"] is False
    assert shared["visual"]["state"] == "active"
    assert "g1 公开档案描述" in json.dumps(shared, ensure_ascii=False)
    assert "g2 绝密" not in json.dumps(shared, ensure_ascii=False)

    await _grant(
        env, owner_g1, ref_g1, capabilities=["view"], scope="group:g1", grantee=viewer.actor_id
    )

    summary = await env.service.get_summary(person_id, viewer)
    blob = json.dumps(summary, ensure_ascii=False)
    assert "g1 公开档案描述" in blob
    assert "g2 绝密" not in blob
    assert str(staged_g2["data"]["asset_id"]) not in blob
    assert [item["scope"] for item in summary["visual"]["profiles"]] == ["group:g1"]
    assert [item["revision"] for item in summary["visual"]["active"]] == [1]
    assert summary["visual"]["candidates"] == []


# ----------------------------------------------------------------------
# AUTO-05：查询缺项不能顺手建档案（读操作无副作用）
# ----------------------------------------------------------------------
async def test_querying_without_visuals_does_not_create_profiles(env):
    peer, _, peer_id = env.access("u2", name="阿乙")
    viewer, viewer_ref, viewer_id = env.access("u3", name="阿丙")

    missing = await env.service.get_summary(viewer_id, viewer)
    assert missing["visual"]["state"] == "missing"
    assert env.service.store.fetchall("SELECT profile_id FROM vs_profile") == []

    shared_missing = await env.service.get_summary(peer_id, viewer)
    assert shared_missing["visual"]["state"] == "missing"
    assert env.service.store.fetchall("SELECT profile_id FROM vs_profile") == []


# ----------------------------------------------------------------------
# 旧 manual grant 撤回不影响共享手动任务，自动授权仍单独重验。
# ----------------------------------------------------------------------
async def test_manual_grant_revocation_does_not_cancel_shared_queued_job(env):
    photo = env.photo("owner")
    owner, ref, owner_id = env.access("u1", attachments=[photo])
    assert (await env.stage(owner, ref, description="黑发红围巾"))["success"]

    grantee, grantee_ref, _ = env.access("u2", name="阿乙")
    granted = await _grant(
        env,
        owner,
        ref,
        expires_at="2999-01-01T00:00:00+00:00",
        scope="group:g1",
        grantee=grantee.actor_id,
    )
    grant_id = granted["data"]["grant_id"]

    engine = FakeEngine(env, grantee, env.store())
    submitted = await image_generate.run(
        "画阿甲在雪地里",
        subjects=[{"ref": "person_id", "value": owner_id}],
        source_ref=grantee_ref,
        data_store=engine.store,
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        invocation_context=env.invocation(grantee, grantee_ref, user="u2"),
        engine_context=engine,
    )
    assert submitted["success"], submitted
    job_id = submitted["data"]["job_id"]

    job = env.service.store.job(job_id)
    assert job["grant_id"] == ""
    subject = job["manifest"]["subjects"][0]
    assert subject["person_id"] == owner_id and subject["revision"] == 1
    assert not subject["grant_id"] and subject["capability"] == "upload_generate"
    assert not subject.get("grant")
    assert int(env.service.store.grant(grant_id)["used_count"]) == 0

    revoke = await env.service.manage(
        "revoke_consent",
        access=owner,
        evidence_ref=ref,
        target={"ref": "requester"},
        grant_id=grant_id,
    )
    assert revoke["success"] and job_id not in revoke["data"]["cancelled_jobs"]

    await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert len(engine.generated) == 1
    assert [data for _, data in engine.generated[0]["references"]] == [photo.read_bytes()]
    queue_job = next(
        item for item in image_generate._load_jobs(engine.store) if item["id"] == job_id
    )
    assert queue_job["status"] == "done" and not queue_job.get("isolated")
    assert engine.callbacks[-1]["artifact_path"]
    assert int(env.service.store.grant(grant_id)["used_count"]) == 0


# ----------------------------------------------------------------------
# AT-30：能力调整/到期/次数/范围/来源修订在提交前按实际任务再验一遍
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "mutation",
    [
        "capability_removed",
        "expired",
        "uses_exhausted",
        "scope_narrowed",
        "source_revision_changed",
        "source_scope_tampered",
    ],
)
async def test_auto_grant_constraints_revalidated_before_upstream(env, mutation):
    await _auto_policy(env)
    owner, ref, _ = env.access("u1", attachments=[env.photo("auto_recheck")])
    staged = await env.stage(owner, ref)
    assert staged["success"]
    granted = await _grant(
        env,
        owner,
        ref,
        mode="auto_candidate",
        capabilities=["view", "upload_generate"],
        expires_at="2999-01-01T00:00:00+00:00",
        max_uses=5,
        scope="group:g1",
    )
    grant_id = granted["data"]["grant_id"]
    engine = FakeEngine(env, owner, env.store())
    submitted = await env.service.auto_candidates(image_generate.build_visual_queue(engine.store))
    assert len(submitted["submitted"]) == 1
    job_id = submitted["submitted"][0]["data"]["job_id"]
    assert env.service.store.job(job_id)["grant_id"] == grant_id
    assert env.service.store.grant(grant_id)["used_count"] == 1
    if mutation == "capability_removed":
        adjusted = await env.service.manage(
            "revoke_consent",
            access=owner,
            evidence_ref=ref,
            target={"ref": "requester"},
            capabilities=["upload_generate"],
        )
        assert adjusted["success"] and adjusted["data"]["adjusted"] == [grant_id]
    elif mutation == "expired":
        _sql(
            env,
            "UPDATE vs_grant SET expires_at=? WHERE grant_id=?",
            ("2000-01-01T00:00:00+00:00", grant_id),
        )
    elif mutation == "uses_exhausted":
        _sql(env, "UPDATE vs_grant SET max_uses=1, used_count=2 WHERE grant_id=?", (grant_id,))
    elif mutation == "scope_narrowed":
        _sql(env, "UPDATE vs_grant SET scope='group:g2' WHERE grant_id=?", (grant_id,))
    elif mutation == "source_revision_changed":
        _sql(env, "UPDATE vs_grant SET source_revision=999 WHERE grant_id=?", (grant_id,))
    else:
        _sql(
            env,
            "UPDATE vs_profile SET scope='group:g2' WHERE profile_id=?",
            (staged["data"]["profile_id"],),
        )
        assert env.service.store.grant(grant_id)["scope"] == "group:g1"
    preflight = await env.service.preflight_generation(job_id)
    assert preflight["allow"] is False, preflight
    assert preflight["code"] in (
        {"consent_required", "cancelled_by_revocation"}
        if mutation == "source_scope_tampered"
        else {"cancelled_by_revocation"}
    )
    assert env.service.store.job(job_id)["queue_state"] == "cancelled"
    await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert engine.generated == []
    assert all(not callback.get("artifact_path") for callback in engine.callbacks)
    # 只归还本自动任务预占；其它持久使用计数不会被放大退款。
    assert int(env.service.store.grant(grant_id)["used_count"]) <= 1


# ----------------------------------------------------------------------
# AT-30：自动付费任务没有可核验授权，不能因为共享手动素材已开放就放行。
# ----------------------------------------------------------------------
async def test_auto_job_without_verified_grant_is_refused_before_upload(env):
    await _auto_policy(env)
    owner, ref, _ = env.access("u1", attachments=[env.photo("auto_missing_grant")])
    assert (await env.stage(owner, ref))["success"]
    await _grant(
        env,
        owner,
        ref,
        mode="auto_candidate",
        scope="group:g1",
        expires_at="2999-01-01T00:00:00+00:00",
    )
    engine = FakeEngine(env, owner, env.store())
    submitted = await env.service.auto_candidates(image_generate.build_visual_queue(engine.store))
    job_id = submitted["submitted"][0]["data"]["job_id"]
    manifest = env.service.store.job(job_id)["manifest"]
    assert manifest["auto"] and manifest["grant_id"]
    manifest["grant_id"] = ""
    manifest["subjects"][0]["grant_id"] = ""
    _sql(
        env,
        "UPDATE vs_job SET grant_id='', manifest=? WHERE job_id=?",
        (json.dumps(manifest), job_id),
    )
    preflight = await env.service.preflight_generation(job_id)
    assert preflight["allow"] is False
    assert preflight["code"] == "consent_required"
    await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert engine.generated == []


# ----------------------------------------------------------------------
# AT-41 / AT-45：绝对/相对/~/file:// 同一文件必须得到同一结论
# ----------------------------------------------------------------------
async def test_guard_path_normalises_private_group_paths_and_rejects_orphans(env):
    photo = env.photo("owner")
    owner, owner_ref, _ = env.access("u1", group="g2", name="阿甲", attachments=[photo])
    staged = await env.stage(owner, owner_ref)
    asset_path = env.asset_file(staged["data"]["asset_id"])
    env.access("u1", group="g1", name="阿甲")
    stranger, stranger_ref, _ = env.access("u2", name="阿乙")

    home_rel = os.path.relpath(asset_path, env.service.persona_home)
    variants = [
        str(asset_path),
        asset_path.as_posix(),
        asset_path.as_uri(),
        home_rel,
        f".{os.sep}{home_rel}",
        f"~/{Path(home_rel).as_posix()}",
        f"~/{home_rel}",
    ]
    try:
        cwd_rel = os.path.relpath(asset_path, Path.cwd())
    except ValueError:  # 跨盘符时没有相对路径，跳过这个变体
        cwd_rel = ""
    if cwd_rel:
        variants.append(cwd_rel)
    rel_for_tools = f"~/{home_rel}"
    if os.name == "nt":
        variants.append(str(asset_path).upper())
    for variant in variants:
        guard = await env.service.guard_path(variant, stranger, "vision_read")
        assert guard["managed"] is True, variant
        assert guard["allowed"] is False, variant

    orphan_dir = env.work / "visual_assets" / "orphan"
    orphan_dir.mkdir(parents=True)
    orphan = orphan_dir / "extra.png"
    orphan.write_bytes(_png((1, 2, 3)))
    orphan_guard = await env.service.guard_path(str(orphan), stranger, "vision_read")
    assert orphan_guard["managed"] is True and orphan_guard["allowed"] is False

    engine = FakeEngine(env, stranger, env.store())
    denied_read = await read_image.run(
        paths=[rel_for_tools],
        engine_context=engine,
        invocation_context=env.invocation(stranger, stranger_ref, user="u2"),
        data_store=engine.store,
    )
    assert denied_read["success"] is False and denied_read["code"] == "consent_required"

    denied_generate = await image_generate.run(
        "改一下这张图",
        reference_images=[asset_path.as_uri()],
        data_store=engine.store,
        invocation_context=env.invocation(stranger, stranger_ref, user="u2"),
        engine_context=engine,
    )
    assert denied_generate["success"] is False and denied_generate["code"] == "consent_required"

    class Bridge:
        def __init__(self) -> None:
            class Adapter:
                async def send_group_msg(self, target_id, message):
                    return {"data": {"message_id": 1}}

                async def upload_group_file(self, target_id, path, name):
                    raise AssertionError("拒绝的路径不应真的上传")

            self.adapter = Adapter()

    denied_upload = await group_file_exec.run(
        "file",
        file_path=rel_for_tools,
        bridge=Bridge(),
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        engine_context=engine,
        invocation_context=env.invocation(stranger, stranger_ref, user="u2"),
        data_store=engine.store,
    )
    assert denied_upload["success"] is False
    assert denied_upload["data"]["code"] == "consent_required"


# ----------------------------------------------------------------------
# AUTH-04：管理员可预览/确认删除他人素材；旧路径与副本都不能再读
# ----------------------------------------------------------------------
async def test_admin_deletion_cleans_copies_and_blocks_task_inputs(env):
    photo = env.photo("owner")
    owner, ref, person_id = env.access("u1", attachments=[photo])
    staged = await env.stage(owner, ref)
    asset_path = env.asset_file(staged["data"]["asset_id"])
    engine = FakeEngine(env, owner, env.store())
    submitted = await _submit_person_job(env, owner, ref, engine)
    assert submitted["success"], submitted
    job_id = submitted["data"]["job_id"]
    copy_dir = engine.store.artifact_dir / "references" / job_id
    assert copy_dir.is_dir() and any(copy_dir.iterdir())

    normalized = env.work / "tool_data" / "artifacts" / "read_image" / "normalized"
    normalized.mkdir(parents=True)
    cached = normalized / "cached.png"
    cached.write_bytes(asset_path.read_bytes())

    admin, admin_ref, _ = env.admin()
    other, other_ref, _ = env.access("u2", name="阿乙")
    denied = await env.service.prepare_deletion(
        {"ref": "person_id", "value": person_id}, other, evidence_ref=other_ref
    )
    assert denied["success"] is False and denied["code"] == "permission_denied"

    prepared = await env.service.prepare_deletion(
        {"ref": "person_id", "value": person_id}, admin, evidence_ref=admin_ref
    )
    assert prepared["success"], prepared
    counts = prepared["data"]["preview"]["counts"]
    assert counts["assets"] >= 1 and counts["task_copies"] >= 1

    executed = await env.service.execute_deletion(
        prepared["data"]["confirm_token"], admin, evidence_ref=admin_ref
    )
    assert executed["success"], executed
    cleaned = executed["data"]["cleaned"]
    assert cleaned["assets"] >= 1 and cleaned["task_copies"] >= 1
    assert not asset_path.exists() and not copy_dir.exists() and not cached.exists()

    replay = await env.service.execute_deletion(
        prepared["data"]["confirm_token"], admin, evidence_ref=admin_ref
    )
    assert replay["success"] is False and replay["code"] == "confirm_invalid"

    stranger_engine = FakeEngine(env, other, env.store())
    old_read = await read_image.run(
        paths=[str(asset_path)],
        engine_context=stranger_engine,
        invocation_context=env.invocation(other, other_ref, user="u2"),
        data_store=stranger_engine.store,
    )
    assert old_read["success"] is False
    assert old_read["code"] in {"not_found_or_not_visible", "consent_required"}

    copied_dir = engine.store.artifact_dir / "references" / "img_after_delete"
    copied_dir.mkdir(parents=True)
    copied = copied_dir / "01_copy.png"
    copied.write_bytes(photo.read_bytes())
    copied_read = await read_image.run(
        paths=[str(copied)],
        engine_context=stranger_engine,
        invocation_context=env.invocation(other, other_ref, user="u2"),
        data_store=stranger_engine.store,
    )
    assert copied_read["success"] is False


# ----------------------------------------------------------------------
# AUTO-02 / TOOL-04：自动授权默认固定当前来源修订，future 必须显式
# ----------------------------------------------------------------------
async def test_auto_grant_pins_revision_and_future_is_explicit(env):
    await _auto_policy(env)
    photo = env.photo("v1")
    user, ref, person_id = env.access("u1", attachments=[photo])
    assert (await env.stage(user, ref))["success"]
    granted = await _grant(
        env,
        user,
        ref,
        mode="auto_candidate",
        expires_at="2999-01-01T00:00:00+00:00",
        scope="group:g1",
    )
    row = env.service.store.grant(granted["data"]["grant_id"])
    assert row["revision_scope"] == "selected" and int(row["source_revision"]) == 1
    assert policies.auto_grant_applies(row, 999, "g1") is False

    second_photo = env.photo("v2", color=(30, 200, 30))
    user2, ref2, _ = env.access("u1", attachments=[second_photo])
    second = await env.stage(user2, ref2, description="第二版")
    assert second["success"] and second["data"]["revision"] == 2

    queue = FakeQueue()
    skipped = await env.service.auto_candidates(queue)
    assert skipped["submitted"] == [] and queue.jobs == []
    assert any(item["reason"] == "grant_not_applicable" for item in skipped["skipped"])

    await _grant(
        env,
        user,
        ref,
        mode="auto_candidate",
        revision_scope="future",
        expires_at="2999-01-01T00:00:00+00:00",
        scope="group:g1",
    )
    submitted = await env.service.auto_candidates(queue)
    assert len(submitted["submitted"]) == 1 and len(queue.jobs) == 1
    assert submitted["submitted"][0]["data"]["source_revision"] == 2


# ----------------------------------------------------------------------
# AUTO-04：失败的自动派生只退自己的预占；并发任务不被别人连坐
# ----------------------------------------------------------------------
async def test_failed_derive_refunds_only_its_own_reservation(env):
    await _auto_policy(env, per_person=2)
    user, ref, person_id = env.access("u1", attachments=[env.photo("self")])
    assert (await env.stage(user, ref))["success"]
    persona_id = env.people.persona_id
    persona_bucket = f"auto:persona:{persona_id}"
    day = policies.today_local()

    characters = []
    for index in range(2):
        created = await env.service.manage(
            "create_character",
            access=user,
            evidence_ref=ref,
            target={"ref": "requester"},
            name=f"角色{index}",
        )
        assert created["success"], created
        character_id = created["data"]["person_id"]
        env.people.update_names(character_id, user, ref, preferred_name=f"角色{index}")
        staged = await env.stage(
            user,
            ref,
            target={"ref": "person_id", "value": character_id},
            description=f"角色{index}的外观",
        )
        assert staged["success"], staged
        await _grant(
            env,
            user,
            ref,
            target={"ref": "person_id", "value": character_id},
            mode="auto_candidate",
            expires_at="2999-01-01T00:00:00+00:00",
            scope="group:g1",
        )
        characters.append(character_id)

    queue = FakeQueue()
    submitted = await env.service.auto_candidates(queue, limit=2)
    assert len(submitted["submitted"]) == 2, submitted
    jobs = {item["person_id"]: item["data"]["job_id"] for item in submitted["submitted"]}
    first_id, second_id = characters
    for character_id in characters:
        assert env.service.store.quota_used(f"auto:person:{character_id}", day) == 1
    assert env.service.store.quota_used(persona_bucket, day) == 2

    failed = await env.service.fail_generation(jobs[first_id], "generation_failed")
    assert failed["status"] == "failed"
    assert env.service.store.quota_used(f"auto:person:{first_id}", day) == 0
    assert env.service.store.quota_used(f"auto:person:{second_id}", day) == 1
    assert env.service.store.quota_used(persona_bucket, day) == 1

    replay = await env.service.manage(
        "derive",
        access=user,
        evidence_ref=ref,
        target={"ref": "person_id", "value": first_id},
        queue=FakeQueue(),
        auto=True,
    )
    assert replay["success"] is False and replay["code"] == "already_processed"
    assert env.service.store.quota_used(f"auto:person:{first_id}", day) == 0
    assert env.service.store.quota_used(f"auto:person:{second_id}", day) == 1


# ----------------------------------------------------------------------
# GEN-03：已跨过提交边界的结果未知：不退款、不自动重放
# ----------------------------------------------------------------------
async def test_uncertain_outcome_keeps_budget_and_is_not_replayed(env):
    await _auto_policy(env)
    user, ref, person_id = env.access("u1", attachments=[env.photo("self")])
    assert (await env.stage(user, ref))["success"]
    granted = await _grant(
        env,
        user,
        ref,
        mode="auto_candidate",
        expires_at="2999-01-01T00:00:00+00:00",
        scope="group:g1",
    )
    grant_id = granted["data"]["grant_id"]
    queue = FakeQueue()
    submitted = await env.service.auto_candidates(queue)
    job_id = submitted["submitted"][0]["data"]["job_id"]

    assert (await env.service.preflight_generation(job_id))["allow"] is True
    assert (await env.service.acquire_generation_permit(job_id))["allow"] is True
    assert (await env.service.mark_generation_submit_started(job_id))["allow"] is True
    failed = await env.service.fail_generation(job_id, "upstream_timeout", uncertain=True)
    assert failed["status"] == "outcome_uncertain"

    day = policies.today_local()
    assert env.service.store.quota_used(f"auto:person:{person_id}", day) == 1
    assert int(env.service.store.grant(grant_id)["used_count"]) == 1

    replay = await env.service.manage(
        "derive",
        access=user,
        evidence_ref=ref,
        target={"ref": "requester"},
        queue=FakeQueue(),
        auto=True,
    )
    assert replay["success"] is False and replay["code"] == "outcome_uncertain"
    assert env.service.store.quota_used(f"auto:person:{person_id}", day) == 1
    assert int(env.service.store.grant(grant_id)["used_count"]) == 1


# ----------------------------------------------------------------------
# ASSET-04 / GEN-01：两个主体把必填参考撑爆时必须明确报错，不被吞成参数错误
# ----------------------------------------------------------------------
async def test_two_subject_required_reference_limit_is_not_swallowed(env):
    user, ref, person_id = env.access("u1", attachments=[env.photo("self")])
    staged = await env.stage(user, ref)
    assert staged["success"]
    asset = env.service.store.asset(staged["data"]["asset_id"])
    for idx, view in enumerate(["侧面", "背面", "坐姿"], start=2):
        env.service.store.add_reference_item(
            staged["data"]["reference_set_id"],
            {
                "idx": idx,
                "asset_id": asset["asset_id"],
                "person_id": person_id,
                "revision": 1,
                "purpose": "identity",
                "view": view,
                "required": True,
                "sha256": asset["sha256"],
            },
        )

    created = await env.service.manage(
        "create_character",
        access=user,
        evidence_ref=ref,
        target={"ref": "requester"},
        name="小雀",
    )
    assert created["success"], created
    character_id = created["data"]["person_id"]
    character_photo = env.photo("char", color=(20, 120, 60))
    char_access, char_ref, _ = env.access("u1", attachments=[character_photo])
    staged_char = await env.stage(
        char_access, char_ref, target={"ref": "person_id", "value": character_id}
    )
    assert staged_char["success"], staged_char

    subjects = [
        {"ref": "requester"},
        {"ref": "person_id", "value": character_id},
    ]
    with pytest.raises(VisualError) as excinfo:
        await env.service.compile_generation(access=user, subjects=subjects, prompt="两个人并肩站着")
    assert excinfo.value.code == "reference_limit_exceeded"

    engine = FakeEngine(env, user, env.store())
    result = await image_generate.run(
        "两个人并肩站着",
        subjects=subjects,
        source_ref=ref,
        data_store=engine.store,
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        invocation_context=env.invocation(user, ref),
        engine_context=engine,
    )
    assert result["success"] is False
    assert result["code"] == "reference_limit_exceeded"
    assert engine.generated == [] and image_generate._load_jobs(engine.store) == []


# ----------------------------------------------------------------------
# AT-29：启用冲突要报 revision_conflict；服务缺失要报 service_unavailable
# ----------------------------------------------------------------------
async def test_activation_conflict_and_unavailable_service_are_explicit(env):
    user, ref, _ = env.access("u1", attachments=[env.photo("v1")])
    first = await env.stage(user, ref, description="第一版")
    assert first["success"]
    second_photo = env.photo("v2", color=(30, 30, 200))
    user2, ref2, _ = env.access("u1", attachments=[second_photo])
    second = await env.stage(user2, ref2, description="第二版")
    assert second["success"] and second["data"]["revision"] == 2

    profile_id = second["data"]["profile_id"]
    conflicted = await env.service.manage(
        "activate",
        access=user2,
        evidence_ref=ref2,
        target={"ref": "requester"},
        profile_id=profile_id,
        revision=1,
    )
    assert conflicted["success"] is False and conflicted["code"] == "revision_conflict"
    stale = await env.service.manage(
        "activate",
        access=user2,
        evidence_ref=ref2,
        target={"ref": "requester"},
        profile_id=profile_id,
        revision=1,
        expected_revision=1,
    )
    assert stale["success"] is False and stale["code"] == "revision_conflict"
    assert stale["data"]["current_revision"] == 2

    class NoServiceEngine:
        def get_visual_service(self):
            return None

        def get_person_access(self, invocation_context):
            return None

    unavailable = await manage_tool_run(
        "stage",
        target={"ref": "requester"},
        source={"kind": "attachment", "index": 0},
        evidence_ref=ref2,
        engine_context=NoServiceEngine(),
        invocation_context=env.invocation(user2, ref2),
    )
    assert unavailable["success"] is False and unavailable["code"] == "service_unavailable"


# ----------------------------------------------------------------------
# 授权使用人、删除墓碑与重复启用必须来自实际用户操作，而非信任参数。
# ----------------------------------------------------------------------
async def test_auto_grant_is_bound_to_grantee_while_manual_library_is_shared(env):
    await _auto_policy(env)
    owner, owner_ref, owner_id = env.access("u1", attachments=[env.photo("auto_grantee")])
    assert (await env.stage(owner, owner_ref))["success"]
    permitted, _, permitted_id = env.access("u2", name="阿乙")
    stranger, stranger_ref, _ = env.access("u3", name="阿丙")
    grant = await _grant(
        env,
        owner,
        owner_ref,
        mode="auto_candidate",
        grantee=permitted_id,
        max_uses=2,
        scope="group:g1",
        expires_at="2999-01-01T00:00:00+00:00",
    )
    target = {"ref": "person_id", "value": owner_id}
    denied = await env.service.manage(
        "derive",
        access=stranger,
        evidence_ref=stranger_ref,
        target=target,
        auto=True,
        grant_id=grant["data"]["grant_id"],
        queue=FakeQueue(),
    )
    assert denied["success"] is False and denied["code"] == "permission_denied"
    for viewer in (permitted, stranger):
        plan = await env.service.compile_generation(access=viewer, subjects=[target], prompt="画阿甲")
        assert not plan["manifest"]["subjects"][0]["grant_id"]
    assert env.service.store.grant(grant["data"]["grant_id"])["used_count"] == 0


async def test_stage_requires_source_and_autonomous_human_cannot_generate(env):
    user, ref, _ = env.access("u1", attachments=[env.photo("owner")])
    blocked = await env.service.manage(
        "stage",
        access=user,
        source_ref="not-in-pending",
        target={"ref": "requester"},
        source={"kind": "attachment", "index": 0},
    )
    assert blocked["success"] is False and blocked["code"] == "evidence_invalid"
    await env.stage(user, ref)
    autonomous, _, _ = env.access("u1", self_initiated=True)
    with pytest.raises(VisualError) as excinfo:
        await env.service.compile_generation(
            access=autonomous,
            subjects=[{"ref": "requester"}],
            prompt="自己决定画我",
        )
    assert excinfo.value.code == "permission_denied"


async def test_manual_derive_shares_revisions_without_spending_auto_grant(env):
    await _auto_policy(env)
    owner, ref, owner_id = env.access("u1", attachments=[env.photo("derive_shared_v1")])
    first = await env.stage(owner, ref)
    owner2, ref2, _ = env.access(
        "u1", attachments=[env.photo("derive_shared_v2", color=(20, 180, 230))]
    )
    second = await env.stage(owner2, ref2)
    assert second["data"]["revision"] == 2
    viewer, viewer_ref, _ = env.access("u2", name="阿乙")
    granted = await _grant(
        env,
        owner2,
        ref2,
        mode="auto_candidate",
        max_uses=1,
        source_revision=1,
        scope="group:g1",
        grantee=viewer.actor_id,
        expires_at="2999-01-01T00:00:00+00:00",
    )
    target = {"ref": "person_id", "value": owner_id}
    queue = FakeQueue()
    for revision in (2, 1):
        manual = await env.service.manage(
            "derive",
            access=viewer,
            evidence_ref=viewer_ref,
            target=target,
            profile_id=first["data"]["profile_id"],
            revision=revision,
            queue=queue,
        )
        assert manual["success"], manual
        job = env.service.store.job(manual["data"]["job_id"])
        assert job["source_revision"] == revision and not job["grant_id"]
    assert env.service.store.grant(granted["data"]["grant_id"])["used_count"] == 0
    wrong_auto = await env.service.manage(
        "derive",
        access=viewer,
        evidence_ref=viewer_ref,
        target=target,
        profile_id=first["data"]["profile_id"],
        revision=2,
        auto=True,
        redo=True,
        queue=queue,
    )
    assert wrong_auto["success"] is False and wrong_auto["code"] == "consent_required"
    automatic = await env.service.manage(
        "derive",
        access=viewer,
        evidence_ref=viewer_ref,
        target=target,
        profile_id=first["data"]["profile_id"],
        revision=1,
        auto=True,
        redo=True,
        queue=queue,
    )
    assert automatic["success"], automatic
    assert env.service.store.grant(granted["data"]["grant_id"])["used_count"] == 1
    assert (
        env.service.store.job(automatic["data"]["job_id"])["grant_id"]
        == granted["data"]["grant_id"]
    )
    exhausted = await env.service.manage(
        "derive",
        access=viewer,
        evidence_ref=viewer_ref,
        target=target,
        profile_id=first["data"]["profile_id"],
        revision=1,
        auto=True,
        redo=True,
        queue=queue,
    )
    assert exhausted["success"] is False and exhausted["code"] in {
        "consent_required",
        "quota_exceeded",
    }


async def test_deleted_person_cannot_stage_again_or_reactivate(env):
    user, ref, person_id = env.access("u1", attachments=[env.photo("owner")])
    staged = await env.stage(user, ref)
    prepared = await env.service.prepare_deletion({"ref": "requester"}, user, evidence_ref=ref)
    executed = await env.service.execute_deletion(
        prepared["data"]["confirm_token"], user, evidence_ref=ref
    )
    assert executed["success"], executed
    again = await env.stage(user, ref)
    assert again["success"] is False and again["code"] == "permission_denied"
    activate = await env.service.manage(
        "activate",
        access=user,
        evidence_ref=ref,
        target={"ref": "person_id", "value": person_id},
        profile_id=staged["data"]["profile_id"],
        revision=1,
    )
    assert activate["success"] is False and activate["code"] == "permission_denied"


async def test_candidate_activation_is_explicit_cas_not_stale_idempotent_success(env):
    user, ref, _ = env.access("u1", attachments=[env.photo("owner")])
    source = await env.stage(user, ref, activate=False)
    profile_id = source["data"]["profile_id"]
    first = await env.service.manage(
        "activate",
        access=user,
        evidence_ref=ref,
        target={"ref": "requester"},
        profile_id=profile_id,
        revision=1,
        expected_revision=None,
    )
    assert first["success"] and first["status"] == "active"
    admin, _, _ = env.admin()
    await env.service.set_deployment_policy(admin, total_quota=3)
    derived = await env.service.manage(
        "derive", access=user, evidence_ref=ref, target={"ref": "requester"}, queue=FakeQueue()
    )
    assert derived["success"], derived
    await env.service.complete_generation(
        derived["data"]["job_id"], image_bytes=_png((1, 200, 1)), mime_type=PNG_MIME
    )
    kwargs = {
        "access": user,
        "evidence_ref": ref,
        "target": {"ref": "requester"},
        "profile_id": profile_id,
        "revision": 2,
    }
    stale_expectation = await env.service.manage("activate", **kwargs, expected_revision=None)
    assert stale_expectation["success"] is False
    assert stale_expectation["code"] == "revision_conflict"
    activated = await env.service.manage("activate", **kwargs, expected_revision=1)
    assert activated["success"] and activated["status"] == "active"
    duplicate = await env.service.manage("activate", **kwargs, expected_revision=2)
    assert duplicate["success"] and duplicate["status"] == "already_processed"
    omitted = await env.service.manage("activate", **kwargs)
    assert omitted["success"] is False and omitted["code"] == "revision_conflict"


async def test_activated_generated_candidate_keeps_original_source_retirement_dependency(env):
    photo = env.photo("candidate_ancestral_source", color=(180, 65, 25))
    owner, ref, _ = env.access("u1", attachments=[photo])
    original = await env.stage(owner, ref)
    assert original["success"], original
    admin, _, _ = env.admin()
    assert (await env.service.set_deployment_policy(admin, total_quota=3))["success"]
    engine = FakeEngine(env, owner, env.store())
    common = dict(
        engine_context=engine,
        invocation_context=env.invocation(owner, ref),
        data_store=engine.store,
    )
    derived = await manage_tool_run(
        "derive",
        target={"ref": "requester"},
        evidence_ref=ref,
        **common,
    )
    assert derived["success"], derived
    await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert len(engine.generated) == 1
    assert [data for _, data in engine.generated[0]["references"]] == [photo.read_bytes()]
    source_job = env.service.store.job(derived["data"]["job_id"])
    assert source_job["queue_state"] == "done" and not source_job["isolated"]
    candidate_id = source_job["result_asset_id"]
    candidate = env.service.store.asset(candidate_id)
    assert candidate["source_ref"] == source_job["job_id"]
    activated = await manage_tool_run(
        "activate",
        target={"ref": "requester"},
        evidence_ref=ref,
        profile_id=original["data"]["profile_id"],
        revision=source_job["revision"],
        expected_revision=original["data"]["revision"],
        **common,
    )
    assert activated["success"], activated
    submitted = await image_generate.run(
        "使用刚确认的三视图画新场景",
        subjects=[{"ref": "requester"}],
        source_ref=ref,
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        **common,
    )
    assert submitted["success"], submitted
    job_id = submitted["data"]["job_id"]
    job = env.service.store.job(job_id)
    assert [item["asset_id"] for item in job["manifest"]["references"]] == [candidate_id]
    queued = next(item for item in image_generate._load_jobs(engine.store) if item["id"] == job_id)
    reference_dir = engine.store.artifact_dir / "references" / job_id
    assert [(reference_dir / name).read_bytes() for name in queued["references"]] == [
        engine.image_bytes
    ]
    retired = await manage_tool_run(
        "retire",
        target={"ref": "requester"},
        evidence_ref=ref,
        asset_id=original["data"]["asset_id"],
        **common,
    )
    assert retired["success"], retired
    await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert len(engine.generated) == 1, "启用生成候选不能洗掉最初来源的停用状态"
    assert env.service.store.job(job_id)["queue_state"] == "cancelled"
    queued = next(item for item in image_generate._load_jobs(engine.store) if item["id"] == job_id)
    assert queued["status"] == "cancelled" and not queued.get("image_path")


# ======================================================================
# 独立业务终验：权限不能因旧工具入口、同人物或附加参考而扩大。
# ======================================================================


class _VisualReceiptBridge:
    """只记录 fake 平台真正收到的字节；不连接 NapCat 或网络。"""

    def __init__(self) -> None:
        self.adapter = self
        self.sent: list[dict] = []

    async def send_group_msg(self, target_id: str, message: list[dict]) -> dict:
        reference = message[0]["data"]["file"]
        if reference.startswith("data:image/png;base64,"):
            encoded = reference.split(",", 1)[1]
        else:
            assert reference.startswith("base64://")
            encoded = reference[9:]
        self.sent.append({"kind": "image", "target": target_id, "bytes": base64.b64decode(encoded)})
        return {"data": {"message_id": len(self.sent)}}

    async def upload_group_file(self, target_id: str, path: str, name: str) -> dict:
        self.sent.append({"kind": "file", "target": target_id, "bytes": Path(path).read_bytes()})
        return {"data": {"message_id": len(self.sent)}}


async def _use_final_visual_tool(
    env: Env,
    engine: FakeEngine,
    access: PersonAccess,
    ref: str,
    entry: str,
    asset_id: str,
    bridge: _VisualReceiptBridge,
) -> dict:
    """从真实读图/外发工具进入；不直接调用守卫或代替权限实现。"""
    common = {
        "engine_context": engine,
        "invocation_context": env.invocation(access, ref, user="u2"),
        "data_store": engine.store,
    }
    path = str(env.asset_file(asset_id))
    if entry == "read_assets":
        return await read_image.run(assets=[asset_id], **common)
    if entry == "read_paths":
        return await read_image.run(paths=[path], **common)
    return await group_file_exec.run(
        "image" if entry == "send_image" else "file",
        **({"image_path": path} if entry == "send_image" else {"file_path": path}),
        bridge=bridge,
        chat_context={
            "group_id": access.group_id,
            "chat_type": "group",
            "chat_id": access.group_id,
        },
        **common,
    )


@pytest.mark.parametrize(
    "entry", ["open_asset", "read_assets", "read_paths", "send_image", "send_file"]
)
async def test_final_private_asset_stays_in_source_group_despite_broad_grant(env, entry):
    private = env.photo("private_g2", color=(11, 22, 33))
    owner_g2, ref_g2, owner_id = env.access("u1", group="g2", attachments=[private])
    secret = await env.stage(owner_g2, ref_g2, description="仅限 g2 的私人形象")
    assert secret["success"], secret

    owner_g1, ref_g1, same_owner = env.access(
        "u1", group="g1", attachments=[env.photo("public_g1", color=(120, 130, 140))]
    )
    assert same_owner == owner_id
    env.people.update_names(owner_id, owner_g1, ref_g1, preferred_name="阿甲")
    assert (await env.stage(owner_g1, ref_g1, description="g1 公开版本"))["success"]
    viewer, viewer_ref, _ = env.access("u2", group="g1", name="阿乙")
    grant = await _grant(
        env,
        owner_g1,
        ref_g1,
        capabilities=["view", "vision_read", "external_send"],
        revision_scope="future",
        scope="group:g1",
        grantee=viewer.actor_id,
        max_uses=5,
    )
    grant_id = grant["data"]["grant_id"]
    assert env.service.store.profile(secret["data"]["profile_id"])["scope"] == "group:g2"
    engine = FakeEngine(env, viewer, env.store())
    bridge = _VisualReceiptBridge()
    asset_id = secret["data"]["asset_id"]

    if entry == "open_asset":
        with pytest.raises(VisualError) as excinfo:
            await env.service.open_asset_for_read(asset_id, viewer)
        assert excinfo.value.code in {"consent_required", "not_found_or_not_visible"}
    else:
        result = await _use_final_visual_tool(
            env, engine, viewer, viewer_ref, entry, asset_id, bridge
        )
        assert result["success"] is False, result
        assert (result.get("code") or result.get("data", {}).get("code")) in {
            "consent_required",
            "not_found_or_not_visible",
        }
        assert not result.get("multimodal_blocks")
    assert bridge.sent == []
    assert env.service.store.grant(grant_id)["used_count"] == 0
    assert env.service.store.fetchall("SELECT * FROM vs_delivery") == []


@pytest.mark.parametrize("entry", ["read_assets", "read_paths", "send_image", "send_file"])
async def test_shared_read_and_send_ignore_manual_grant_limits_but_respect_retirement(env, entry):
    photo = env.photo("shared_revision_one")
    owner, ref1, _ = env.access("u1", attachments=[photo])
    first = await env.stage(owner, ref1, description="共享第一版")
    second_photo = env.photo("shared_revision_two", color=(40, 50, 240))
    owner, ref2, _ = env.access("u1", attachments=[second_photo])
    second = await env.stage(owner, ref2, description="共享第二版")
    assert first["success"] and second["success"]
    assert first["data"]["profile_id"] == second["data"]["profile_id"]
    viewer, viewer_ref, _ = env.access("u2", name="阿乙")
    capability = "vision_read" if entry.startswith("read_") else "external_send"
    grant = await _grant(
        env,
        owner,
        ref2,
        capabilities=[capability],
        max_uses=1,
        source_revision=1,
        scope="group:g1",
        grantee=viewer.actor_id,
    )
    grant_id = grant["data"]["grant_id"]
    engine = FakeEngine(env, viewer, env.store())
    bridge = _VisualReceiptBridge()
    before = await env.service.guard_path(
        env.asset_file(first["data"]["asset_id"]), viewer, capability
    )
    assert before["allowed"] and env.service.store.grant(grant_id)["used_count"] == 0
    for staged, expected in (
        (first, photo.read_bytes()),
        (second, second_photo.read_bytes()),
        (second, second_photo.read_bytes()),
    ):
        result = await _use_final_visual_tool(
            env, engine, viewer, viewer_ref, entry, staged["data"]["asset_id"], bridge
        )
        assert result["success"], result
        received = (
            Path(result["multimodal_blocks"][0]["value"]).read_bytes()
            if entry.startswith("read_")
            else bridge.sent[-1]["bytes"]
        )
        assert received == expected
    assert env.service.store.grant(grant_id)["used_count"] == 0
    retired = await env.service.manage(
        "retire",
        access=owner,
        evidence_ref=ref2,
        target={"ref": "requester"},
        profile_id=second["data"]["profile_id"],
        revision=2,
    )
    assert retired["success"]
    denied = await _use_final_visual_tool(
        env, engine, viewer, viewer_ref, entry, second["data"]["asset_id"], bridge
    )
    assert denied["success"] is False and not denied.get("multimodal_blocks")
    if entry.startswith("send_"):
        assert len(bridge.sent) == 3
        receipts = env.service.store.fetchall("SELECT * FROM vs_delivery")
        assert len(receipts) == 3 and all(row["success"] for row in receipts)
        assert all(row["actor_id"] == viewer.actor_id for row in receipts)


async def test_shared_legacy_reference_keeps_binding_without_spending_manual_grant(env):
    photo = env.photo("legacy_reference")
    owner, owner_ref, owner_id = env.access("u1", attachments=[photo])
    staged = await env.stage(owner, owner_ref)
    assert staged["success"]
    grantee, ref, _ = env.access("u2", name="阿乙")
    granted = await _grant(
        env,
        owner,
        owner_ref,
        capabilities=["upload_generate"],
        max_uses=1,
        scope="group:g1",
        grantee=grantee.actor_id,
    )
    grant_id = granted["data"]["grant_id"]
    engine = FakeEngine(env, grantee, env.store())
    params = {
        "reference_images": [str(env.asset_file(staged["data"]["asset_id"]))],
        "data_store": engine.store,
        "chat_context": {"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        "invocation_context": env.invocation(grantee, ref, user="u2"),
        "engine_context": engine,
    }
    submitted = await image_generate.run("保留原图配色，改为水彩风格", **params)
    assert submitted["success"], submitted
    job_id = submitted["data"]["job_id"]
    bound = env.service.store.job(job_id)
    assert bound is not None, "旧裸路径入口也必须持久化授权绑定，不能只保存 JSON 路径"
    assert bound["actor_id"] == grantee.actor_id and bound["group_id"] == "g1"
    assert bound["input_hash"]
    assert grant_id not in json.dumps(bound["manifest"], ensure_ascii=False)
    assert not bound["grant_id"]
    refs = bound["manifest"]["references"]
    assert any(
        item["asset_id"] == staged["data"]["asset_id"]
        and item["person_id"] == owner_id
        and item["profile_id"] == staged["data"]["profile_id"]
        and item["revision"] == 1
        for item in refs
    )
    assert env.service.store.grant(grant_id)["used_count"] == 0
    revoked = await env.service.manage(
        "revoke_consent",
        access=owner,
        evidence_ref=owner_ref,
        target={"ref": "requester"},
        grant_id=grant_id,
    )
    assert revoked["success"] and job_id not in revoked["data"]["cancelled_jobs"]
    repeated = await image_generate.run("再把同一张图改成油画", **params)
    assert repeated["success"], repeated
    assert len(image_generate._load_jobs(engine.store)) == 2
    for _ in range(2):
        await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert len(engine.generated) == 2
    assert all(
        [data for _, data in request["references"]] == [photo.read_bytes()]
        for request in engine.generated
    )
    assert engine.generated[0]["prompt"] == "保留原图配色，改为水彩风格"
    assert env.service.store.grant(grant_id)["used_count"] == 0
    assert env.service.store.job(job_id)["queue_state"] == "done"


async def test_shared_legacy_reference_retirement_before_tick_blocks_upstream(env):
    owner, owner_ref, _ = env.access("u1", attachments=[env.photo("legacy_revoke")])
    staged = await env.stage(owner, owner_ref)
    assert staged["success"]
    grantee, ref, _ = env.access("u2", name="阿乙")
    granted = await _grant(
        env,
        owner,
        owner_ref,
        capabilities=["upload_generate"],
        max_uses=1,
        scope="group:g1",
        grantee=grantee.actor_id,
    )
    engine = FakeEngine(env, grantee, env.store())
    submitted = await image_generate.run(
        "以这张图为参考改成夜景",
        reference_images=[str(env.asset_file(staged["data"]["asset_id"]))],
        data_store=engine.store,
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        invocation_context=env.invocation(grantee, ref, user="u2"),
        engine_context=engine,
    )
    assert submitted["success"], submitted
    job_id = submitted["data"]["job_id"]
    revoked = await env.service.manage(
        "retire",
        access=owner,
        evidence_ref=owner_ref,
        target={"ref": "requester"},
        profile_id=staged["data"]["profile_id"],
        revision=staged["data"]["revision"],
    )
    assert revoked["success"], revoked
    await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert engine.generated == [], "无 subjects 的旧入口也不得在停用后向上游传图"
    assert all(not callback.get("artifact_path") for callback in engine.callbacks)
    queued = next(job for job in image_generate._load_jobs(engine.store) if job["id"] == job_id)
    assert queued["status"] == "cancelled"
    bound = env.service.store.job(job_id)
    assert bound is not None and bound["queue_state"] == "cancelled"
    assert bound["external_boundary"] == "not_submitted"
    assert env.service.store.grant(granted["data"]["grant_id"])["used_count"] == 0


async def test_shared_extra_reference_keeps_source_and_retirement_dependency(env):
    owner, owner_ref, owner_id = env.access(
        "u1", attachments=[env.photo("extra_owner", color=(200, 90, 30))]
    )
    extra = await env.stage(owner, owner_ref, description="只作为场景参考的另一人的图")
    actor, ref, actor_id = env.access("u2", name="阿乙", attachments=[env.photo("actual_subject")])
    actual = await env.stage(actor, ref)
    assert extra["success"] and actual["success"]
    grant = await _grant(
        env,
        owner,
        owner_ref,
        capabilities=["upload_generate"],
        max_uses=2,
        scope="group:g1",
        grantee=actor_id,
    )
    engine = FakeEngine(env, actor, env.store())
    submitted = await image_generate.run(
        "画我，另图只借用配色",
        subjects=[{"ref": "requester"}],
        source_ref=ref,
        reference_images=[str(env.asset_file(extra["data"]["asset_id"]))],
        data_store=engine.store,
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        invocation_context=env.invocation(actor, ref, user="u2"),
        engine_context=engine,
    )
    assert submitted["success"], submitted
    job_id = submitted["data"]["job_id"]
    bound = env.service.store.job(job_id)
    manifest = bound["manifest"]
    assert [item["person_id"] for item in manifest["subjects"]] == [actor_id]
    reference = next(
        item for item in manifest["references"] if item["asset_id"] == extra["data"]["asset_id"]
    )
    assert reference["person_id"] == owner_id
    assert reference["profile_id"] == extra["data"]["profile_id"] and reference["revision"] == 1
    assert grant["data"]["grant_id"] not in json.dumps(manifest, ensure_ascii=False)
    assert env.service.store.grant(grant["data"]["grant_id"])["used_count"] == 0

    revoked = await env.service.manage(
        "retire",
        access=owner,
        evidence_ref=owner_ref,
        target={"ref": "requester"},
        profile_id=extra["data"]["profile_id"],
        revision=extra["data"]["revision"],
    )
    assert revoked["success"], revoked
    await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert engine.generated == []
    assert env.service.store.job(job_id)["queue_state"] == "cancelled"
    queued = next(job for job in image_generate._load_jobs(engine.store) if job["id"] == job_id)
    assert queued["status"] == "cancelled"
    assert all(not callback.get("artifact_path") for callback in engine.callbacks)


async def test_final_empty_subject_scenery_with_multiple_pending_authors_avoids_people(
    env, monkeypatch
):
    _, admin_ref, _ = env.access("admin", admin=True, name="管理员")
    actor, ref, _ = env.access("u2", name="阿乙")
    invocation = ToolInvocationContext(
        caller=UnifiedUser(user_id="admin", name="管理员", metadata={"is_developer": True}),
        group_id="g1",
        evidence_refs=(admin_ref, ref),
        evidence_callers=(
            (admin_ref, UnifiedUser(user_id="admin", name="管理员", metadata={"is_developer": True})),
            (ref, UnifiedUser(user_id="u2", name="阿乙")),
        ),
    )
    engine = FakeEngine(env, actor, env.store())
    scene = env.photo("ordinary_scenery", color=(0, 150, 210))
    prompt = "画一片没有人的海边，保留照片里的蓝色天空"

    async def unexpected_person_compilation(**kwargs):
        raise AssertionError("空 subjects 的普通风景不得进入人物编译")

    monkeypatch.setattr(env.service, "compile_generation", unexpected_person_compilation)
    submitted = await image_generate.run(
        prompt,
        subjects=[],
        reference_images=[str(scene)],
        data_store=engine.store,
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        invocation_context=invocation,
        engine_context=engine,
    )
    assert submitted["success"], submitted
    job_id = submitted["data"]["job_id"]
    assert env.service.store.job(job_id) is None
    assert env.service.store.fetchall("SELECT * FROM vs_profile") == []
    assert env.service.store.fetchall("SELECT * FROM vs_grant") == []
    await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert len(engine.generated) == 1
    assert engine.generated[0]["prompt"] == prompt
    assert [data for _, data in engine.generated[0]["references"]] == [scene.read_bytes()]
    queued = next(job for job in image_generate._load_jobs(engine.store) if job["id"] == job_id)
    assert queued["status"] == "done" and not queued.get("visual")
    assert len(engine.callbacks) == 1 and engine.callbacks[0]["artifact_path"]


@pytest.mark.parametrize("source_group", ["g1", "g2"])
async def test_shared_asset_can_bind_another_subject_without_widening_private_scope(
    env, source_group
):
    photo = env.photo("reusable_original")
    owner, owner_ref, owner_id = env.access("u1", group=source_group, attachments=[photo])
    staged = await env.stage(owner, owner_ref)
    source_id = staged["data"]["asset_id"]
    original = env.service.store.asset(source_id)
    if source_group != "g1":
        env.access("u1", group="g1", name="阿甲")
    actor, ref, actor_id = env.access("u2", name="阿乙")
    engine = FakeEngine(env, actor, env.store())
    params = dict(
        target={"ref": "requester"},
        source={"kind": "asset", "asset_id": source_id},
        activate=True,
        identity_description="复用可见共享图",
        evidence_ref=ref,
        engine_context=engine,
        invocation_context=env.invocation(actor, ref, user="u2"),
        data_store=engine.store,
    )
    imported = await manage_tool_run("stage", **params)
    if source_group == "g2":
        assert imported["success"] is False, imported
        assert imported["code"] in {"consent_required", "not_found_or_not_visible"}
        assert env.service.store.profiles_for(actor_id) == []
        assert env.service.store.assets_for(actor_id) == []
        assert not (env.work / "visual_assets" / actor_id).exists()
    else:
        assert imported["success"], imported
        asset = env.service.store.asset(imported["data"]["asset_id"])
        assert asset["person_id"] == actor_id != owner_id
        assert asset["source_ref"] == ref
        assert imported["data"]["source"]["asset_id"] == source_id
        assert asset["parent_asset_id"] == source_id
        assert env.asset_file(asset["asset_id"]).read_bytes() == photo.read_bytes()
        created = await manage_tool_run(
            "create_character",
            name="复用子图的角色",
            evidence_ref=ref,
            engine_context=engine,
            invocation_context=env.invocation(actor, ref, user="u2"),
            data_store=engine.store,
        )
        assert created["success"], created
        grandchild = await manage_tool_run(
            "stage",
            target={"ref": "person_id", "value": created["data"]["person_id"]},
            source={"kind": "asset", "asset_id": asset["asset_id"]},
            activate=True,
            evidence_ref=ref,
            engine_context=engine,
            invocation_context=env.invocation(actor, ref, user="u2"),
            data_store=engine.store,
        )
        assert grandchild["success"], grandchild
        grandchild_id = grandchild["data"]["asset_id"]
        assert env.service.store.asset(grandchild_id)["parent_asset_id"] == asset["asset_id"]
        lineage_ids = [source_id, asset["asset_id"], grandchild_id]
        assert len(set(lineage_ids)) == 3
        lineage_paths = [env.asset_file(item) for item in lineage_ids]
        assert len(set(lineage_paths)) == 3 and all(path.is_file() for path in lineage_paths)
        independent_photo = env.photo("actor_independent", color=(15, 150, 220))
        independent_actor, independent_ref, _ = env.access("u2", attachments=[independent_photo])
        independent = await env.stage(independent_actor, independent_ref)
        assert independent["success"], independent
        independent_path = env.asset_file(independent["data"]["asset_id"])
        submitted = await image_generate.run(
            "以复用子图为参考画场景",
            subjects=[],
            source_ref=ref,
            reference_images=[str(env.asset_file(asset["asset_id"]))],
            chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
            engine_context=engine,
            invocation_context=env.invocation(actor, ref, user="u2"),
            data_store=engine.store,
        )
        assert submitted["success"], submitted
        job_id = submitted["data"]["job_id"]
        reference_dir = engine.store.artifact_dir / "references" / job_id
        copies = list(reference_dir.iterdir())
        assert len(copies) == 1 and copies[0].read_bytes() == photo.read_bytes()
        retired = await env.service.manage(
            "retire",
            access=owner,
            evidence_ref=owner_ref,
            target={"ref": "requester"},
            asset_id=source_id,
        )
        assert retired["success"]
        blocked = await manage_tool_run("stage", **params)
        assert blocked["success"] is False
        assert env.service.store.asset(source_id)["status"] != "available"
        reread = await read_image.run(
            assets=[asset["asset_id"]],
            engine_context=engine,
            invocation_context=env.invocation(actor, ref, user="u2"),
            data_store=engine.store,
        )
        assert reread["success"] is False and not reread.get("multimodal_blocks")
        queue_before = engine.store.store_path.read_bytes()
        prepared = await env.service.prepare_deletion(
            {"ref": "requester"},
            owner,
            evidence_ref=owner_ref,
        )
        assert prepared["success"], prepared
        counts = prepared["data"]["preview"]["counts"]
        assert counts["assets"] == len(lineage_ids), counts
        assert counts["jobs"] == 1 and counts["task_copies"] == len(copies)
        assert all(path.is_file() for path in [*lineage_paths, *copies, independent_path])
        assert engine.store.store_path.read_bytes() == queue_before
        deleted = await env.service.execute_deletion(
            prepared["data"]["confirm_token"],
            owner,
            evidence_ref=owner_ref,
        )
        assert deleted["success"] and deleted["data"]["generation_blocked"], deleted
        assert deleted["data"]["cleaned"]["assets"] == len(lineage_ids)
        assert deleted["data"]["cleaned"]["task_copies"] == len(copies)
        assert deleted["data"]["cleaned"]["queue_snapshots_scrubbed"] == 1
        assert all(not path.exists() for path in [*lineage_paths, *copies])
        assert not reference_dir.exists()
        assert all(env.service.store.asset(item)["status"] == "deleted" for item in lineage_ids)
        assert env.service.store.subject_deleted(owner_id)
        assert not env.service.store.subject_deleted(actor_id)
        assert not env.service.store.subject_deleted(created["data"]["person_id"])
        assert env.service.store.asset(independent["data"]["asset_id"])["status"] == "available"
        assert independent_path.read_bytes() == independent_photo.read_bytes()
        disk_store = ToolDataStore(engine.store.store_path)
        queued = next(
            item for item in image_generate._load_jobs(disk_store) if item["id"] == job_id
        )
        assert queued["status"] == "cancelled" and queued["deleted"] is True
        assert queued["callback_state"] == "skipped"
        assert not queued.get("prompt") and not queued.get("references")
        bound = env.service.store.job(job_id)
        assert bound["queue_state"] == "cancelled" and bound["isolated"]
        assert bound["manifest"]["deleted"] and not bound["prompt"]
        await image_generate._process_once(engine, engine.store, recover_interrupted=False)
        assert engine.generated == []
        preserved = await read_image.run(
            assets=[independent["data"]["asset_id"]],
            engine_context=engine,
            data_store=engine.store,
            invocation_context=env.invocation(independent_actor, independent_ref, user="u2"),
        )
        assert preserved["success"], preserved
        assert (
            Path(preserved["multimodal_blocks"][0]["value"]).read_bytes()
            == independent_photo.read_bytes()
        )
    assert env.service.store.asset(source_id)["source_ref"] == original["source_ref"]
    assert env.service.store.grants_for(owner_id) == []


@pytest.mark.parametrize("branch", ["person", "legacy_managed"])
async def test_final_reference_snapshot_tampering_never_reaches_upstream(env, branch):
    photo = env.photo("snapshot_original")
    actor, ref, _ = env.access("u1", attachments=[photo])
    staged = await env.stage(actor, ref)
    assert staged["success"]
    engine = FakeEngine(env, actor, env.store())
    person_fields = {"subjects": [{"ref": "requester"}], "source_ref": ref}
    if branch == "legacy_managed":
        person_fields = {"reference_images": [str(env.asset_file(staged["data"]["asset_id"]))]}
    submitted = await image_generate.run(
        "改成冬天的配色",
        **person_fields,
        data_store=engine.store,
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        invocation_context=env.invocation(actor, ref),
        engine_context=engine,
    )
    assert submitted["success"], submitted
    job_id = submitted["data"]["job_id"]
    before = env.service.store.job(job_id)
    assert before is not None and before["input_hash"]
    queued = next(job for job in image_generate._load_jobs(engine.store) if job["id"] == job_id)
    snapshot = engine.store.artifact_dir / "references" / job_id / queued["references"][0]
    assert snapshot.read_bytes() == photo.read_bytes()
    snapshot.write_bytes(_png((240, 240, 0)))  # 文件还在且可解码，但已不是入队时的图。
    assert env.asset_file(staged["data"]["asset_id"]).read_bytes() == photo.read_bytes()

    await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert engine.generated == []
    assert all(not callback.get("artifact_path") for callback in engine.callbacks)
    queued = next(job for job in image_generate._load_jobs(engine.store) if job["id"] == job_id)
    assert queued["status"] == "failed" and "哈希" in queued["error"]
    after = env.service.store.job(job_id)
    assert after["queue_state"] == "failed"
    assert after["external_boundary"] == "not_submitted"
    assert after["input_hash"] == before["input_hash"]
    assert not after.get("result_asset_id")


@pytest.mark.parametrize("role", ["second_subject", "extra_reference"])
@pytest.mark.parametrize("phase", ["queued", "completed"])
async def test_final_deleting_nonprimary_person_blocks_copies_and_generated_result(
    env, role, phase
):
    actor, ref, actor_id = env.access("u1", attachments=[env.photo("primary_subject")])
    primary = await env.stage(actor, ref)
    owner, owner_ref, owner_id = env.access(
        "u2", name="阿乙", attachments=[env.photo("secondary_subject", color=(50, 180, 70))]
    )
    secondary = await env.stage(owner, owner_ref)
    assert primary["success"] and secondary["success"]
    await _grant(
        env,
        owner,
        owner_ref,
        capabilities=["upload_generate", "vision_read", "external_send"],
        grantee=actor_id,
        scope="group:g1",
        max_uses=6,
    )
    subjects = [{"ref": "requester"}]
    references = []
    if role == "second_subject":
        subjects.append({"ref": "person_id", "value": owner_id})
    else:
        references.append(str(env.asset_file(secondary["data"]["asset_id"])))
    engine = FakeEngine(env, actor, env.store())
    submitted = await image_generate.run(
        "画我和另一张参考的配色",
        subjects=subjects,
        source_ref=ref,
        reference_images=references,
        data_store=engine.store,
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        invocation_context=env.invocation(actor, ref),
        engine_context=engine,
    )
    assert submitted["success"], submitted
    job_id = submitted["data"]["job_id"]
    bound = env.service.store.job(job_id)
    assert bound["person_id"] == actor_id != owner_id  # 删除对象不是任务首主体。
    reference_dir = engine.store.artifact_dir / "references" / job_id
    copies = list(reference_dir.iterdir())
    assert len(copies) == 2
    product_path = None
    result_asset_id = ""
    if phase == "completed":
        await image_generate._process_once(engine, engine.store, recover_interrupted=False)
        assert len(engine.generated) == 1
        queued = next(job for job in image_generate._load_jobs(engine.store) if job["id"] == job_id)
        assert queued["status"] == "done"
        product_path = Path(queued["image_path"])
        assert product_path.is_file()
        result_asset_id = env.service.store.job(job_id)["result_asset_id"]
        assert result_asset_id
    preview = await env.service.prepare_deletion(
        {"ref": "requester"}, owner, evidence_ref=owner_ref
    )
    assert preview["success"], preview
    assert preview["data"]["preview"]["counts"]["jobs"] == 1
    assert preview["data"]["preview"]["counts"]["task_copies"] == 2
    assert all(copy.is_file() for copy in copies)  # 仅预览不会删除。
    deleted = await env.service.execute_deletion(
        preview["data"]["confirm_token"], owner, evidence_ref=owner_ref
    )
    assert deleted["success"] and deleted["data"]["generation_blocked"]
    assert deleted["data"]["cleaned"]["task_copies"] == 2
    assert deleted["data"]["unresolved"]  # 不谎称能撤回平台/外部服务副本。
    assert not reference_dir.exists()
    assert env.asset_file(primary["data"]["asset_id"]).is_file()
    after = env.service.store.job(job_id)
    assert after["isolated"] == 1
    assert after["queue_state"] == ("cancelled" if phase == "queued" else "isolated")
    if phase == "queued":
        await image_generate._process_once(engine, engine.store, recover_interrupted=False)
        assert engine.generated == []
        assert all(not callback.get("artifact_path") for callback in engine.callbacks)
    else:
        assert not product_path.exists()
        blocked_asset = await read_image.run(
            assets=[result_asset_id],
            engine_context=engine,
            invocation_context=env.invocation(actor, ref),
            data_store=engine.store,
        )
        assert blocked_asset["success"] is False and not blocked_asset.get("multimodal_blocks")
    bridge = _VisualReceiptBridge()
    for forbidden_path in [*copies, *([product_path] if product_path else [])]:
        result = await group_file_exec.run(
            "image",
            image_path=str(forbidden_path),
            bridge=bridge,
            chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
            engine_context=engine,
            invocation_context=env.invocation(actor, ref),
            data_store=engine.store,
        )
        assert result["success"] is False, result
    assert bridge.sent == []
    assert env.service.store.fetchall("SELECT * FROM vs_delivery") == []


@pytest.mark.parametrize("entry", ["get_summary", "person_query"])
async def test_shared_queries_reveal_new_visible_revision_without_grant_consumption(env, entry):
    owner, ref1, owner_id = env.access("u1", attachments=[env.photo("view_selected_v1")])
    first = await env.stage(owner, ref1, description="已授权查看的第一版形象")
    assert first["success"]
    viewer, viewer_ref, _ = env.access("u2", name="阿乙")
    grant = await _grant(
        env,
        owner,
        ref1,
        capabilities=["view"],
        source_revision=1,
        revision_scope="selected",
        grantee=viewer.actor_id,
        max_uses=3,
        scope="group:g1",
    )
    engine = FakeEngine(env, viewer, env.store())

    async def query_visual():
        if entry == "get_summary":
            result = await env.service.get_summary(owner_id, viewer, detail="detail")
            assert result["status"] == "found", result
            return result["visual"]
        result = await person_query.run(
            {"ref": "person_id", "value": owner_id},
            sections=["visual"],
            detail="detail",
            engine_context=engine,
            invocation_context=env.invocation(viewer, viewer_ref, user="u2"),
        )
        assert result["status"] == "found", result
        return result["sections"]["visual"]

    initial = json.dumps(await query_visual(), ensure_ascii=False)
    assert "已授权查看的第一版形象" in initial
    assert first["data"]["asset_id"] in initial
    owner2, ref2, _ = env.access(
        "u1", attachments=[env.photo("view_private_v2", color=(21, 180, 42))]
    )
    second = await env.stage(owner2, ref2, description="绝密的第二版新身份特征")
    assert second["success"] and second["data"]["revision"] == 2
    visual = await query_visual()
    blob = json.dumps(visual, ensure_ascii=False)
    assert "绝密的第二版新身份特征" in blob
    assert second["data"]["asset_id"] in blob
    assert second["data"]["reference_set_id"] in blob
    assert [item["revision"] for item in visual["active"]] == [2]
    assert env.service.store.grant(grant["data"]["grant_id"])["used_count"] == 0
    assert env.service.store.profile(first["data"]["profile_id"])["active_revision"] == 2


@pytest.mark.parametrize("representation", ["base64", "data_uri"])
@pytest.mark.parametrize("source_group", ["g1", "g2"])
async def test_shared_inline_image_sends_without_grant_but_private_group_stays_private(
    env, representation, source_group
):
    owner, owner_ref, owner_id = env.access(
        "u1", group=source_group, attachments=[env.photo("inline_original")]
    )
    staged = await env.stage(owner, owner_ref)
    assert staged["success"]
    copied = env.chat_dir / "original_cache_copy.png"
    copied.write_bytes(env.asset_file(staged["data"]["asset_id"]).read_bytes())
    if source_group == "g2":
        env.access("u1", group="g1", name="阿甲")
    viewer, viewer_ref, _ = env.access("u2", name="阿乙")
    prefix = "base64://" if representation == "base64" else "data:image/png;base64,"
    bridge = _VisualReceiptBridge()
    engine = FakeEngine(env, viewer, env.store())
    params = dict(
        image_path=prefix + base64.b64encode(copied.read_bytes()).decode("ascii"),
        bridge=bridge,
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        engine_context=engine,
        invocation_context=env.invocation(viewer, viewer_ref, user="u2"),
        data_store=engine.store,
    )
    for _ in range(2):
        result = await group_file_exec.run("image", **params)
        assert result["success"] is (source_group == "g1"), result
    if source_group == "g1":
        assert [item["bytes"] for item in bridge.sent] == [copied.read_bytes()] * 2
        receipts = env.service.store.fetchall("SELECT * FROM vs_delivery")
        assert len(receipts) == 2 and all(row["success"] for row in receipts)
    else:
        assert result["data"]["code"] in {"consent_required", "not_found_or_not_visible"}
        assert bridge.sent == []
        assert env.service.store.fetchall("SELECT * FROM vs_delivery") == []
    assert env.service.store.grants_for(owner_id) == []


async def test_final_legacy_snapshot_becoming_managed_is_checked_on_every_tick(env):
    owner_photo = env.photo("late_managed_owner", color=(190, 70, 30))
    owner, owner_ref, _ = env.access("u1", attachments=[owner_photo])
    staged = await env.stage(owner, owner_ref)
    assert staged["success"]
    actor, ref, _ = env.access("u2", name="阿乙")
    scenery = env.photo("initially_plain_scenery", color=(20, 150, 230))
    engine = FakeEngine(env, actor, env.store())
    submitted = await image_generate.run(
        "画一片没有人的海滩",
        reference_images=[str(scenery)],
        data_store=engine.store,
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        invocation_context=env.invocation(actor, ref, user="u2"),
        engine_context=engine,
    )
    assert submitted["success"], submitted
    job_id = submitted["data"]["job_id"]
    assert env.service.store.job(job_id) is None
    queued = next(job for job in image_generate._load_jobs(engine.store) if job["id"] == job_id)
    assert not queued.get("visual") and queued["status"] == "pending"
    snapshot = engine.store.artifact_dir / "references" / job_id / queued["references"][0]
    assert snapshot.read_bytes() == scenery.read_bytes()
    snapshot.write_bytes(env.asset_file(staged["data"]["asset_id"]).read_bytes())

    # 同进程的普通 tick，不借助启动恢复扫描，也不能将新出现的受管字节外传。
    await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert engine.generated == []
    queued = next(job for job in image_generate._load_jobs(engine.store) if job["id"] == job_id)
    assert queued["status"] == "cancelled"
    assert queued["error_code"] == "legacy_managed_unverifiable"
    assert queued["isolated"] and queued["artifact_suppressed"]
    assert all(not callback.get("artifact_path") for callback in engine.callbacks)
    assert env.service.store.job(job_id) is None


@pytest.mark.parametrize(
    "entry",
    ["url_read", "url_send_no_cache", "url_send_cache_failure", "external_copy_read", "reupload"],
)
async def test_shared_content_transfer_preserves_bytes_source_and_retirement(
    env, monkeypatch, entry
):
    original = env.photo("content_bound_original", color=(191, 81, 37))
    owner, owner_ref, owner_id = env.access("u1", attachments=[original])
    staged = await env.stage(owner, owner_ref, description="Alice 的共享原图")
    assert staged["success"], staged
    original_asset = env.service.store.asset(staged["data"]["asset_id"])
    outside = env.root / "outside_persona"
    outside.mkdir()
    copied = outside / "arbitrary_copy.png"
    copied.write_bytes(original.read_bytes())
    actor, ref, actor_id = env.access("u2", name="Bob", attachments=[copied])
    engine = FakeEngine(env, actor, env.store())
    bridge = _VisualReceiptBridge()
    downloaded = []
    cache_attempts = []
    url = "https://visual-qa.invalid/reuploaded-picture.png"

    async def fake_download(source: str) -> bytes:
        assert source == url
        downloaded.append(source)
        return original.read_bytes()

    monkeypatch.setattr(read_image, "_download_image", fake_download)
    if entry == "url_send_cache_failure":

        async def failed_cache(source: str) -> str:
            cache_attempts.append(source)
            raise RuntimeError("fake adapter cache unavailable")

        monkeypatch.setattr(bridge, "cache_image", failed_cache, raising=False)
    common = dict(
        engine_context=engine,
        invocation_context=env.invocation(actor, ref, user="u2"),
        data_store=engine.store,
    )

    async def transfer():
        if entry == "reupload":
            return await manage_tool_run(
                "stage",
                target={"ref": "requester"},
                source={"kind": "attachment"},
                evidence_ref=ref,
                identity_description="Bob 使用共享图",
                activate=True,
                **common,
            )
        if entry.startswith("url_send"):
            return await group_file_exec.run(
                "image",
                image_path=url,
                bridge=bridge,
                chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
                **common,
            )
        return await read_image.run(paths=[url if entry == "url_read" else str(copied)], **common)

    result = await transfer()
    assert result["success"], result
    if entry == "reupload":
        imported = env.service.store.asset(result["data"]["asset_id"])
        assert imported["person_id"] == imported["uploader_id"] == actor_id
        assert imported["source_ref"] == ref
        assert env.asset_file(imported["asset_id"]).read_bytes() == original.read_bytes()
    elif entry.startswith("url_send"):
        assert [item["bytes"] for item in bridge.sent] == [original.read_bytes()]
        assert len(env.service.store.fetchall("SELECT * FROM vs_delivery")) == 1
    else:
        assert Path(result["multimodal_blocks"][0]["value"]).read_bytes() == original.read_bytes()
    assert env.service.store.asset(staged["data"]["asset_id"]) == original_asset
    assert env.asset_file(staged["data"]["asset_id"]).read_bytes() == original.read_bytes()
    retired = await env.service.manage(
        "retire",
        access=owner,
        evidence_ref=owner_ref,
        target={"ref": "requester"},
        asset_id=staged["data"]["asset_id"],
    )
    assert retired["success"]
    blocked = await transfer()
    assert blocked["success"] is False and not blocked.get("multimodal_blocks")
    assert downloaded == ([url, url] if entry.startswith("url_") else [])
    assert cache_attempts == ([url, url] if entry == "url_send_cache_failure" else [])
    assert len(bridge.sent) == (1 if entry.startswith("url_send") else 0)
    assert env.service.store.grants_for(owner_id) == []


async def test_final_same_output_bytes_keep_each_generation_source_independent(env):
    photo = env.photo("same_output_source", color=(140, 40, 170))
    actor, ref, _ = env.access("u1", attachments=[photo])
    staged = await env.stage(actor, ref)
    assert staged["success"], staged
    original_id = staged["data"]["asset_id"]
    original = env.service.store.asset(original_id)
    engine = FakeEngine(env, actor, env.store())
    engine.image_bytes = _png((30, 190, 210))
    completed = []
    assets = []
    for prompt in ["画我在晴天的花园", "画我在雨后的街道"]:
        submitted = await image_generate.run(
            prompt,
            subjects=[{"ref": "requester"}],
            source_ref=ref,
            data_store=engine.store,
            chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
            invocation_context=env.invocation(actor, ref),
            engine_context=engine,
        )
        assert submitted["success"], submitted
        job_id = submitted["data"]["job_id"]
        await image_generate._process_once(engine, engine.store, recover_interrupted=False)
        job = env.service.store.job(job_id)
        assert job["queue_state"] == "done" and not job["isolated"]
        asset = env.service.store.asset(job["result_asset_id"])
        assert asset["origin"] == "generated" and asset["source_ref"] == job_id
        assert env.asset_file(asset["asset_id"]).read_bytes() == engine.image_bytes
        completed.append(job)
        assets.append(asset)

    assert len(engine.generated) == 2
    assert engine.generated[0]["prompt"] != engine.generated[1]["prompt"]
    assert all(
        [data for _, data in request["references"]] == [photo.read_bytes()]
        for request in engine.generated
    )
    assert completed[0]["job_id"] != completed[1]["job_id"]
    assert completed[0]["result_asset_id"] != completed[1]["result_asset_id"]
    assert assets[0]["sha256"] == assets[1]["sha256"]
    assert assets[0]["rel_path"] != assets[1]["rel_path"]
    assert env.service.store.asset(assets[0]["asset_id"]) == assets[0]
    assert env.service.store.asset(original_id) == original
    assert env.asset_file(original_id).read_bytes() == photo.read_bytes()

    # 只注入第一个 job 的本地隔离事件，不撤回双方共同使用的来源修订。
    isolated = env.service.store.update_job(
        completed[0]["job_id"],
        queue_state="isolated",
        isolated=1,
        error_code="cancelled_by_revocation",
    )
    assert isolated["isolated"] == 1
    with pytest.raises(VisualError) as excinfo:
        await env.service.open_asset_for_read(assets[0]["asset_id"], actor)
    assert excinfo.value.code == "cancelled_by_revocation"
    readable = await env.service.open_asset_for_read(assets[1]["asset_id"], actor)
    assert readable["bytes"] == engine.image_bytes
    assert env.service.store.asset(assets[1]["asset_id"])["source_ref"] == completed[1]["job_id"]
    assert env.service.store.job(completed[1]["job_id"]) == completed[1]
    assert env.service.store.asset(assets[0]["asset_id"])["source_ref"] == completed[0]["job_id"]
    assert env.service.store.asset(original_id) == original
    assert env.asset_file(original_id).read_bytes() == photo.read_bytes()


@pytest.mark.parametrize("phase", ["queued", "completed"])
async def test_final_confirmed_visual_deletion_scrubs_sensitive_metadata_and_queue(env, phase):
    secrets = [f"QA_DELETE_{phase}_{kind}_ONLY" for kind in ("IDENTITY", "OUTFIT", "PROMPT")]
    actor, actor_ref, actor_id = env.access(
        "u1", attachments=[env.photo("delete_metadata_primary", color=(15, 90, 180))]
    )
    primary = await env.stage(actor, actor_ref)
    owner, owner_ref, owner_id = env.access(
        "u2", name="删除申请者", attachments=[env.photo("delete_metadata_owner", color=(190, 30, 75))]
    )
    secondary = await env.service.manage(
        "stage",
        target={"ref": "requester"},
        access=owner,
        evidence_ref=owner_ref,
        identity_description=secrets[0],
        default_outfit=secrets[1],
        activate=True,
    )
    assert primary["success"] and secondary["success"]
    await _grant(
        env,
        owner,
        owner_ref,
        capabilities=["upload_generate"],
        scope="group:g1",
        grantee=actor_id,
        max_uses=3,
    )
    source_path = env.asset_file(secondary["data"]["asset_id"])
    evidence_before = env.people.get_evidence(owner_ref)
    engine = FakeEngine(env, actor, env.store())
    submitted = await image_generate.run(
        secrets[2],
        subjects=[{"ref": "requester"}, {"ref": "person_id", "value": owner_id}],
        source_ref=actor_ref,
        data_store=engine.store,
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        invocation_context=env.invocation(actor, actor_ref),
        engine_context=engine,
    )
    assert submitted["success"], submitted
    job_id = submitted["data"]["job_id"]
    assert env.service.store.job(job_id)["person_id"] == actor_id != owner_id
    reference_dir = engine.store.artifact_dir / "references" / job_id
    copies = list(reference_dir.iterdir())
    assert len(copies) == 2
    output_paths = []
    if phase == "completed":
        await image_generate._process_once(engine, engine.store, recover_interrupted=False)
        assert len(engine.generated) == 1
        queued = next(job for job in image_generate._load_jobs(engine.store) if job["id"] == job_id)
        assert queued["status"] == "done"
        output_paths.append(Path(queued["image_path"]))
        output_paths.append(env.asset_file(env.service.store.job(job_id)["result_asset_id"]))
        assert all(path.is_file() for path in output_paths)
    env.service.store.insert_audit(
        {
            "audit_id": f"va_qa_delete_metadata_{phase}",
            "actor_id": actor_id,
            "action": "generation_requested",
            "object_type": "job",
            "object_id": job_id,
            "detail": {"description": secrets[0], "outfit": secrets[1], "prompt": secrets[2]},
        }
    )
    tables = [
        row["name"]
        for row in env.service.store.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'vs_%' ORDER BY name"
        )
    ]

    def visual_snapshot():
        return {table: env.service.store.fetchall(f'SELECT * FROM "{table}"') for table in tables}

    before = visual_snapshot()
    queue_before = engine.store.store_path.read_bytes()
    for secret in secrets:
        assert secret in json.dumps(before, ensure_ascii=False)
        assert secret in json.dumps(json.loads(queue_before), ensure_ascii=False)
    prepared = await env.service.prepare_deletion(
        {"ref": "requester"}, owner, scope="person", evidence_ref=owner_ref
    )
    assert prepared["success"], prepared
    previewed = visual_snapshot()
    assert {key: value for key, value in previewed.items() if key != "vs_deletion"} == {
        key: value for key, value in before.items() if key != "vs_deletion"
    }
    assert engine.store.store_path.read_bytes() == queue_before
    assert source_path.is_file() and all(path.is_file() for path in copies)
    generated_before = len(engine.generated)
    callbacks_before = len(engine.callbacks)

    deleted = await env.service.execute_deletion(
        prepared["data"]["confirm_token"], owner, evidence_ref=owner_ref
    )
    assert deleted["success"] and deleted["data"]["generation_blocked"]
    remaining = json.dumps(visual_snapshot(), ensure_ascii=False)
    queue_after = json.loads(engine.store.store_path.read_text(encoding="utf-8"))
    for secret in secrets:
        assert secret not in remaining, f"删除后 SQLite 仍保留敏感内容：{secret}"
        assert secret not in json.dumps(queue_after, ensure_ascii=False)
    assert not source_path.exists() and not reference_dir.exists()
    assert all(not path.exists() for path in output_paths)
    assert env.asset_file(primary["data"]["asset_id"]).is_file()
    assert (
        env.service.store.fetchall(
            "SELECT * FROM vs_reference_item WHERE person_id=? OR asset_id=?",
            (owner_id, secondary["data"]["asset_id"]),
        )
        == []
    )
    assert (
        env.service.store.fetchall("SELECT * FROM vs_reference_set WHERE person_id=?", (owner_id,))
        == []
    )
    bound = env.service.store.job(job_id)
    assert bound is not None and not bound["prompt"] and bound["isolated"]
    disk_store = ToolDataStore(engine.store.store_path)
    queued = next(job for job in image_generate._load_jobs(disk_store) if job["id"] == job_id)
    assert queued["status"] in {"cancelled", "isolated"}
    assert queued["deleted"] is True and queued["callback_state"] == "skipped"
    assert not queued.get("prompt") and not queued.get("references")
    assert env.people.get_person(owner_id, owner)["person_id"] == owner_id
    assert env.people.get_evidence(owner_ref) == evidence_before
    for result in [prepared["data"]["preview"], deleted["data"]]:
        assert isinstance(result["retained"], list) and result["retained"]
        disclosure = json.dumps(result["retained"], ensure_ascii=False)
        assert "身份" in disclosure
        assert any(word in disclosure for word in ("原聊天", "聊天记录", "聊天证据"))
        assert any(word in disclosure for word in ("不删", "不删除", "不含", "不包括", "保留", "不覆盖", "不在"))

    after_delete = visual_snapshot()
    queue_tombstone = engine.store.store_path.read_bytes()
    visual_files = {
        path: path.read_bytes()
        for path in (env.work / "visual_assets").rglob("*")
        if path.is_file()
    }
    late_artifact = engine.store.artifact_dir / "images" / f"{job_id}.png"
    late_artifact.parent.mkdir(parents=True, exist_ok=True)
    late_artifact.write_bytes(engine.image_bytes)
    late_result = await env.service.complete_generation(
        job_id, image_bytes=engine.image_bytes, mime_type=PNG_MIME, artifact_path=str(late_artifact)
    )
    assert late_result["isolated"] and late_result["deliverable"] is False
    assert late_result["code"] == "cancelled_by_revocation"
    assert not late_artifact.exists()
    assert visual_snapshot() == after_delete
    assert engine.store.store_path.read_bytes() == queue_tombstone
    assert {
        path: path.read_bytes()
        for path in (env.work / "visual_assets").rglob("*")
        if path.is_file()
    } == visual_files

    for _ in range(2):
        await image_generate._process_once(engine, engine.store, recover_interrupted=False)
    assert len(engine.generated) == generated_before
    assert len(engine.callbacks) == callbacks_before
    for secret in secrets:
        assert secret not in json.dumps(visual_snapshot(), ensure_ascii=False)
        assert secret not in engine.store.store_path.read_text(encoding="utf-8")
    assert not reference_dir.exists() and all(not path.exists() for path in output_paths)
    bridge = _VisualReceiptBridge()
    for path in [source_path, *copies, *output_paths]:
        denied = await group_file_exec.run(
            "image",
            image_path=str(path),
            bridge=bridge,
            chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
            data_store=engine.store,
            engine_context=engine,
            invocation_context=env.invocation(actor, actor_ref),
        )
        assert denied["success"] is False, denied
    assert bridge.sent == []


@pytest.mark.parametrize("flow", ["bind_human", "bind_character", "person", "legacy", "extra"])
async def test_shared_library_real_tools_need_no_manual_person_grant(env, flow):
    owner, owner_ref, owner_id = env.access(
        "u1", attachments=[env.photo("shared_owner", color=(40, 90, 130))]
    )
    target_id = owner_id
    if flow == "bind_character":
        created = await env.service.manage(
            "create_character",
            access=owner,
            evidence_ref=owner_ref,
            target={"ref": "requester"},
            name="共享立绘角色",
        )
        assert created["success"], created
        target_id = created["data"]["person_id"]
    target = {"ref": "person_id", "value": target_id}
    original = await env.stage(owner, owner_ref, target=target)
    assert original["success"], original
    provided = env.photo("shared_contribution", color=(35, 180, 65))
    actor, ref, actor_id = env.access("u2", name="提供者", attachments=[provided])
    engine = FakeEngine(env, actor, env.store())
    common = dict(
        engine_context=engine,
        invocation_context=env.invocation(actor, ref, user="u2"),
        data_store=engine.store,
    )
    active_before = env.service.store.profile(original["data"]["profile_id"])["active_revision"]
    if flow.startswith("bind_"):
        contribution = await manage_tool_run(
            "stage",
            target=target,
            source={"kind": "attachment", "index": 0},
            evidence_ref=ref,
            identity_description="提供给共享素材库的新立绘",
            activate=True,
            **common,
        )
        assert contribution["success"], contribution
        data = contribution["data"]
        assert data["status"] == "draft" and not data.get("unbound")
        profile = env.service.store.profile(data["profile_id"])
        asset = env.service.store.asset(data["asset_id"])
        assert profile["person_id"] == asset["person_id"] == target_id
        assert asset["uploader_id"] == actor_id != target_id
        assert asset["source_ref"] == ref
        assert env.asset_file(asset["asset_id"]).read_bytes() == provided.read_bytes()
        assert profile["active_revision"] == active_before
        for action in ("activate", "retire"):
            denied = await manage_tool_run(
                action,
                target=target,
                evidence_ref=ref,
                profile_id=data["profile_id"],
                revision=data["revision"],
                expected_revision=active_before,
                **common,
            )
            assert denied["success"] is False and denied["code"] == "permission_denied"
        denied_delete = await env.service.prepare_deletion(target, actor, evidence_ref=ref)
        assert denied_delete["success"] is False and denied_delete["code"] == "permission_denied"
        owner_engine = FakeEngine(env, owner, engine.store)
        review = await person_query.run(
            target=target,
            sections=["visual"],
            detail="detail",
            engine_context=owner_engine,
            invocation_context=env.invocation(owner, owner_ref),
            data_store=engine.store,
        )
        assert review["status"] == "found", review
        drafts = review["sections"]["visual"]["drafts"]
        assert any(
            item["revision"] == data["revision"] and item["profile_id"] == data["profile_id"]
            for item in drafts
        )
        assert env.service.store.profile(data["profile_id"])["active_revision"] == active_before
        assert (
            profile["scope"] == env.service.store.profile(original["data"]["profile_id"])["scope"]
        )
        activated = await manage_tool_run(
            "activate",
            target=target,
            evidence_ref=owner_ref,
            profile_id=data["profile_id"],
            revision=data["revision"],
            expected_revision=active_before,
            engine_context=owner_engine,
            invocation_context=env.invocation(owner, owner_ref),
            data_store=engine.store,
        )
        assert activated["success"], activated
        assert env.service.store.profile(data["profile_id"])["active_revision"] == data["revision"]
        asset_id = data["asset_id"]
        expected = provided.read_bytes()
    else:
        asset_id = original["data"]["asset_id"]
        expected = env.asset_file(asset_id).read_bytes()
        reference = str(env.asset_file(asset_id))
        if flow == "extra":
            assert (await env.stage(actor, ref))["success"]
        subjects = (
            [target] if flow == "person" else ([{"ref": "requester"}] if flow == "extra" else [])
        )
        submitted = await image_generate.run(
            f"共享素材库流程 {flow}",
            subjects=subjects,
            reference_images=[reference] if flow in {"legacy", "extra"} else [],
            source_ref=ref,
            chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
            **common,
        )
        assert submitted["success"], submitted
        await image_generate._process_once(engine, engine.store, recover_interrupted=False)
        assert len(engine.generated) == 1
        assert expected in [image for _, image in engine.generated[0]["references"]]
        job = env.service.store.job(submitted["data"]["job_id"])
        assert job["queue_state"] == "done" and not job["isolated"]
        assert not job["grant_id"]
        assert env.service.store.asset(job["result_asset_id"])["source_ref"] == job["job_id"]
    summary = await person_query.run(target=target, sections=["visual"], **common)
    assert summary["status"] == "found" and summary["sections"]["visual"]["state"] == "active"
    read = await read_image.run(assets=[asset_id], **common)
    assert read["success"] and read["multimodal_blocks"]
    bridge = _VisualReceiptBridge()
    sent = await group_file_exec.run(
        "image",
        image_path=str(env.asset_file(asset_id)),
        bridge=bridge,
        chat_context={"group_id": "g1", "chat_type": "group", "chat_id": "g1"},
        **common,
    )
    assert sent["success"], sent
    assert [receipt["bytes"] for receipt in bridge.sent] == [expected]
    assert env.service.store.fetchall("SELECT * FROM vs_grant") == []
