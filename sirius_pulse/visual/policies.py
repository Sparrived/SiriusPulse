"""授权、配额与部署方管理策略。

PRD 要求把这些约束分开持久化，并且任何人都不能通过模型参数自行扩大额度：

- 用户 ``set_policy`` 限定自动候选；兼容旧 manual 记录不限制可见素材的手动共享；
- 部署方总额度、自动开关与保留期只由可信管理员通过管理入口设置；
- 自动候选的每日次数按人 1 次、按人格 3 次原子预占；未触达外部提交的取消才返还。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sirius_pulse.visual.access import (
    GRANT_MODES,
    GRANTABLE_CAPABILITIES,
    REVISION_SCOPES,
    _grant_expired,
    grant_covers_revision,
    scope_matches,
)
from sirius_pulse.visual.errors import VisualError
from sirius_pulse.visual.store import VisualStore, now_iso

DEFAULT_AUTO_PER_PERSON = 1
DEFAULT_AUTO_PER_PERSONA = 3

SETTING_TOTAL_QUOTA = "deployment_total_quota"
SETTING_USED = "deployment_used"
SETTING_AUTO_ENABLED = "deployment_auto_enabled"
SETTING_RETENTION_DAYS = "deployment_retention_days"
SETTING_AUTO_PER_PERSON = "auto_daily_per_person"
SETTING_AUTO_PER_PERSONA = "auto_daily_per_persona"


def today_local() -> str:
    """按运行时本地时区划分每日额度窗口。"""
    return date.today().isoformat()


def _idempotency(parts: list[str]) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _setting_int(store: VisualStore, key: str, default: int) -> int:
    value = store.setting(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def deployment_state(store: VisualStore) -> dict[str, Any]:
    total = store.setting(SETTING_TOTAL_QUOTA)
    used = _setting_int(store, SETTING_USED, 0)
    try:
        total_int = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_int = None
    return {
        "controller": str(store.setting("persona_visual_controller") or ""),
        "total_quota": total_int,
        "used": used,
        "remaining": None if total_int is None else max(0, total_int - used),
        "auto_enabled": bool(store.setting(SETTING_AUTO_ENABLED)),
        "retention_days": _setting_int(store, SETTING_RETENTION_DAYS, 0) or None,
        "auto_daily_per_person": _setting_int(
            store, SETTING_AUTO_PER_PERSON, DEFAULT_AUTO_PER_PERSON
        ),
        "auto_daily_per_persona": _setting_int(
            store, SETTING_AUTO_PER_PERSONA, DEFAULT_AUTO_PER_PERSONA
        ),
    }


def set_deployment_policy(
    store: VisualStore,
    access: Any,
    *,
    controller: str | None = None,
    total_quota: int | None = None,
    auto_enabled: bool | None = None,
    retention_days: int | None = None,
    per_person_daily: int | None = None,
    per_persona_daily: int | None = None,
    evidence_ref: str = "",
) -> dict[str, Any]:
    """部署方管理入口：只有可信管理员可调用，工具 Schema 不开放。"""
    if not bool(getattr(access, "is_admin", False)):
        raise VisualError("permission_denied", "只有可信应用管理员可以修改部署策略")
    if controller is not None:
        if not str(controller).strip():
            raise VisualError("invalid_argument", "controller 不能为空")
        store.set_setting(
            "persona_visual_controller", str(controller).strip(), actor_id=_actor(access)
        )
    if total_quota is not None:
        if int(total_quota) < 0:
            raise VisualError("invalid_argument", "total_quota 不能为负")
        store.set_setting(SETTING_TOTAL_QUOTA, int(total_quota), actor_id=_actor(access))
    if auto_enabled is not None:
        store.set_setting(SETTING_AUTO_ENABLED, bool(auto_enabled), actor_id=_actor(access))
    if retention_days is not None:
        if int(retention_days) < 0:
            raise VisualError("invalid_argument", "retention_days 不能为负")
        store.set_setting(SETTING_RETENTION_DAYS, int(retention_days), actor_id=_actor(access))
    if per_person_daily is not None:
        if int(per_person_daily) < 0:
            raise VisualError("invalid_argument", "per_person_daily 不能为负")
        store.set_setting(SETTING_AUTO_PER_PERSON, int(per_person_daily), actor_id=_actor(access))
    if per_persona_daily is not None:
        if int(per_persona_daily) < 0:
            raise VisualError("invalid_argument", "per_persona_daily 不能为负")
        store.set_setting(SETTING_AUTO_PER_PERSONA, int(per_persona_daily), actor_id=_actor(access))
    state = deployment_state(store)
    _audit(
        store,
        access,
        "set_deployment_policy",
        "persona",
        "",
        "",
        "",
        "",
        evidence_ref=evidence_ref,
        detail={
            "controller": controller,
            "total_quota": state["total_quota"],
            "auto_enabled": state["auto_enabled"],
            "retention_days": state["retention_days"],
        },
    )
    return {"status": "ok", "deployment": state}


def deployment_auto_ready(store: VisualStore) -> bool:
    state = deployment_state(store)
    if not state["auto_enabled"]:
        return False
    if state["total_quota"] is None:
        return False
    return int(state["remaining"] or 0) > 0


def consume_deployment(store: VisualStore) -> bool:
    """消费一次部署方生成额度；未配置总额度时不限制显式手动任务。"""
    total = store.setting(SETTING_TOTAL_QUOTA)
    try:
        total_int = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_int = None
    used = _setting_int(store, SETTING_USED, 0)
    if total_int is not None and used >= total_int:
        return False
    store.set_setting(SETTING_USED, used + 1, actor_id="visual_service")
    return True


def refund_deployment(store: VisualStore) -> None:
    used = _setting_int(store, SETTING_USED, 0)
    store.set_setting(SETTING_USED, max(0, used - 1), actor_id="visual_service")


def reserve_auto_budget(store: VisualStore, person_id: str, persona_id: str) -> bool:
    """原子预占每日自动额度 + 部署总额度。"""
    day = today_local()
    per_person = _setting_int(store, SETTING_AUTO_PER_PERSON, DEFAULT_AUTO_PER_PERSON)
    per_persona = _setting_int(store, SETTING_AUTO_PER_PERSONA, DEFAULT_AUTO_PER_PERSONA)
    if per_person <= 0 or per_persona <= 0:
        return False
    person_bucket = f"auto:person:{person_id}"
    persona_bucket = f"auto:persona:{persona_id}"
    if not store.reserve_quota(person_bucket, day, per_person):
        return False
    if not store.reserve_quota(persona_bucket, day, per_persona):
        store.release_quota(person_bucket, day)
        return False
    if not consume_deployment(store):
        store.release_quota(person_bucket, day)
        store.release_quota(persona_bucket, day)
        return False
    return True


def refund_auto_budget(store: VisualStore, person_id: str, persona_id: str) -> None:
    """只对可证明未提交上游的任务返还额度。"""
    day = today_local()
    store.release_quota(f"auto:person:{person_id}", day)
    store.release_quota(f"auto:persona:{persona_id}", day)
    refund_deployment(store)


def create_grant(
    store: VisualStore,
    access: Any,
    *,
    person_id: str,
    mode: str,
    capabilities: list[str],
    revision_scope: str = "",
    scope: str = "",
    expires_at: str = "",
    max_uses: int | None = None,
    recipe: str = "",
    recipe_version: str = "",
    source_revision: int | None = None,
    evidence_ref: str = "",
    grantee: str = "",
) -> dict[str, Any]:
    """记录维护者的自动候选预授权或旧手动许可；共享使用不依赖 manual 记录。"""
    mode = str(mode or "manual").strip()
    if mode not in GRANT_MODES:
        raise VisualError("invalid_argument", f"不支持的授权模式: {mode}")
    caps = sorted({str(cap).strip() for cap in capabilities if str(cap).strip()})
    unknown = set(caps) - set(GRANTABLE_CAPABILITIES)
    if unknown:
        raise VisualError(
            "invalid_argument",
            f"不支持授予的能力: {', '.join(sorted(unknown))}；正式启用/删除只能由本人或控制者操作",
        )
    if not caps:
        raise VisualError("invalid_argument", "capabilities 不能为空")
    revision_scope = str(revision_scope or "").strip()
    if not revision_scope:
        # PRD TOOL-04：默认只覆盖当前选定的来源修订；“以后新增的也覆盖”必须显式 future。
        revision_scope = "selected"
    if revision_scope not in REVISION_SCOPES:
        raise VisualError("invalid_argument", f"不支持的修订覆盖范围: {revision_scope}")
    if expires_at:
        try:
            datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise VisualError("invalid_argument", "expires_at 必须是 ISO 8601 时间") from exc
    if source_revision is None:
        source_revision = _active_revision(store, person_id)
    if mode == "auto_candidate":
        if "upload_generate" not in caps:
            raise VisualError("consent_required", "自动候选授权必须包含 upload_generate 能力")
        if revision_scope != "future" and source_revision is None:
            raise VisualError(
                "consent_required",
                "自动候选授权必须绑定具体来源修订；请先启用一张正式原图，或显式选择 future",
            )
        if not expires_at and max_uses is None:
            raise VisualError(
                "consent_required",
                "自动候选授权必须明确有效期（expires_at）或次数（max_uses），不默认无限授权",
            )
    scope = str(scope or "").strip()
    if not scope:
        group_id = str(getattr(access, "group_id", "") or "").strip()
        scope = f"group:{group_id}" if group_id else "persona"

    key = _idempotency(
        [
            "set_policy",
            person_id,
            mode,
            ",".join(caps),
            revision_scope,
            scope,
            expires_at,
            str(max_uses),
            str(source_revision),
            str(evidence_ref),
            grantee or _actor(access),
        ]
    )
    existing = store.grant_by_idempotency(key)
    if existing is not None:
        return {
            "status": "already_processed",
            "grant": grant_summary(existing),
            "grant_id": existing["grant_id"],
        }

    grant_id = f"vg_{uuid.uuid4().hex[:12]}"
    store.insert_grant(
        {
            "grant_id": grant_id,
            "person_id": person_id,
            "granted_by": _actor(access),
            "grantee_id": str(grantee or _actor(access)),
            "mode": mode,
            "capabilities": caps,
            "revision_scope": revision_scope,
            "source_revision": source_revision,
            "recipe": recipe,
            "recipe_version": recipe_version,
            "scope": scope,
            "expires_at": expires_at or None,
            "max_uses": None if max_uses is None else int(max_uses),
            "evidence_ref": evidence_ref,
            "idempotency_key": key,
            "created_at": now_iso(),
        }
    )
    row = store.grant(grant_id)
    assert row is not None
    _audit(
        store,
        access,
        "set_policy",
        "grant",
        grant_id,
        "",
        "",
        key,
        evidence_ref=evidence_ref,
        detail={"person_id": person_id, "mode": mode, "capabilities": caps, "scope": scope},
    )
    return {"status": "ok", "grant": grant_summary(row), "grant_id": grant_id}


def revoke_consent(
    store: VisualStore,
    access: Any,
    *,
    person_id: str,
    grant_id: str = "",
    capabilities: list[str] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """撤回许可：可撤指定 grant，也可从授权里摘掉单项能力。"""
    revoked: list[str] = []
    adjusted: list[str] = []
    if grant_id:
        row = store.grant(grant_id)
        if row is None or str(row.get("person_id")) != person_id:
            raise VisualError("not_found_or_not_visible", "授权不存在或不属于该人物")
        if store.revoke_grant(grant_id, reason):
            revoked.append(grant_id)
    else:
        wanted = {str(cap).strip() for cap in (capabilities or []) if str(cap).strip()}
        if not wanted:
            raise VisualError("invalid_argument", "撤回授权需要 grant_id 或明确的能力范围")
        for row in store.grants_for(person_id, status="active"):
            caps = [cap for cap in (row.get("capabilities") or [])]
            remaining = [cap for cap in caps if cap not in wanted]
            if len(remaining) == len(caps):
                continue
            if remaining:
                store.update_grant_capabilities(str(row["grant_id"]), remaining)
                adjusted.append(str(row["grant_id"]))
            elif store.revoke_grant(str(row["grant_id"]), reason or "撤回能力"):
                revoked.append(str(row["grant_id"]))
    _audit(
        store,
        access,
        "revoke_consent",
        "person",
        person_id,
        "",
        "",
        "",
        evidence_ref="",
        detail={"revoked": revoked, "adjusted": adjusted, "capabilities": capabilities or []},
    )
    return {
        "status": "ok",
        "revoked": revoked,
        "adjusted": adjusted,
        "grants": [grant_summary(row) for row in store.grants_for(person_id)],
    }


def grant_summary(row: dict[str, Any]) -> dict[str, Any]:
    """返回给模型/管理界面的授权摘要，不含内部路径。"""
    return {
        "grant_id": str(row.get("grant_id") or ""),
        "mode": str(row.get("mode") or ""),
        "capabilities": list(row.get("capabilities") or []),
        "revision_scope": str(row.get("revision_scope") or ""),
        "source_revision": row.get("source_revision"),
        "scope": str(row.get("scope") or ""),
        "expires_at": row.get("expires_at"),
        "max_uses": row.get("max_uses"),
        "used_count": int(row.get("used_count") or 0),
        "status": str(row.get("status") or ""),
        "recipe": str(row.get("recipe") or ""),
        "recipe_version": str(row.get("recipe_version") or ""),
        "created_at": row.get("created_at"),
        "grantee_id": str(row.get("grantee_id") or row.get("granted_by") or ""),
    }


def auto_grant_applies(row: dict[str, Any], revision: int, group_id: str) -> bool:
    """判断一条授权是否覆盖当前来源修订与作用域。

    只有显式 ``future`` 才覆盖授权之后的修订；``selected``／``current`` 都必须
    固定到具体来源修订，缺 ``source_revision`` 时不能被解释成“任意修订”。
    """
    if str(row.get("mode")) != "auto_candidate" or str(row.get("status")) != "active":
        return False
    if _grant_expired(row):
        return False
    max_uses = row.get("max_uses")
    if max_uses is not None and int(row.get("used_count") or 0) >= int(max_uses):
        return False
    if not scope_matches(str(row.get("scope") or ""), group_id):
        return False
    if not grant_covers_revision(row, revision):
        return False
    return "upload_generate" in (row.get("capabilities") or [])


def release_job_budget(store: VisualStore, job: dict[str, Any]) -> bool:
    """精确退还**该任务自己**预占的次数/总额度，同一任务只退一次。

    并发任务各自持有自己的预占；不能用“有人失败就减桶”那种写法，否则会退掉
    别人的额度。只有 manifest 明确记录本次预占时才会退还。
    """
    manifest = job.get("manifest") if isinstance(job.get("manifest"), dict) else {}
    if manifest.get("budget_refunded"):
        return False
    auto_reserved = bool(manifest.get("auto_reserved"))
    deployment_charged = bool(manifest.get("deployment_charged"))
    if not auto_reserved and not deployment_charged:
        return False
    if auto_reserved:
        refund_auto_budget(
            store,
            str(job.get("person_id") or ""),
            str(manifest.get("persona_id") or ""),
        )
    elif deployment_charged:
        refund_deployment(store)
    marked = dict(manifest)
    marked["budget_refunded"] = True
    store.update_job(str(job.get("job_id") or ""), manifest=json.dumps(marked, ensure_ascii=False))
    return True


def release_job_grants(store: VisualStore, job: dict[str, Any]) -> bool:
    """归还某任务在入队时预占的授权使用次数（同一任务只退一次）。"""
    manifest = job.get("manifest") if isinstance(job.get("manifest"), dict) else {}
    if manifest.get("grants_refunded"):
        return False
    grant_ids = [
        str(grant_id) for grant_id in (manifest.get("grants_consumed") or []) if str(grant_id)
    ]
    if not grant_ids:
        return False
    for grant_id in grant_ids:
        store.refund_grant(grant_id)
    marked = dict(manifest)
    marked["grants_refunded"] = True
    store.update_job(str(job.get("job_id") or ""), manifest=json.dumps(marked, ensure_ascii=False))
    return True


def _active_revision(store: VisualStore, person_id: str) -> int | None:
    """该人物当前启用的正式修订号；没有则返回 None。"""
    row = store.fetchone(
        "SELECT active_revision FROM vs_profile WHERE person_id=?"
        " AND active_revision IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
        (person_id,),
    )
    if row is None or row.get("active_revision") is None:
        return None
    return int(row["active_revision"])


def _actor(access: Any) -> str:
    return str(getattr(access, "actor_id", "") or "")


def _audit(
    store: VisualStore,
    access: Any,
    action: str,
    object_type: str,
    object_id: str,
    expected_revision: Any,
    result_revision: Any,
    key: str,
    *,
    evidence_ref: str,
    detail: dict[str, Any],
) -> None:
    store.insert_audit(
        {
            "audit_id": f"va_{uuid.uuid4().hex[:16]}",
            "actor_id": _actor(access),
            "action": action,
            "object_type": object_type,
            "object_id": object_id,
            "expected_revision": expected_revision,
            "result_revision": result_revision,
            "idempotency_key": key,
            "evidence_ref": evidence_ref,
            "detail": detail,
            "created_at": now_iso(),
        }
    )


def grant_expiry_from_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=int(days))).isoformat()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
