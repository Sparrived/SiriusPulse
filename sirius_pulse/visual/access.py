"""人物解析、证据校验与视觉能力授权。

PRD 的核心约束：账号确定身份，名称只用于称呼与候选；授权只在服务侧判断；
query 返回的 ID 或句柄不是 bearer token。本模块把所有这些判断收敛到一处，
供工具、后台控制器与管理 API 共用。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sirius_pulse.visual.errors import VisualError, as_visual_error
from sirius_pulse.visual.store import VisualStore

#: 共享素材使用能力；保留 grant 字段兼容旧任务与自动预授权，不转移维护权。
GRANTABLE_CAPABILITIES: frozenset[str] = frozenset(
    {"view", "vision_read", "upload_generate", "external_send"}
)

#: 允许出现在 set_policy 的自动化模式；不提供 auto_activate。
GRANT_MODES: frozenset[str] = frozenset({"manual", "auto_candidate"})

#: 修订覆盖范围：默认只覆盖当前选定来源。
REVISION_SCOPES: frozenset[str] = frozenset({"selected", "current", "future"})


def persona_id_of(people: Any) -> str:
    return str(getattr(people, "persona_id", "") or "")


def person_for(people: Any, person_id: str, access: Any) -> dict[str, Any]:
    """按权限读取人物；不可见与不存在统一外显，避免泄露隐藏人存在性。"""
    if not person_id:
        raise VisualError("not_found_or_not_visible", "没有指定人物")
    try:
        person = people.get_person(person_id, access)
    except Exception as exc:  # noqa: BLE001 - people 的异常带 code 属性
        raise as_visual_error(exc, "not_found_or_not_visible") from exc
    if not isinstance(person, dict) or not str(person.get("person_id") or ""):
        raise VisualError("not_found_or_not_visible", "人物不存在或不可见")
    return person


def resolve_person(people: Any, target: dict[str, Any], access: Any, source_ref: str) -> str:
    """把 target 引用解析成人物 UUID；不接受模型自造的 scope。"""
    if not isinstance(target, dict):
        raise VisualError("invalid_argument", "target 必须是单个主体引用")
    ref = str(target.get("ref") or "").strip()
    if (
        bool(getattr(access, "self_initiated", False))
        and str(target.get("ref") or "") == "requester"
    ):
        raise VisualError("permission_denied", "自主回合不能发起人物外部生成")
    if ref == "requester":
        refs = tuple(getattr(access, "evidence_refs", ()) or ())
        chosen = str(source_ref or (refs[0] if len(refs) == 1 else ""))
        require_evidence(people, access, chosen)
    if ref not in {"requester", "persona", "person_id", "name", "mention_ref"}:
        raise VisualError("invalid_argument", f"不支持的 target.ref: {ref or '(空)'}")
    if ref not in {"requester", "persona"} and not str(target.get("value") or "").strip():
        raise VisualError("invalid_argument", f"target.ref={ref} 需要 value")
    try:
        resolved = people.resolve_target(target, access, source_ref)
    except Exception as exc:  # noqa: BLE001
        raise as_visual_error(exc, "not_found_or_not_visible") from exc
    person_id = str(resolved or "").strip()
    if not person_id:
        raise VisualError("not_found_or_not_visible", "人物不存在或不可见")
    return person_id


def require_evidence(people: Any, access: Any, evidence_ref: str) -> dict[str, Any]:
    """校验证据确属本回合真实待处理指令，并返回真实作者信息。

    ``people.require_write`` 负责「这条证据能不能作为当前操作依据」；这里再确认
    证据作者就是操作者本人，防止模型拿别人的历史消息冒充同意。
    """
    evidence_ref = str(evidence_ref or "").strip()
    actor_id = str(getattr(access, "actor_id", "") or "").strip()
    if not evidence_ref:
        raise VisualError("evidence_invalid", "缺少本回合来源证据（evidence_ref）")
    if not actor_id:
        raise VisualError("permission_denied", "没有可验证的账号身份，不能执行正式变更")
    try:
        evidence = people.require_write(actor_id, access, evidence_ref)
    except Exception as exc:  # noqa: BLE001
        raise as_visual_error(exc, "evidence_invalid") from exc
    if not isinstance(evidence, dict):
        raise VisualError("evidence_invalid", "来源证据不可验证")
    author = str(evidence.get("actor_id") or "").strip()
    if author and author != actor_id:
        raise VisualError("evidence_invalid", "证据作者不是本次操作者")
    return evidence


def require_evidence_or_admin(people: Any, access: Any, evidence_ref: str) -> dict[str, Any]:
    """管理 API 可由可信管理员省略证据；普通工具路径永远需要真实证据。"""
    if not str(evidence_ref or "").strip() and bool(getattr(access, "is_admin", False)):
        return {"actor_id": str(getattr(access, "actor_id", "") or ""), "admin": True}
    return require_evidence(people, access, evidence_ref)


def is_persona(people: Any, person_id: str) -> bool:
    return bool(person_id) and person_id == persona_id_of(people)


def is_self(access: Any, person_id: str) -> bool:
    actor = str(getattr(access, "actor_id", "") or "")
    return bool(actor) and actor == person_id


def person_kind(person: dict[str, Any]) -> str:
    return str(person.get("kind") or "human").strip().lower()


def controller_of(store: VisualStore, person: dict[str, Any]) -> str:
    """人格采用可信部署配置；角色采用已登记的控制关系。"""
    person_id = str(person.get("person_id") or "")
    if person_kind(person) == "persona":
        return str(store.setting("persona_visual_controller") or "")
    return str(person.get("controller_id") or store.control(person_id) or "")


def can_maintain(store: VisualStore, people: Any, person: dict[str, Any], access: Any) -> bool:
    """是否拥有正式资料/形象的控制权（启用、授权、停用、删除）。"""
    person_id = str(person.get("person_id") or "")
    if is_self(access, person_id):
        return True
    if is_persona(people, person_id) and bool(getattr(access, "is_admin", False)):
        return True
    controller = controller_of(store, person)
    return bool(controller) and controller == str(getattr(access, "actor_id", "") or "")


def scope_matches(grant_scope: str, group_id: str) -> bool:
    scope = str(grant_scope or "").strip()
    if not scope or scope in {"persona", "global"}:
        return True
    if scope == f"group:{group_id}":
        return True
    return scope == str(group_id or "")


def _grant_expired(grant: dict[str, Any]) -> bool:
    expires_at = str(grant.get("expires_at") or "").strip()
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= datetime.now(timezone.utc)


def active_grants_with(
    store: VisualStore, person_id: str, capability: str, group_id: str
) -> list[dict[str, Any]]:
    """当前仍有效、覆盖该能力与作用域的授权。"""
    usable: list[dict[str, Any]] = []
    for grant in store.grants_for(person_id, status="active"):
        if capability not in (grant.get("capabilities") or []):
            continue
        if _grant_expired(grant):
            continue
        max_uses = grant.get("max_uses")
        if max_uses is not None and int(grant.get("used_count") or 0) >= int(max_uses):
            continue
        if not scope_matches(str(grant.get("scope") or ""), group_id):
            continue
        if not store.grantee_for(grant):
            continue
        usable.append(grant)
    return usable


def authorize(
    store: VisualStore,
    people: Any,
    person: dict[str, Any],
    access: Any,
    capability: str,
    *,
    asset: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """判断操作者能否对某人物资产执行指定能力。

    Returns:
        ``(allowed, code)``；拒绝时 code 是给模型的安全错误码。
    """
    allowed, code, _grant = authorize_with_grant(
        store, people, person, access, capability, asset=asset
    )
    return allowed, code


def authorize_with_grant(
    store: VisualStore,
    people: Any,
    person: dict[str, Any],
    access: Any,
    capability: str,
    *,
    asset: dict[str, Any] | None = None,
    revision: int | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """人格内可见素材共享使用，不消耗逐人物许可；维护权仍独立校验。

    人物与档案作用域由调用方解析，资产状态在此复核。保留返回形状以兼容旧入口；
    自动候选的限定预授权由 policies/lifecycle 单独执行，不能从共享使用推导。
    """
    person_id = str(person.get("person_id") or "")
    if bool(getattr(access, "self_initiated", False)):
        return False, "permission_denied", None
    if not person_id or store.subject_deleted(person_id):
        return False, "not_found_or_not_visible", None
    if asset is not None and str(asset.get("status") or "") != "available":
        return False, "consent_required", None
    if capability in GRANTABLE_CAPABILITIES:
        return True, "", None
    if capability in {"activate", "delete"} and can_maintain(store, people, person, access):
        return True, "", None
    return False, "permission_denied", None


def grant_covers_revision(grant: dict[str, Any] | None, revision: int | None) -> bool:
    """授权是否覆盖指定的视觉修订。

    ``future`` 是显式选择“以后新增的修订也覆盖”；其余模式必须固定来源修订，
    没有 ``source_revision`` 的授权不能解释成“任意修订”（PRD AUTO-01／TOOL-04）。
    """
    if grant is None:
        return False
    if str(grant.get("revision_scope") or "selected") == "future":
        return True
    pinned = grant.get("source_revision")
    if pinned is None or revision is None:
        return False
    return int(pinned) == int(revision)


def require_capability(
    store: VisualStore,
    people: Any,
    person: dict[str, Any],
    access: Any,
    capability: str,
    *,
    asset: dict[str, Any] | None = None,
    revision: int | None = None,
) -> dict[str, Any] | None:
    """校验共享使用或维护能力；普通素材使用返回 None，不占用自动候选许可。"""
    allowed, code, grant = authorize_with_grant(
        store, people, person, access, capability, asset=asset, revision=revision
    )
    if not allowed:
        label = {
            "view": "查看该人物视觉资料",
            "vision_read": "把该人物的图片送入视觉模型",
            "upload_generate": "把该人物的图片上传到生成服务",
            "external_send": "在聊天中发送该人物图片",
        }.get(capability, capability)
        raise VisualError(code, f"无法{label}；请检查素材状态、可见范围或维护权限")
    return grant
