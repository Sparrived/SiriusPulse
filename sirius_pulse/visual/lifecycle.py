"""生成绑定的状态机与外部提交边界。

PRD GEN-03／AUTO-04 的核心：快照固定内容，不固定权限。任务入队后，每次
真正向上游发起请求前都要重新检查撤权、停用与文件状态；本地提交许可与撤权
之间必须留下可解释的持久化时间线：

``prepared → enqueued → dispatching → permit_acquired → submit_started → submitted``

其中 ``submit_started`` 之后的结果一律按“可能已外传”处理，不自动重放付费请求。
"""

from __future__ import annotations

from typing import Any

from sirius_pulse.visual.access import _grant_expired, grant_covers_revision, scope_matches
from sirius_pulse.visual.errors import VisualError
from sirius_pulse.visual.store import VisualStore, now_iso

TERMINAL_STATES: frozenset[str] = frozenset(
    {"done", "failed", "cancelled", "isolated", "outcome_uncertain"}
)
PENDING_STATES: frozenset[str] = frozenset(
    {"prepared", "enqueued", "dispatching", "permit_acquired", "submitted"}
)


def begin_binding(
    store: VisualStore,
    *,
    job_id: str,
    purpose: str,
    actor_id: str,
    group_id: str,
    prompt: str,
    input_hash: str,
    manifest: dict[str, Any],
    person_id: str = "",
    profile_id: str = "",
    revision: int | None = None,
    source_revision: int | None = None,
    source_hash: str = "",
    grant_id: str = "",
    recipe: str = "",
    recipe_version: str = "",
    attempt: int = 1,
    idempotency_key: str = "",
) -> dict[str, Any]:
    store.insert_job(
        {
            "job_id": job_id,
            "idempotency_key": idempotency_key,
            "purpose": purpose,
            "person_id": person_id,
            "profile_id": profile_id,
            "revision": revision,
            "source_revision": source_revision,
            "source_hash": source_hash,
            "recipe": recipe,
            "recipe_version": recipe_version,
            "prompt": prompt,
            "input_hash": input_hash,
            "manifest": manifest,
            "grant_id": grant_id,
            "actor_id": actor_id,
            "group_id": group_id,
            "attempt": attempt,
            "queue_state": "prepared",
            "external_boundary": "not_submitted",
        }
    )
    job = store.job(job_id)
    assert job is not None
    return job


def mark_enqueued(store: VisualStore, job_id: str) -> dict[str, Any]:
    job = store.update_job(job_id, only_states=("prepared",), queue_state="enqueued")
    if job is None:
        raise VisualError("invalid_argument", "生成任务不在可入队状态")
    return job


def abort_binding(store: VisualStore, job_id: str, code: str) -> dict[str, Any]:
    job = store.update_job(
        job_id,
        only_states=tuple(PENDING_STATES),
        queue_state="failed",
        error_code=code,
        finished_at=now_iso(),
    )
    if job is None:
        raise VisualError("invalid_argument", "生成任务已结束，无法中止")
    return job


def _grant_still_valid(store: VisualStore, grant_id: str) -> bool:
    return _grant_reason(store, grant_id, capability="upload_generate") == ""


def _grant_reason(
    store: VisualStore,
    grant_id: str,
    *,
    capability: str,
    person_id: str = "",
    revision: Any = None,
    group_id: str = "",
) -> str:
    """按**实际使用的授权**重验能力、范围、有效期、次数与来源修订。

    只看 ``status == active`` 不够：撤回单项能力、把作用域改小、到期或次数用尽
    都必须让排队中/提交中/待交付的任务失效；空 grant_id 也不能当作放行理由。
    """
    grant = store.grant(grant_id)
    if grant is None or str(grant.get("status")) != "active":
        return "cancelled_by_revocation"
    if capability not in (grant.get("capabilities") or []):
        return "cancelled_by_revocation"
    if person_id and str(grant.get("person_id") or "") != person_id:
        return "cancelled_by_revocation"
    if group_id and not scope_matches(str(grant.get("scope") or ""), group_id):
        return "cancelled_by_revocation"
    if _grant_expired(grant):
        return "cancelled_by_revocation"
    max_uses = grant.get("max_uses")
    if max_uses is not None and int(grant.get("used_count") or 0) > int(max_uses):
        return "cancelled_by_revocation"
    if capability == "upload_generate" and not grant_covers_revision(grant, revision):
        return "cancelled_by_revocation"
    return ""


def _profile_scope_matches(profile: dict[str, Any], group_id: str) -> bool:
    """人格内共享只扩大使用，不扩大档案原本的会话可见范围。"""
    scope = str(profile.get("scope") or "").strip()
    if scope in {"", "persona"}:
        return True
    return bool(group_id) and scope in {group_id, f"group:{group_id}"}


def _source_reason(
    store: VisualStore,
    person_id: Any,
    profile_id: Any,
    revision: Any,
    group_id: str,
) -> str:
    """共享使用仍须绑定真实主体、未停用修订和当前任务可见的档案。"""
    person_id = str(person_id or "").strip()
    profile_id = str(profile_id or "").strip()
    if not person_id or not profile_id or revision is None or isinstance(revision, bool):
        return "required_asset_missing"
    if store.subject_deleted(person_id):
        return "cancelled_by_revocation"
    try:
        number = int(revision)
    except (TypeError, ValueError):
        return "required_asset_missing"
    profile = store.profile(profile_id)
    if profile is None or str(profile.get("person_id") or "") != person_id:
        return "required_asset_missing"
    if not _profile_scope_matches(profile, group_id):
        return "consent_required"
    row = store.revision(profile_id, number)
    if row is None:
        return "required_asset_missing"
    if str(row.get("person_id") or "") != person_id:
        return "required_asset_missing"
    if str(row.get("status") or "") in {"retired", "rejected"}:
        return "cancelled_by_revocation"
    return ""


def _auto_reason(store: VisualStore, job: dict[str, Any], manifest: dict[str, Any]) -> str:
    """自动候选不能借共享素材规则省略许可、配额或已消费记录。"""
    group_id = str(job.get("group_id") or "")
    grant_id = str(job.get("grant_id") or manifest.get("grant_id") or "")
    grant = store.grant(grant_id)
    person_id = str(job.get("person_id") or "")
    if grant is None or str(grant.get("mode") or "") != "auto_candidate":
        return "consent_required"
    if str(grant.get("person_id") or "") != person_id:
        return "consent_required"
    if store.grantee_for(grant) != str(job.get("actor_id") or ""):
        return "consent_required"
    reason = _grant_reason(
        store,
        grant_id,
        capability="upload_generate",
        person_id=person_id,
        revision=job.get("source_revision"),
        group_id=group_id,
    )
    if reason:
        return reason
    # 入队已预占本次次数；_grant_reason 已允许 used_count == max_uses，
    # 这里只验证本任务确实消费了这条授权，不能再按“下一次是否可新建”拒绝本次。
    consumed = manifest.get("grants_consumed")
    if not isinstance(consumed, list) or grant_id not in [str(item) for item in consumed]:
        return "consent_required"
    # 自动控制器以 auto_reserved 记录每日桶及总额预占；另一条共享入队路径
    # 会补写 deployment_charged，不能把后者的默认值误当成自动预占缺失。
    if not manifest.get("auto_reserved"):
        return "quota_exceeded"
    if manifest.get("budget_refunded") or manifest.get("grants_refunded"):
        return "quota_exceeded"
    return ""


def _subject_reason(store: VisualStore, job: dict[str, Any]) -> str:
    """共享素材不要求逐人 grant，但自动任务和显式 grant 仍完整重验。"""
    manifest = job.get("manifest") if isinstance(job.get("manifest"), dict) else {}
    if bool(manifest.get("auto")) or str(job.get("purpose") or "") == "auto_candidate":
        reason = _auto_reason(store, job, manifest)
        if reason:
            return reason
    raw_subjects = manifest.get("subjects")
    raw_additional = manifest.get("additional_subjects")
    if raw_additional is None:
        raw_additional = []
    if not isinstance(raw_subjects, list) or not isinstance(raw_additional, list):
        return "required_asset_missing"
    subjects = [*raw_subjects, *raw_additional]
    if not subjects:
        # 普通非人物生图没有人物绑定；人物／派生任务不能拿空清单绕过校验。
        if str(job.get("purpose") or "") not in {"image_generate", "reference_image"}:
            return "required_asset_missing"
        if str(job.get("grant_id") or "") or manifest.get("references"):
            return "required_asset_missing"
        return ""
    if store.subject_deleted(str(job.get("actor_id") or "")):
        return "consent_required"
    actor = str(job.get("actor_id") or "")
    group_id = str(job.get("group_id") or "")
    # 顶层授权是任务绑定的一部分：不能只从单个 subject 擦掉它就改用共享规则。
    bound_grant_id = str(job.get("grant_id") or manifest.get("grant_id") or "")
    bound_grant = store.grant(bound_grant_id) if bound_grant_id else None
    if bound_grant_id and bound_grant is None:
        return "cancelled_by_revocation"
    bound_grant_checked = not bound_grant_id
    for subject in subjects:
        if not isinstance(subject, dict):
            return "required_asset_missing"
        person_id = str(subject.get("person_id") or "").strip()
        profile_id = str(subject.get("profile_id") or "").strip()
        revision = subject.get("revision")
        if not person_id:
            return "consent_required"
        if store.subject_deleted(person_id):
            return "cancelled_by_revocation"
        grant_id = str(subject.get("grant_id") or "")
        capability = str(subject.get("capability") or "upload_generate")
        if grant_id:
            reason = _grant_reason(
                store,
                grant_id,
                capability=capability,
                person_id=person_id,
                revision=revision,
                group_id=group_id,
            )
            if reason:
                return reason
            if store.grantee_for(store.grant(grant_id) or {}) != actor:
                return "cancelled_by_revocation"
            if grant_id == bound_grant_id:
                bound_grant_checked = True
        elif bound_grant is not None and str(bound_grant.get("person_id") or "") == person_id:
            reason = _grant_reason(
                store,
                bound_grant_id,
                capability="upload_generate",
                person_id=person_id,
                revision=revision,
                group_id=group_id,
            )
            if reason:
                return reason
            if store.grantee_for(bound_grant) != actor:
                return "cancelled_by_revocation"
            bound_grant_checked = True
        reason = _source_reason(store, person_id, profile_id, revision, group_id)
        if reason:
            return reason
    if not bound_grant_checked:
        return "cancelled_by_revocation"
    return ""


def _manifest_references(job: dict[str, Any]) -> list[dict[str, Any]] | None:
    manifest = job.get("manifest")
    if not isinstance(manifest, dict):
        return None
    refs = manifest.get("references")
    if not isinstance(refs, list):
        return None
    if any(not isinstance(ref, dict) for ref in refs):
        return None
    return refs


def _asset_chain_reason(
    store: VisualStore,
    asset: dict[str, Any],
    ref: dict[str, Any],
    group_id: str,
    *,
    visited_jobs: set[str] | None = None,
) -> str:
    """父链、生成来源、同内容重传和参考集都不能洗掉撤权与范围。"""
    del ref
    visited_assets: set[str] = set()
    checked_jobs = set(visited_jobs or ())
    current: dict[str, Any] | None = asset
    first = True
    while current is not None:
        asset_id = str(current.get("asset_id") or "")
        if not asset_id or asset_id in visited_assets:
            return "required_asset_missing" if first else "cancelled_by_revocation"
        visited_assets.add(asset_id)
        if str(current.get("status") or "") != "available":
            return "cancelled_by_revocation"
        person_id = str(current.get("person_id") or "")
        if not person_id:
            return "required_asset_missing" if first else "cancelled_by_revocation"
        if store.subject_deleted(person_id):
            return "cancelled_by_revocation"

        source_ref = str(current.get("source_ref") or "")
        source_job = store.job(source_ref) if source_ref else None
        source_job_id = str((source_job or {}).get("job_id") or source_ref)
        if source_job is not None and str(source_job.get("result_asset_id") or "") == asset_id:
            if source_job_id in checked_jobs:
                return "required_asset_missing"
            reason = revocation_reason(store, source_job, _visited_jobs=checked_jobs)
            if reason:
                return reason

        bindings = store.fetchall(
            "SELECT p.profile_id,p.person_id AS profile_person_id,p.scope,"
            " r.revision,r.person_id AS revision_person_id,r.status"
            " FROM vs_reference_item i"
            " JOIN vs_reference_set s ON s.set_id=i.set_id"
            " JOIN vs_profile p ON p.profile_id=s.profile_id"
            " JOIN vs_revision r ON r.profile_id=p.profile_id AND r.revision=s.revision"
            " WHERE i.asset_id=?",
            (asset_id,),
        )
        if bindings:
            usable = False
            for binding in bindings:
                if str(binding.get("profile_person_id") or "") != person_id:
                    return "cancelled_by_revocation"
                if str(binding.get("revision_person_id") or "") != person_id:
                    return "cancelled_by_revocation"
                if not _profile_scope_matches(binding, group_id):
                    continue
                if str(binding.get("status") or "") in {"retired", "rejected"}:
                    continue
                usable = True
            if not usable:
                # 有受管绑定但都不在当前范围／均已停用时，不可拿旧快照绕过。
                return "cancelled_by_revocation"
        elif not first and source_job is None:
            # 父链上的无绑定成品必须有可核验的生成任务来源。
            return "required_asset_missing"
        parent_id = str(current.get("parent_asset_id") or "").strip()
        if not parent_id:
            break
        current = store.asset(parent_id)
        first = False
        if current is None:
            return "cancelled_by_revocation"
    return ""


def revocation_reason(
    store: VisualStore, job: dict[str, Any], *, _visited_jobs: set[str] | None = None
) -> str:
    """返回阻止继续使用该绑定的错误码；空字符串表示仍可执行。"""
    visited_jobs = set(_visited_jobs or ())
    job_id = str(job.get("job_id") or "")
    if job_id:
        if job_id in visited_jobs:
            return "required_asset_missing"
        visited_jobs.add(job_id)
    if int(job.get("isolated") or 0):
        return "cancelled_by_revocation"
    manifest = job.get("manifest")
    if not isinstance(manifest, dict) or manifest.get("legacy_unverifiable"):
        return "required_asset_missing"
    references = _manifest_references(job)
    if references is None:
        return "required_asset_missing"
    subject_reason = _subject_reason(store, job)
    if subject_reason:
        return subject_reason
    group_id = str(job.get("group_id") or "")
    for ref in references:
        asset_id = str(ref.get("asset_id") or "")
        if not asset_id:
            if ref.get("purpose") == "extra" and not ref.get("person_id") and ref.get("sha256"):
                continue
            return "required_asset_missing"
        asset = store.asset(asset_id)
        if asset is None:
            return "required_asset_missing"
        reason = _asset_chain_reason(store, asset, ref, group_id, visited_jobs=visited_jobs)
        if reason:
            return reason
        profile_id = str(ref.get("profile_id") or "")
        revision = ref.get("revision")
        if profile_id and revision is not None:
            reason = _source_reason(
                store,
                asset.get("person_id") or ref.get("person_id"),
                profile_id,
                revision,
                group_id,
            )
            if reason:
                return reason
    return ""


def preflight(store: VisualStore, job_id: str) -> dict[str, Any]:
    """后台开始处理前的权限与状态复查；不触达网络。"""
    job = store.job(job_id)
    if job is None:
        return {"allow": False, "code": "outcome_uncertain", "job": None}
    if str(job.get("queue_state")) in TERMINAL_STATES:
        return {
            "allow": False,
            "code": str(job.get("error_code") or "cancelled_by_revocation"),
            "job": job,
        }
    reason = revocation_reason(store, job)
    if reason:
        cancelled = cancel_binding(store, job, reason, refund=not _permit_acquired(job))
        return {"allow": False, "code": reason, "job": cancelled}
    updated = store.update_job(
        job_id,
        only_states=("prepared", "enqueued", "dispatching", "permit_acquired"),
        queue_state="dispatching",
    ) or store.job(job_id)
    return {"allow": True, "code": "", "job": updated}


def _permit_acquired(job: dict[str, Any]) -> bool:
    return str(job.get("external_boundary")) in {
        "permit_acquired",
        "submit_started",
        "submitted",
    }


def acquire_permit(store: VisualStore, job_id: str) -> tuple[bool, str, dict[str, Any] | None]:
    """领取本地提交许可；撤权先于许可时直接取消，不向上游发起请求。"""
    job = store.job(job_id)
    if job is None:
        return False, "outcome_uncertain", None
    if str(job.get("queue_state")) in TERMINAL_STATES:
        return False, str(job.get("error_code") or "cancelled_by_revocation"), job
    reason = revocation_reason(store, job)
    if reason:
        cancelled = cancel_binding(store, job, reason, refund=True)
        return False, reason, cancelled
    updated = store.update_job(
        job_id,
        only_states=("dispatching", "enqueued", "prepared"),
        external_boundary="permit_acquired",
        permit_at=now_iso(),
    )
    if updated is None:
        return False, "already_pending", store.job(job_id)
    return True, "", updated


def start_submit(store: VisualStore, job_id: str) -> tuple[bool, str, dict[str, Any] | None]:
    """真正调用上游前的最后一道同步检查与边界落盘。

    这里到 ``ctx.generate_image`` 之间不能有 ``await``：撤权协程只能在 await 点
    抢占，因此这一小段是本地可见的竞争边界。
    """
    job = store.job(job_id)
    if job is None:
        return False, "outcome_uncertain", None
    reason = revocation_reason(store, job)
    if reason:
        cancelled = cancel_binding(store, job, reason, refund=True)
        return False, reason, cancelled
    now = now_iso()
    updated = store.update_job(
        job_id,
        only_states=("dispatching", "enqueued", "prepared", "permit_acquired"),
        external_boundary="submit_started",
        submit_started_at=now,
        submitted_at=now,
        queue_state="submitted",
    )
    if updated is None:
        return False, "already_pending", store.job(job_id)
    return True, "", updated


def mark_result(
    store: VisualStore,
    job_id: str,
    *,
    result_asset_id: str,
    result_sha256: str,
    result_path: str,
    isolated: bool,
    revision: int | None = None,
) -> dict[str, Any] | None:
    return store.update_job(
        job_id,
        queue_state="isolated" if isolated else "done",
        isolated=1 if isolated else 0,
        result_asset_id=result_asset_id,
        result_sha256=result_sha256,
        result_path=result_path,
        revision=revision,
        external_boundary="submitted",
        finished_at=now_iso(),
    )


def mark_failure(
    store: VisualStore, job_id: str, code: str, *, uncertain: bool = False
) -> dict[str, Any] | None:
    job = store.job(job_id)
    if job is None:
        return None
    boundary = str(job.get("external_boundary") or "")
    uncertain = uncertain or boundary in {"submit_started", "submitted"}
    return store.update_job(
        job_id,
        queue_state="outcome_uncertain" if uncertain else "failed",
        isolated=1 if uncertain else int(job.get("isolated") or 0),
        error_code=code,
        finished_at=now_iso(),
    )


def cancel_binding(
    store: VisualStore,
    job: dict[str, Any],
    code: str,
    *,
    refund: bool = False,
) -> dict[str, Any]:
    """取消尚未外部提交的任务；已提交边界的不退款、不重放。"""
    job_id = str(job.get("job_id") or "")
    updated = store.update_job(
        job_id,
        only_states=tuple(PENDING_STATES),
        queue_state="cancelled",
        error_code=code,
        isolated=1,
        finished_at=now_iso(),
    )
    result = updated or store.job(job_id) or job
    if refund and str(result.get("external_boundary")) == "not_submitted":
        from sirius_pulse.visual.policies import release_job_budget, release_job_grants

        release_job_budget(store, result)
        release_job_grants(store, result)
    return result
