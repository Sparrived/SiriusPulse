"""插件的受管人物生图：本人同意 → 视觉资产授权 → 生图；撤回、额度、缺图都如实拒绝。

用真实的人物服务与视觉资产服务登记形象图，只把生图上游换成记录请求的假 Brain，
从插件看到的结果（图、错误码）和视觉资产系统里留下的授权、任务、素材状态来验收。
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from sirius_pulse.people import PersonAccess, PersonService
from sirius_pulse.plugins.api import PersonImageError
from sirius_pulse.plugins.context import PluginContext
from sirius_pulse.providers.base import ImageGenerationResult
from sirius_pulse.visual import VisualService, policies

pytestmark = pytest.mark.asyncio

GROUP = "g1"


def _png(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buffer, format="PNG")
    return buffer.getvalue()


class World:
    """一个人格：人物服务、视觉资产服务、她自己的形象图，和一个记录请求的生图上游。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.people = PersonService(None, persona_name="月白")
        self.visual = VisualService(root / "persona", people=self.people, conn=self.people.conn)
        assets = root / "assets"
        assets.mkdir()
        (assets / "turnaround.png").write_bytes(_png((240, 240, 240)))
        self.requests: list = []
        self.during_generation = None
        self.engine = SimpleNamespace(
            identity_resolver=None,
            user_manager=None,
            person_service=self.people,
            visual_service=self.visual,
            brain=SimpleNamespace(
                persona_assets_dir=assets, generate_image_async=self._generate_image
            ),
        )
        self.proxy = PluginContext.create(engine=self.engine, plugin_name="gacha").engine
        self._seq = 0

    async def _generate_image(self, request):
        self.requests.append(request)
        if self.during_generation is not None:
            await self.during_generation()
        return ImageGenerationResult(image_bytes=_png((10, 120, 220)), mime_type="image/png")

    def say(self, user: str, text: str, *, attachments: list[Path] | None = None) -> str:
        """群友在群里发一句话，返回人物模块给这条消息记下的证据。"""
        self._seq += 1
        return self.people.record_evidence(
            group_id=GROUP,
            user_id=user,
            platform="napcat",
            platform_uid=f"qq_{user}",
            message_id=f"m{self._seq}",
            content=text,
            attachments=[str(path) for path in attachments or []],
        )

    async def register_look(self, user: str) -> None:
        """群友本人发图说“记一下我的样子”，登记成正式形象。"""
        photo = self.root / f"{user}.png"
        photo.write_bytes(_png((200, 30, 30)))
        ref = self.say(user, "记一下我的样子", attachments=[photo])
        person_id = self.people.person_id_for_user(user)
        access = PersonAccess(actor_id=person_id, group_id=GROUP, evidence_refs=(ref,))
        self.people.update_names(person_id, access, ref, preferred_name=user)
        staged = await self.visual.manage(
            "stage",
            access=access,
            evidence_ref=ref,
            target={"ref": "requester"},
            source={"kind": "attachment", "index": 0},
            identity_description="黑发红围巾",
            default_outfit="校服",
            view="正面",
            activate=True,
        )
        assert staged["success"], staged

    def consent(self, user: str) -> dict:
        ref = self.say(user, "/我的卡 画我")
        return self.proxy.grant_person_image_consent(user_id=user, group_id=GROUP, evidence_ref=ref)

    async def paint(self, user: str) -> dict:
        return await self.proxy.generate_person_image("她在月光下给阿明递一张卡", user_id=user, group_id=GROUP)

    def jobs(self, user: str) -> list[dict]:
        return self.visual.store.jobs_for(self.people.person_id_for_user(user), limit=10)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


async def test_member_who_consented_is_drawn_from_their_registered_look(world):
    await world.register_look("u1")
    consent = world.consent("u1")

    result = await world.paint("u1")

    assert consent["has_reference"] is True
    assert result["image_bytes"] and result["mime_type"] == "image/png"
    request = world.requests[0]
    assert request.model == "image_generate"
    # 他的正式形象 + 她自己的一张形象图；最终提示词由视觉资产系统编译，点名了图序。
    assert len(request.reference_images) == 2
    assert "她在月光下给阿明递一张卡" in request.prompt
    [job] = world.jobs("u1")
    assert job["queue_state"] == "done" and job["grant_id"] == consent["grant_id"]
    assert job["manifest"]["requested_by"] == "plugin:gacha"
    assert policies.deployment_state(world.visual.store)["used"] == 1
    status = world.proxy.get_person_image_consent(user_id="u1", group_id=GROUP)
    assert status == {"consented": True, "grant_id": consent["grant_id"], "has_reference": True}


async def test_when_she_has_a_registered_look_she_is_bound_as_a_person_too(world):
    """她在视觉资产系统里也登记了正式形象：作为第二个人物主体，而不是“附加参考”。"""
    photo = world.root / "persona.png"
    photo.write_bytes(_png((240, 240, 250)))
    ref = world.say("admin", "登记月白的形象", attachments=[photo])
    admin_id = world.people.person_id_for_user("admin")
    admin = PersonAccess(actor_id=admin_id, group_id=GROUP, evidence_refs=(ref,), is_admin=True)
    world.people.update_names(admin_id, admin, ref, preferred_name="管理员")
    staged = await world.visual.manage(
        "stage",
        access=admin,
        evidence_ref=ref,
        target={"ref": "persona"},
        source={"kind": "attachment", "index": 0},
        identity_description="银发红瞳，白色长裙",
        default_outfit="白色长裙",
        view="正面",
        activate=True,
    )
    assert staged["success"], staged
    await world.register_look("u1")
    world.consent("u1")

    await world.paint("u1")

    prompt = world.requests[0].prompt
    assert "银发红瞳" in prompt and "黑发红围巾" in prompt
    assert "附加参考" not in prompt
    assert len(world.requests[0].reference_images) == 2


async def test_consent_must_come_from_the_member_themself(world):
    await world.register_look("u1")
    someone_else = world.say("u2", "/我的卡 画我")

    with pytest.raises(PersonImageError) as denied:
        world.proxy.grant_person_image_consent(
            user_id="u1", group_id=GROUP, evidence_ref=someone_else
        )

    assert denied.value.code in {"evidence_invalid", "permission_denied"}
    assert world.proxy.get_person_image_consent(user_id="u1", group_id=GROUP)["consented"] is False


@pytest.mark.parametrize(
    ("registered", "consented", "code"),
    [
        (True, False, "consent_required"),
        (False, True, "required_asset_missing"),
    ],
)
async def test_without_consent_or_registered_look_nothing_is_sent(
    world, registered, consented, code
):
    if registered:
        await world.register_look("u1")
    else:
        world.say("u1", "大家好")
    if consented:
        assert world.consent("u1")["has_reference"] is registered

    with pytest.raises(PersonImageError) as refused:
        await world.paint("u1")

    assert refused.value.code == code
    assert world.requests == []
    assert policies.deployment_state(world.visual.store)["used"] == 0


async def test_after_revoking_she_no_longer_draws_them_and_old_results_are_isolated(world):
    await world.register_look("u1")
    world.consent("u1")
    await world.paint("u1")
    [job] = world.jobs("u1")

    revoked = await world.proxy.revoke_person_image_consent(user_id="u1", group_id=GROUP)

    assert revoked["isolated_assets"] == [job["result_asset_id"]]
    assert world.visual.store.asset(job["result_asset_id"])["status"] == "isolated"
    with pytest.raises(PersonImageError) as refused:
        await world.paint("u1")
    assert refused.value.code == "consent_required"
    assert len(world.requests) == 1


async def test_revoking_while_she_is_drawing_isolates_the_result(world):
    await world.register_look("u1")
    world.consent("u1")

    async def revoke_now():
        await world.proxy.revoke_person_image_consent(user_id="u1", group_id=GROUP)

    world.during_generation = revoke_now
    with pytest.raises(PersonImageError) as refused:
        await world.paint("u1")

    assert refused.value.code == "cancelled_by_revocation"
    [job] = world.jobs("u1")
    assert job["queue_state"] == "isolated"


async def test_used_up_person_image_quota_is_reported_and_nothing_is_sent(world):
    await world.register_look("u1")
    world.consent("u1")
    world.visual.store.set_setting(policies.SETTING_TOTAL_QUOTA, 0, actor_id="admin")

    with pytest.raises(PersonImageError) as refused:
        await world.paint("u1")

    assert refused.value.code == "quota_exceeded"
    assert world.requests == [] and world.jobs("u1") == []
