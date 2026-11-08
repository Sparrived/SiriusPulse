"""插件的受管人物生图：本人同意 → 视觉资产授权 → 编译、额度、预检、生图、结果隔离。

插件想把某个群友按他登记的人物形象画进图里时，不能自己拿参考图字节去生图：
人物参考图属于视觉资产系统，使用必须可授权、可审计、可撤回。这里把
``image_generate`` Tool 的 ``subjects`` 路径（编译 → 部署额度 → 绑定 → 预检 →
提交许可 → 提交边界 → 完成/隔离）原样搬到一次同步调用里，只多做一件事：
把本人的同意记成一条视觉授权，并绑到生成任务的人物主体上。

- **同意**：本人在群里发指令时，用那条消息的来源证据建一条授权——人物与被授权人
  都是他自己，能力只有 ``upload_generate``，作用域是这个群，``recipe`` 标成
  ``plugin:<插件名>``。证据必须是他本人、本群、账号已绑定的消息。
- **生成**：授权绑在任务主体上，预检、提交许可、提交边界与完成时都会按这条授权
  重验；中途撤回，未提交的取消并退额度，已提交的结果隔离、不交付。
- **撤回**：只收窄权限，所以不要求证据（退出、撤回永远不该比同意更难）；撤回后
  依据这条同意画出的结果在视觉资产系统里一并隔离。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_CAPABILITY = "upload_generate"
_MAX_PERSONA_REFERENCE_BYTES = 20 * 1024 * 1024


class PersonImageError(RuntimeError):
    """受管人物生图没有做成；``code`` 与视觉资产系统的错误码一致。

    常见：``consent_required``（没有同意或已撤回）、``required_asset_missing``
    （没有登记形象图）、``quota_exceeded``（部署方人物生图额度用尽）、
    ``cancelled_by_revocation``（生成途中撤回，结果已隔离）、``unavailable``
    （引擎没有接入人物/视觉服务）、``evidence_invalid``（同意的来源证据不可核验）。
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def consent_marker(plugin_name: str) -> str:
    return f"plugin:{plugin_name}"


def _services(engine: Any) -> tuple[Any, Any]:
    people = getattr(engine, "person_service", None)
    visual = getattr(engine, "visual_service", None)
    if engine is None or people is None or visual is None:
        raise PersonImageError("unavailable", "当前引擎没有接入人物与视觉资产服务")
    return people, visual


def _access(actor_id: str, group_id: str, evidence_ref: str = "") -> Any:
    from sirius_pulse.people import PersonAccess

    return PersonAccess(
        actor_id=actor_id,
        group_id=group_id,
        evidence_refs=(evidence_ref,) if evidence_ref else (),
    )


def _actor_for(people: Any, user_id: str) -> str:
    actor = str(people.person_id_for_user(str(user_id or "")) or "")
    if not actor:
        raise PersonImageError("account_unbound", "这个人还没有可核验的账号身份")
    return actor


def _consent_grants(visual: Any, actor: str, group_id: str, plugin_name: str) -> list[dict]:
    """这个插件在这个群里、以本人名义记下且仍然有效的同意。"""
    from sirius_pulse.visual.access import _grant_expired

    marker = consent_marker(plugin_name)
    scope = f"group:{group_id}"
    return [
        grant
        for grant in visual.store.grants_for(actor, status="active")
        if str(grant.get("recipe") or "") == marker
        and str(grant.get("scope") or "") == scope
        and visual.store.grantee_for(grant) == actor
        and _CAPABILITY in (grant.get("capabilities") or [])
        and not _grant_expired(grant)
    ]


def consent_status(engine: Any, plugin_name: str, *, user_id: str, group_id: str) -> dict:
    people, visual = _services(engine)
    actor = str(people.person_id_for_user(str(user_id or "")) or "")
    if not actor:
        return {"consented": False, "has_reference": False, "grant_id": ""}
    grants = _consent_grants(visual, actor, group_id, plugin_name)
    return {
        "consented": bool(grants),
        "grant_id": str(grants[0]["grant_id"]) if grants else "",
        "has_reference": visual.has_generation_reference(actor, _access(actor, group_id)),
    }


def grant_consent(
    engine: Any, plugin_name: str, *, user_id: str, group_id: str, evidence_ref: str
) -> dict:
    """用本人这条消息的证据记一条同意；已经同意过就返回原来那条。"""
    from sirius_pulse.visual import policies
    from sirius_pulse.visual.access import require_evidence
    from sirius_pulse.visual.errors import VisualError

    people, visual = _services(engine)
    if not str(evidence_ref or "").strip():
        raise PersonImageError("evidence_invalid", "同意必须来自本人在群里发的这条消息")
    actor = _actor_for(people, user_id)
    access = _access(actor, group_id, evidence_ref)
    try:
        require_evidence(people, access, evidence_ref)
        existing = _consent_grants(visual, actor, group_id, plugin_name)
        if existing:
            grant_id = str(existing[0]["grant_id"])
        else:
            result = policies.create_grant(
                visual.store,
                access,
                person_id=actor,
                mode="manual",
                capabilities=[_CAPABILITY],
                revision_scope="future",
                scope=f"group:{group_id}",
                recipe=consent_marker(plugin_name),
                evidence_ref=evidence_ref,
                grantee=actor,
            )
            grant_id = str(result["grant_id"])
    except VisualError as exc:
        raise PersonImageError(exc.code, exc.message) from exc
    logger.info("[plugin:%s] 记下人物生图同意：%s", plugin_name, grant_id)
    return {
        "grant_id": grant_id,
        "has_reference": visual.has_generation_reference(actor, _access(actor, group_id)),
    }


async def revoke_consent(engine: Any, plugin_name: str, *, user_id: str, group_id: str) -> dict:
    """撤回同意：未提交的任务取消，依据这条同意画出的结果隔离。"""
    from sirius_pulse.visual import lifecycle, policies

    people, visual = _services(engine)
    actor = str(people.person_id_for_user(str(user_id or "")) or "")
    revoked: list[str] = []
    cancelled: list[str] = []
    isolated: list[str] = []
    if not actor:
        return {"revoked": revoked, "cancelled_jobs": cancelled, "isolated_assets": isolated}
    access = _access(actor, group_id)
    for grant in _consent_grants(visual, actor, group_id, plugin_name):
        grant_id = str(grant["grant_id"])
        policies.revoke_consent(
            visual.store,
            access,
            person_id=actor,
            grant_id=grant_id,
            reason=f"本人在 {consent_marker(plugin_name)} 撤回同意",
        )
        revoked.append(grant_id)
        for job in visual.store.jobs_by_grant(grant_id):
            job_id = str(job.get("job_id") or "")
            if str(job.get("queue_state")) not in lifecycle.TERMINAL_STATES:
                await visual.cancel_generation(job_id, "cancelled_by_revocation")
                cancelled.append(job_id)
            asset_id = str(job.get("result_asset_id") or "")
            if asset_id:
                visual.store.set_asset_status(asset_id, "isolated")
                isolated.append(asset_id)
    if revoked:
        logger.info("[plugin:%s] 撤回人物生图同意：%s", plugin_name, ", ".join(revoked))
    return {"revoked": revoked, "cancelled_jobs": cancelled, "isolated_assets": isolated}


def _persona_reference_paths(engine: Any, limit: int) -> list[str]:
    from sirius_pulse.utils.persona_assets import collect_persona_assets

    brain = getattr(engine, "brain", None)
    paths: list[str] = []
    for asset in collect_persona_assets(getattr(brain, "persona_assets_dir", None)):
        if len(paths) >= max(0, limit):
            break
        try:
            if asset.path.stat().st_size <= _MAX_PERSONA_REFERENCE_BYTES:
                paths.append(str(asset.path))
        except OSError:
            continue
    return paths


async def generate(
    engine: Any,
    plugin_name: str,
    prompt: str,
    *,
    user_id: str,
    group_id: str,
    persona_references: int = 2,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    """按本人的同意与正式形象生成一张图；任一关卡不过都抛 :class:`PersonImageError`。"""
    from sirius_pulse.providers.base import ImageGenerationRequest
    from sirius_pulse.visual import policies
    from sirius_pulse.visual.errors import VisualError
    from sirius_pulse.visual.images import optimize_image_bytes

    people, visual = _services(engine)
    actor = _actor_for(people, user_id)
    access = _access(actor, group_id)
    grants = _consent_grants(visual, actor, group_id, plugin_name)
    if not grants:
        raise PersonImageError("consent_required", "本人还没有同意被画进图里，或已经撤回")
    grant = grants[0]
    grant_id = str(grant["grant_id"])
    purpose = "person_image"
    # 她自己：在视觉资产系统里登记过正式形象就作为第二个人物主体（编译器会标清图序与身份），
    # 否则退回 ``{persona}/assets/`` 的形象图作附加参考。
    subjects: list[dict[str, Any]] = [{"ref": "person_id", "value": actor}]
    extras: list[str] = []
    if persona_references > 0:
        if visual.has_generation_reference(people.persona_id, access):
            subjects.append({"ref": "persona"})
        else:
            extras = _persona_reference_paths(engine, persona_references)
    try:
        plan = await visual.compile_generation(
            access=access,
            subjects=subjects,
            prompt=prompt,
            extra_references=extras,
            purpose=purpose,
            group_id=group_id,
        )
    except VisualError as exc:
        raise PersonImageError(exc.code, exc.message) from exc

    # 共享使用本来不绑逐人授权；这里显式绑上本人的同意，让之后每一道复查都按它来。
    manifest = plan["manifest"]
    for subject in manifest.get("subjects", []):
        if str(subject.get("person_id") or "") == actor:
            subject["grant_id"] = grant_id
            subject["grant"] = {
                "revision_scope": str(grant.get("revision_scope") or "selected"),
                "scope": str(grant.get("scope") or ""),
                "expires_at": grant.get("expires_at"),
                "max_uses": grant.get("max_uses"),
                "source_revision": grant.get("source_revision"),
            }
    manifest["grant_id"] = grant_id
    manifest["requested_by"] = consent_marker(plugin_name)

    job_id = f"plg_{uuid.uuid4().hex[:10]}"
    if not policies.consume_deployment(visual.store):
        raise PersonImageError("quota_exceeded", "部署方人物生图额度已用尽")
    try:
        await visual.begin_generation(
            job_id, plan, access=access, source_ref="", purpose=purpose, group_id=group_id
        )
    except VisualError as exc:
        policies.refund_deployment(visual.store)
        raise PersonImageError(exc.code, exc.message) from exc
    try:
        await visual.mark_generation_enqueued(job_id)
        for step in (
            visual.preflight_generation,
            visual.acquire_generation_permit,
            visual.mark_generation_submit_started,
        ):
            checked = await step(job_id)
            if not checked.get("allow"):
                code = str(checked.get("code") or "cancelled_by_revocation")
                raise PersonImageError(code, "授权已撤回或素材已停用，这次没有画")
    except PersonImageError:
        raise
    except Exception:
        await visual.cancel_generation(job_id, "queue_unavailable")
        raise

    references = [
        (str(image["name"]), optimize_image_bytes(bytes(image["bytes"])))
        for image in plan["images"]
    ]
    request = ImageGenerationRequest(
        model="image_generate",
        prompt=str(plan["prompt"]),
        reference_images=references,
        timeout_seconds=timeout_seconds,
    )
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            engine.brain.generate_image_async(request), timeout=timeout_seconds
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        await visual.fail_generation(job_id, "generation_timeout")
        raise
    except Exception:
        await visual.fail_generation(job_id, "generation_failed")
        raise
    image_bytes = bytes(result.image_bytes or b"")
    if not image_bytes:
        await visual.fail_generation(job_id, "generation_failed")
        raise PersonImageError("generation_failed", "生图接口没有返回图片")
    outcome = await visual.complete_generation(
        job_id, image_bytes=image_bytes, mime_type=result.mime_type, artifact_path=""
    )
    if outcome.get("isolated"):
        raise PersonImageError(
            str(outcome.get("code") or "cancelled_by_revocation"),
            "生成途中授权被撤回，结果已隔离，不交付",
        )
    logger.info(
        "[plugin:%s] 受管人物生图完成：任务 %s，%d 张参考图，%.1f 秒",
        plugin_name,
        job_id,
        len(references),
        time.perf_counter() - started,
    )
    return {"image_bytes": image_bytes, "mime_type": result.mime_type, "job_id": job_id}
