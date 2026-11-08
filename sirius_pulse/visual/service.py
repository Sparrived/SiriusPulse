"""视觉资产领域服务：授权、不可变修订、参考集、编译、派生与清理。

工具、后台控制器与管理 API 共用这一份服务与同一个 SQLite 真相（PRD ARCH-01）。
所有 public 方法都是 ``async``，便于工具层统一 await；内部重活是本地 SQLite 与
文件操作，不发起网络请求。真正的生图仍走现有 ``image_generate`` 队列与 AMKR。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sirius_pulse.visual import compiler, lifecycle, policies, retention
from sirius_pulse.visual.access import (
    authorize_with_grant,
    can_maintain,
    controller_of,
    is_persona,
    person_for,
    persona_id_of,
    require_capability,
    require_evidence,
    require_evidence_or_admin,
    resolve_person,
)
from sirius_pulse.visual.errors import VisualError, as_visual_error
from sirius_pulse.visual.images import (
    MAX_IMAGE_BYTES,
    atomic_write_bytes,
    inspect_image,
    is_within,
    relative_asset_path,
    safe_segment,
    sha256_bytes,
    sniff_mime,
)
from sirius_pulse.visual.store import VisualStore, now_iso

logger = logging.getLogger(__name__)

_VISUAL_CAPABILITIES = {
    "view",
    "vision_read",
    "upload_generate",
    "external_send",
    "activate",
    "delete",
}
_MAINTAIN_ACTIONS = {
    "create_character",
    "set_policy",
    "revoke_consent",
    "retire",
    "assign_draft",
}
_TERMINAL_JOB_STATES = lifecycle.TERMINAL_STATES


class VisualService:
    """人物视觉资产的唯一领域服务。"""

    def __init__(
        self,
        work_path: str | Path,
        *,
        people: Any,
        conn: Any = None,
    ) -> None:
        self.work_path = Path(work_path)
        self.people = people
        self.store = VisualStore(self.work_path, conn=conn)
        self.assets_root = self.work_path / "visual_assets"
        self.assets_root.mkdir(parents=True, exist_ok=True)
        self.image_artifacts = self.work_path / "tool_data" / "artifacts" / "image_generate"
        self.read_image_artifacts = self.work_path / "tool_data" / "artifacts" / "read_image"
        # 与 bash/read_image/group_file_exec 的 ``~`` 语义保持一致：人格自己的持久 home。
        # 这些工具以 ``tool_data`` 的上一级为 runtime root，home 就是 ``<work>/home``。
        self.persona_home = self.work_path / "home"

    # ==================================================================
    # 公共查询
    # ==================================================================
    async def get_summary(
        self, person_id: str, access: Any, detail: str = "summary"
    ) -> dict[str, Any]:
        """人物视觉概况（授权可见的档案／授权／任务摘要）。"""
        try:
            person = person_for(self.people, person_id, access)
            maintainer = can_maintain(self.store, self.people, person, access)
            public_persona = is_persona(self.people, person_id)
            if self.store.subject_deleted(person_id):
                return {
                    "status": "found",
                    "person_id": person_id,
                    "restricted": True,
                    "visual": {
                        "state": "missing",
                        "profiles": [],
                        "active": [],
                        "candidates": [],
                        "drafts": [],
                    },
                    "missing_sections": ["active", "candidates", "drafts", "consents", "jobs"],
                }
            can_view = authorize_with_grant(self.store, self.people, person, access, "view")[0]
            visible = maintainer or public_persona or can_view
            profiles = [
                profile
                for profile in self.store.profiles_for(person_id)
                if str(profile.get("person_id"))
            ]
            if not visible:
                return {
                    "status": "found",
                    "person_id": person_id,
                    "display_name": str(person.get("display_name") or ""),
                    "kind": str(person.get("kind") or "human"),
                    "person_revision": person.get("revision"),
                    "visual": {"state": "restricted"},
                    "missing_sections": ["active", "candidates", "consents", "jobs"],
                    "restricted": True,
                }
            # 共享使用与维护权都不扩大来源会话范围。
            profiles = [profile for profile in profiles if self._profile_visible(profile, access)]
            active_entries = self._active_entries(person, profiles, access, detail)
            candidates = self._pending_entries(person, access, detail, status="candidate")
            drafts = self._pending_entries(person, access, detail, status="draft")
            result: dict[str, Any] = {
                "status": "found",
                "person_id": person_id,
                "display_name": str(person.get("display_name") or ""),
                "kind": str(person.get("kind") or "human"),
                "person_revision": person.get("revision"),
                "visual": {
                    "state": "active"
                    if active_entries
                    else (
                        "candidate_only" if candidates else "draft_only" if drafts else "missing"
                    ),
                    "profiles": [
                        {
                            "profile_id": str(profile["profile_id"]),
                            "purpose": str(profile.get("purpose") or ""),
                            "scope": str(profile.get("scope") or ""),
                            "active_revision": profile.get("active_revision"),
                        }
                        for profile in profiles
                    ],
                    "active": active_entries,
                    "candidates": candidates,
                    "drafts": drafts,
                },
                "missing_sections": [],
                "restricted": not visible,
            }
            if visible and maintainer:
                result["consents"] = [
                    policies.grant_summary(row) for row in self.store.grants_for(person_id)[:10]
                ]
                result["jobs"] = [
                    self._job_summary(job) for job in self.store.jobs_for(person_id, limit=5)
                ]
            else:
                result["missing_sections"].extend(["consents", "jobs"])
            return result
        except VisualError as exc:
            return exc.to_result()

    def _job_summary(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": str(job.get("job_id") or ""),
            "purpose": str(job.get("purpose") or ""),
            "state": str(job.get("queue_state") or ""),
            "source_revision": job.get("source_revision"),
            "result_revision": job.get("revision"),
            "result_asset_id": str(job.get("result_asset_id") or ""),
            "isolated": bool(int(job.get("isolated") or 0)),
            "error_code": str(job.get("error_code") or ""),
            "created_at": job.get("created_at"),
        }

    def _active_entries(
        self,
        person: dict[str, Any],
        profiles: list[dict[str, Any]],
        access: Any,
        detail: str,
    ) -> list[dict[str, Any]]:
        limit = 500 if detail == "detail" else 200
        entries: list[dict[str, Any]] = []
        for profile in profiles:
            revision_number = profile.get("active_revision")
            if revision_number is None:
                continue
            revision = self.store.revision(str(profile["profile_id"]), int(revision_number))
            if revision is None or str(revision.get("status")) in {"retired", "rejected"}:
                continue
            if not authorize_with_grant(
                self.store, self.people, person, access, "view", revision=int(revision_number)
            )[0]:
                continue
            entries.append(
                self._revision_summary(profile, revision, limit=limit, include_refs=True)
            )
        return entries

    def _pending_entries(
        self,
        person: dict[str, Any],
        access: Any,
        detail: str,
        *,
        status: str,
    ) -> list[dict[str, Any]]:
        limit = 500 if detail == "detail" else 200
        rows = self.store.revisions_for(person["person_id"], statuses=(status,), limit=20)
        result: list[dict[str, Any]] = []
        for revision in rows:
            profile = self.store.profile(str(revision["profile_id"]))
            if profile is None:
                continue
            if not self._profile_visible(profile, access):
                continue
            if not authorize_with_grant(
                self.store, self.people, person, access, "view", revision=int(revision["revision"])
            )[0]:
                continue
            result.append(
                self._revision_summary(profile, revision, limit=limit, include_refs=False)
            )
            if len(result) >= 5:
                break
        return result

    def _revision_summary(
        self,
        profile: dict[str, Any],
        revision: dict[str, Any],
        *,
        limit: int,
        include_refs: bool,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "profile_id": str(profile["profile_id"]),
            "revision": int(revision["revision"]),
            "status": str(revision.get("status") or ""),
            "origin": str(revision.get("origin") or ""),
            "purpose": str(profile.get("purpose") or ""),
            "scope": str(profile.get("scope") or ""),
            "identity_description": compiler.clean_text(
                revision.get("identity_description"), limit
            ),
            "default_outfit": compiler.clean_text(revision.get("default_outfit"), limit),
            "variant_notes": compiler.clean_text(revision.get("variant_notes"), limit),
            "inferred_details": compiler.clean_text(revision.get("inferred_details"), limit),
            "reference_set_id": str(revision.get("set_id") or ""),
        }
        if include_refs:
            entry["references"] = self._reference_summaries(str(revision.get("set_id") or ""))
        return entry

    def _reference_summaries(self, set_id: str) -> list[dict[str, Any]]:
        if not set_id:
            return []
        summaries: list[dict[str, Any]] = []
        for item in self.store.reference_items(set_id):
            asset = self.store.asset(str(item.get("asset_id") or ""))
            summaries.append(
                {
                    "index": int(item["idx"]),
                    "asset_id": str(item.get("asset_id") or ""),
                    "person_id": str(item.get("person_id") or ""),
                    "purpose": str(item.get("purpose") or ""),
                    "view": str(item.get("view") or ""),
                    "required": bool(int(item.get("required") or 0)),
                    "sha256_prefix": str(item.get("sha256") or "")[:12],
                    "available": bool(asset and str(asset.get("status")) == "available"),
                }
            )
        return summaries

    # ==================================================================
    # 动作入口
    # ==================================================================
    async def manage(
        self,
        action: str,
        *,
        access: Any,
        evidence_ref: str = "",
        source_ref: str = "",
        target: dict[str, Any] | None = None,
        queue: Any = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """薄工具与管理 API 共用的动作入口。"""
        action = str(action or "").strip()
        try:
            handler = getattr(self, f"_action_{action}", None)
            if handler is None or action.startswith("_"):
                raise VisualError("unsupported_action", f"不支持的视觉资产动作: {action}")
            if bool(getattr(access, "self_initiated", False)) and action not in {"get_summary"}:
                if not (
                    bool(getattr(access, "self_service", False))
                    and action in {"create_character", "stage", "derive", "activate"}
                ):
                    raise VisualError(
                        "permission_denied",
                        "自主回合不能修改正式视觉资产；请在用户明确指令下操作",
                    )
            return await handler(
                access=access,
                evidence_ref=evidence_ref,
                source_ref=source_ref,
                target=target,
                queue=queue,
                fields=fields,
            )
        except VisualError as exc:
            return exc.to_result()
        except Exception as exc:  # noqa: BLE001 - 不把内部堆栈/路径漏给模型
            logger.exception("视觉资产动作失败: %s", action)
            return as_visual_error(exc).to_result()

    async def _action_create_character(
        self,
        *,
        access: Any,
        evidence_ref: str,
        source_ref: str,
        target: dict[str, Any] | None,
        queue: Any,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        del source_ref, target, queue
        name = compiler.clean_text(fields.get("name"), 80)
        if not name:
            raise VisualError("invalid_argument", "创建角色需要明确名称")
        if not getattr(access, "self_service", False):
            require_evidence(self.people, access, evidence_ref)
        key = _hash_parts(
            ["create_character", str(getattr(access, "actor_id", "")), evidence_ref, name]
        )
        existing = self.store.audit_by_idempotency(key)
        if existing is not None:
            detail = _audit_detail(existing)
            return {
                "success": True,
                "status": "already_processed",
                "code": "already_processed",
                "change_id": str(existing["audit_id"]),
                "data": detail,
            }
        try:
            created = self.people.create_character(name, access, evidence_ref)
        except Exception as exc:  # noqa: BLE001
            raise as_visual_error(exc, "permission_denied") from exc
        person_id = str((created or {}).get("person_id") or "").strip()
        if not person_id:
            raise VisualError("invalid_argument", "人物服务没有返回角色 UUID")
        actor_id = str(getattr(access, "actor_id", "") or "")
        if actor_id:
            self.store.set_control(person_id, actor_id)
        detail = {
            "person_id": person_id,
            "display_name": str((created or {}).get("display_name") or name),
            "kind": "character",
            "controller_id": actor_id,
            "self_unchanged": True,
        }
        audit_id = _insert_audit(
            self.store, access, "create_character", "person", person_id, key, evidence_ref, detail
        )
        return {
            "success": True,
            "status": "ok",
            "code": "",
            "change_id": audit_id,
            "summary": f"已登记虚构角色「{name}」；本人身份与默认形象不变",
            "data": detail,
        }

    async def _action_stage(
        self,
        *,
        access: Any,
        evidence_ref: str,
        source_ref: str,
        target: dict[str, Any] | None,
        queue: Any,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        del queue
        target = target or {"ref": "requester"}
        person_id = resolve_person(self.people, target, access, source_ref or evidence_ref)
        person = person_for(self.people, person_id, access)
        self.store.require_subject_active(person_id)
        if getattr(access, "self_service", False):
            evidence = {
                "actor_id": str(getattr(access, "actor_id", "")),
                "attachments": [],
                "self_service": True,
            }
        else:
            evidence = require_evidence(self.people, access, evidence_ref)
        attachments = evidence.get("attachments")
        if not attachments:
            try:
                full_evidence = self.people.get_evidence(evidence_ref)
            except Exception:  # noqa: BLE001 - 证据主体已校验，附件只是补充信息
                full_evidence = None
            if isinstance(full_evidence, dict):
                attachments = full_evidence.get("attachments")
        source = fields.get("source")
        data, source_meta = self._load_source(
            source,
            access=access,
            evidence_ref=evidence_ref,
            attachments=attachments,
        )
        info = inspect_image(data, max_bytes=MAX_IMAGE_BYTES)
        purpose = str(fields.get("purpose") or "identity").strip() or "identity"
        if purpose not in {"identity", "outfit", "pose", "style", "scene"}:
            raise VisualError("invalid_argument", f"不支持的用途: {purpose}")
        view = compiler.clean_text(fields.get("view"), 40)
        scope = str(fields.get("scope") or "").strip()
        default_scope = self._default_scope(person, access)
        if scope and scope != default_scope:
            if not bool(getattr(access, "is_admin", False)):
                raise VisualError("permission_denied", "扩大形象共享范围需要管理授权")
            if scope != "persona":
                raise VisualError("invalid_argument", "共享范围只能是当前会话或人格")
        activate = bool(fields.get("activate"))
        maintainer = can_maintain(self.store, self.people, person, access)

        scope = scope or default_scope
        # 投稿者与目标人物独立；绑定只新增草稿，不赋予维护权或改写正式指针。
        profile = self._profile_for_stage(person_id, purpose, scope, fields.get("profile_id"))
        key = _hash_parts(
            [
                "stage",
                person_id,
                str(getattr(access, "actor_id", "")),
                evidence_ref,
                info["sha256"],
                purpose,
                scope,
                view,
                "1" if activate else "0",
            ]
        )
        existing = self.store.audit_by_idempotency(key)
        if existing is not None:
            return {
                "success": True,
                "status": "already_processed",
                "code": "already_processed",
                "change_id": str(existing["audit_id"]),
                "data": _audit_detail(existing),
            }

        previous_active = profile.get("active_revision")
        with self.store.transaction():
            revision_number = self.store.bump_revision(str(profile["profile_id"]))
            asset = self._save_asset(
                data,
                info,
                person_id=person_id,
                access=access,
                controller_id=controller_of(self.store, person),
                purpose=purpose,
                view=view,
                origin="original",
                source_ref=evidence_ref,
                segment=person_id,
                parent_asset_id=str(source_meta.get("asset_id") or "") or None,
            )
            set_id = f"rs_{uuid.uuid4().hex[:12]}"
            self.store.create_reference_set(
                set_id=set_id,
                profile_id=str(profile["profile_id"]),
                revision=revision_number,
                person_id=asset["person_id"],
                purpose=purpose,
            )
            self.store.add_reference_item(
                set_id,
                {
                    "idx": 1,
                    "asset_id": asset["asset_id"],
                    "person_id": asset["person_id"],
                    "revision": revision_number,
                    "purpose": purpose,
                    "view": view,
                    "required": True,
                    "sha256": asset["sha256"],
                },
            )
            self.store.insert_revision(
                {
                    "profile_id": profile["profile_id"],
                    "revision": revision_number,
                    "person_id": asset["person_id"],
                    "status": "draft",
                    "origin": "upload",
                    "identity_description": compiler.clean_text(
                        fields.get("identity_description"), 800
                    ),
                    "default_outfit": compiler.clean_text(fields.get("default_outfit"), 800),
                    "variant_notes": compiler.clean_text(fields.get("variant_notes"), 800),
                    "inferred_details": compiler.clean_text(fields.get("inferred_details"), 800),
                    "parent_revision": None,
                    "set_id": set_id,
                    "created_by": str(getattr(access, "actor_id", "") or ""),
                    "created_at": now_iso(),
                }
            )
            detail: dict[str, Any] = {
                "profile_id": str(profile["profile_id"]),
                "revision": revision_number,
                "asset_id": asset["asset_id"],
                "reference_set_id": set_id,
                "sha256": info["sha256"],
                "status": "draft",
                "scope": scope,
                "person_id": person_id,
                "unbound": False,
                "source": source_meta,
            }
            audit_id = _insert_audit(
                self.store,
                access,
                "stage",
                "revision",
                f"{profile['profile_id']}:{revision_number}",
                key,
                evidence_ref,
                detail,
            )

        activation: dict[str, Any] | None = None
        if activate and not maintainer:
            activation = {
                "success": False,
                "status": "confirmation_required",
                "code": "permission_denied",
                "summary": "已绑定目标并保存草稿；投稿不会授予启用权，请由有维护权的人确认",
            }
        if maintainer and activate:
            activation = await self._activate_revision(
                person=person,
                profile_id=str(profile["profile_id"]),
                revision=revision_number,
                expected_revision=previous_active,
                access=access,
                evidence_ref=evidence_ref,
                idempotency_seed=key,
            )
            if activation.get("success"):
                detail["status"] = "active"
            else:
                detail["status"] = "draft"
        return {
            "success": True,
            "status": detail["status"],
            "code": "",
            "change_id": audit_id,
            "summary": (
                f"原图已安全保存为修订 v{revision_number}"
                + ("并启用为正式形象" if detail["status"] == "active" else "（草稿，未启用）")
            ),
            "data": {**detail, "activation": activation},
        }

    async def _action_derive(
        self,
        *,
        access: Any,
        evidence_ref: str,
        source_ref: str,
        target: dict[str, Any] | None,
        queue: Any,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        del source_ref
        target = target or {"ref": "requester"}
        person_id = resolve_person(self.people, target, access, evidence_ref)
        person = person_for(self.people, person_id, access)
        auto = bool(fields.get("auto"))
        if auto:
            # 后台使用已登记的限定预授权，不伪装为新的用户会话指令。
            access = _Access(
                actor_id=str(getattr(access, "actor_id", "")),
                group_id=str(getattr(access, "group_id", "")),
            )
        persona_id = persona_id_of(self.people)
        grant_id = str(fields.get("grant_id") or "")
        redo = bool(fields.get("redo"))

        profile, revision = self._select_source_revision(person_id, access, fields)
        status = str(revision.get("status") or "")
        if status not in {"active", "superseded"}:
            raise VisualError(
                "consent_required",
                "派生来源必须是已确认的正式或历史修订；未确认候选不能反复作为派生输入",
            )
        self.store.require_subject_active(person_id)
        require_capability(
            self.store,
            self.people,
            person,
            access,
            "upload_generate",
            revision=int(revision["revision"]),
        )
        items = self._revision_reference_items(revision)
        subject = self._subject_from_revision(person, profile, revision, {}, fields)
        prompt = compiler.build_turnaround_prompt(subject, len(items))
        input_hash = _hash_parts([prompt] + [str(item.get("sha256") or "") for item in items])
        idem_key = _hash_parts(
            [
                "derive",
                person_id,
                str(profile["profile_id"]),
                str(revision["revision"]),
                input_hash,
                compiler.TURNAROUND_RECIPE,
                compiler.TURNAROUND_RECIPE_VERSION,
            ]
        )

        self.store.require_subject_active(person_id)
        if not redo:
            existing = self.store.job_by_idempotency(idem_key)
            if existing is not None:
                state = str(existing.get("queue_state"))
                if state not in _TERMINAL_JOB_STATES:
                    return {
                        "success": True,
                        "status": "already_pending",
                        "code": "already_pending",
                        "summary": "同源派生任务已在进行中，不需要重复提交",
                        "data": {
                            "job_id": str(existing["job_id"]),
                            "state": state,
                        },
                    }
                if state == "done" and existing.get("result_asset_id"):
                    return {
                        "success": True,
                        "status": "candidate_exists",
                        "code": "already_processed",
                        "summary": "同源候选已经生成；如确需重做请明确指定 redo",
                        "data": self._job_summary(existing),
                    }
                # 失败/未知/已隔离/已取消：重放前不消耗任何额度或授权次数；
                # 未知结果尤其不能自动重扣费，必须由用户明确 redo。
                code = "outcome_uncertain" if state == "outcome_uncertain" else "already_processed"
                message = (
                    "同源任务的上游结果未知，不会自动重试付费操作；如确需重做请明确 redo"
                    if code == "outcome_uncertain"
                    else f"同源任务已结束（{state}），不会自动重放；如确需重做请明确 redo"
                )
                raise VisualError(
                    code,
                    message,
                    data={
                        "job_id": str(existing["job_id"]),
                        "state": state,
                        "error_code": str(existing.get("error_code") or ""),
                    },
                )

        if not items:
            raise VisualError("required_asset_missing", "该正式形象没有可用参考图")

        if auto:
            if grant_id:
                grant_row = self.store.grant(grant_id)
                if grant_row is None or str(grant_row.get("person_id")) != person_id:
                    raise VisualError("permission_denied", "指定的自动候选授权不属于该人物")
                if self.store.grantee_for(grant_row) != str(getattr(access, "actor_id", "")):
                    raise VisualError("permission_denied", "自动候选授权不属于当前执行者")
                if not policies.auto_grant_applies(
                    grant_row,
                    int(revision["revision"]),
                    str(getattr(access, "group_id", "") or ""),
                ):
                    raise VisualError("consent_required", "该自动候选授权未覆盖当前来源修订或已失效")
            else:
                grant_id = self._auto_grant_id(person_id, int(revision["revision"]), access)
            if not grant_id:
                raise VisualError("consent_required", "没有覆盖该来源修订的有效自动候选授权")
            if not policies.deployment_auto_ready(self.store):
                raise VisualError("quota_exceeded", "部署方额度未开启或已用尽，不能自动生成")
            if not policies.reserve_auto_budget(self.store, person_id, persona_id):
                raise VisualError("quota_exceeded", "自动派生次数已达今日上限")
            if not self.store.consume_grant(grant_id):
                policies.refund_auto_budget(self.store, person_id, persona_id)
                raise VisualError("quota_exceeded", "该授权次数已用尽或被撤回")
            budget_flags = {"auto_reserved": True, "deployment_charged": False}
        else:
            if not getattr(access, "self_service", False):
                require_evidence(self.people, access, evidence_ref)
            # 手动共享使用只计部署额度，不占用后台自动候选的限定许可。
            grant_id = ""
            if not policies.consume_deployment(self.store):
                raise VisualError("quota_exceeded", "部署方生成总额度已用尽")
            budget_flags = {"auto_reserved": False, "deployment_charged": True}

        try:
            images: list[dict[str, Any]] = []
            for item in items:
                asset = self.store.asset(str(item["asset_id"]))
                data = self._read_asset_bytes(asset)
                if len(data) > MAX_IMAGE_BYTES:
                    raise VisualError("asset_invalid", "参考图超过单张 20MB 限制")
                images.append(
                    {
                        "name": _reference_name(item["idx"], str(item["asset_id"]), asset["mime"]),
                        "bytes": data,
                        "sha256": str(item.get("sha256") or sha256_bytes(data)),
                        "asset_id": str(item["asset_id"]),
                        "person_id": str(item.get("person_id") or ""),
                        "profile_id": str(profile["profile_id"]),
                        "revision": int(item.get("revision") or revision["revision"]),
                        "purpose": str(item.get("purpose") or "identity"),
                        "view": str(item.get("view") or ""),
                        "required": bool(int(item.get("required") or 0)),
                    }
                )
        except Exception:
            if auto:
                policies.refund_auto_budget(self.store, person_id, persona_id)
            else:
                policies.refund_deployment(self.store)
            if grant_id:
                self.store.refund_grant(grant_id)
            raise
        attempt = 1
        if redo:
            attempt = int(fields.get("attempt") or 1)
            idem_key = _hash_parts([idem_key, "redo", str(uuid.uuid4().hex)])
        job_id = f"img_{uuid.uuid4().hex[:8]}"
        manifest = {
            "purpose": "turnaround_derive",
            "persona_id": persona_id,
            "auto": auto,
            "source_ref": evidence_ref,
            "grant_id": grant_id,
            "capability": "upload_generate",
            "grants_consumed": [grant_id] if grant_id else [],
            "grants_reserved": [grant_id] if grant_id else [],
            "recipe": compiler.TURNAROUND_RECIPE,
            "recipe_version": compiler.TURNAROUND_RECIPE_VERSION,
            **budget_flags,
            "subjects": [
                {
                    "person_id": person_id,
                    "display_name": str(person.get("display_name") or ""),
                    "profile_id": str(profile["profile_id"]),
                    "revision": int(revision["revision"]),
                    "capability": "upload_generate",
                    "grant_id": grant_id,
                }
            ],
            "references": [
                {
                    "index": index,
                    "asset_id": image["asset_id"],
                    "person_id": image["person_id"],
                    "profile_id": image["profile_id"],
                    "revision": image["revision"],
                    "purpose": image["purpose"],
                    "view": image["view"],
                    "sha256": image["sha256"],
                }
                for index, image in enumerate(images, start=1)
            ],
        }
        try:
            lifecycle.begin_binding(
                self.store,
                job_id=job_id,
                purpose="turnaround_derive",
                actor_id=str(getattr(access, "actor_id", "") or ""),
                group_id=str(getattr(access, "group_id", "") or ""),
                prompt=prompt,
                input_hash=input_hash,
                manifest=manifest,
                person_id=person_id,
                profile_id=str(profile["profile_id"]),
                revision=None,
                source_revision=int(revision["revision"]),
                source_hash=str(revision.get("input_hash") or ""),
                grant_id=grant_id,
                recipe=compiler.TURNAROUND_RECIPE,
                recipe_version=compiler.TURNAROUND_RECIPE_VERSION,
                attempt=attempt,
                idempotency_key=idem_key,
            )
        except Exception:
            # 预占已发生但任务没能落盘：只回滚本次预占，不碰别人的额度。
            if auto:
                policies.refund_auto_budget(self.store, person_id, persona_id)
            else:
                policies.refund_deployment(self.store)
            if grant_id:
                self.store.refund_grant(grant_id)
            raise
        if queue is None:
            _cancel_and_refund(self.store, job_id, "queue_unavailable")
            raise VisualError("queue_unavailable", "生图队列未接入，无法提交派生任务")
        try:
            queue.enqueue_visual(
                job_id=job_id,
                prompt=prompt,
                images=images,
                meta={
                    "group_id": str(getattr(access, "group_id", "") or ""),
                    "actor_id": str(getattr(access, "actor_id", "") or ""),
                    "visual": manifest,
                    "purpose": "turnaround_derive",
                    "auto": auto,
                },
            )
            lifecycle.mark_enqueued(self.store, job_id)
        except Exception as exc:  # noqa: BLE001
            _cancel_and_refund(self.store, job_id, "queue_unavailable")
            raise VisualError("queue_unavailable", f"提交生图队列失败：{exc}") from exc
        detail = {
            "job_id": job_id,
            "person_id": person_id,
            "profile_id": str(profile["profile_id"]),
            "source_revision": int(revision["revision"]),
            "candidate_only": True,
            "recipe": compiler.TURNAROUND_RECIPE,
            "recipe_version": compiler.TURNAROUND_RECIPE_VERSION,
            "preview": "候选生成后需要有权者明确确认，才会成为正式形象",
        }
        _insert_audit(
            self.store,
            access,
            "derive",
            "job",
            job_id,
            "",
            evidence_ref,
            detail,
        )
        return {
            "success": True,
            "status": "pending",
            "code": "",
            "summary": "已提交后台三视图候选任务；完成只是候选，不会自动启用或外发",
            "data": detail,
        }

    async def _action_activate(
        self,
        *,
        access: Any,
        evidence_ref: str,
        source_ref: str,
        target: dict[str, Any] | None,
        queue: Any,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        del source_ref, queue
        target = target or {"ref": "requester"}
        person_id = resolve_person(self.people, target, access, evidence_ref)
        person = person_for(self.people, person_id, access)
        self.store.require_subject_active(person_id)
        if not can_maintain(self.store, self.people, person, access):
            raise VisualError("permission_denied", "只有本人或角色控制者可以启用正式形象")
        if not getattr(access, "self_service", False):
            require_evidence_or_admin(self.people, access, evidence_ref)
        profile_id = str(fields.get("profile_id") or "").strip()
        revision = fields.get("revision")
        if not profile_id or revision is None:
            raise VisualError("invalid_argument", "启用需要 profile_id 与 revision")
        expected = fields.get("expected_revision", _MISSING)
        result = await self._activate_revision(
            person=person,
            profile_id=profile_id,
            revision=int(revision),
            expected_revision=expected
            if expected is _MISSING or expected is None
            else int(expected),
            access=access,
            evidence_ref=evidence_ref,
            idempotency_seed="",
        )
        return result

    async def _action_set_policy(
        self,
        *,
        access: Any,
        evidence_ref: str,
        source_ref: str,
        target: dict[str, Any] | None,
        queue: Any,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        del source_ref, queue
        target = target or {"ref": "requester"}
        person_id = resolve_person(self.people, target, access, evidence_ref)
        person = person_for(self.people, person_id, access)
        if not can_maintain(self.store, self.people, person, access):
            raise VisualError("permission_denied", "只有有维护权的人可以设置自动候选预授权或兼容许可")
        require_evidence_or_admin(self.people, access, evidence_ref)
        mode = str(fields.get("mode") or "manual").strip()
        scope = str(fields.get("scope") or "").strip()
        if scope and scope not in {"persona", f"group:{access.group_id}"}:
            raise VisualError("invalid_argument", "授权作用域必须对应当前会话")
        grantee = str(fields.get("grantee") or getattr(access, "actor_id", "") or "")
        person_for(self.people, grantee, access)
        if is_persona(self.people, grantee):
            raise VisualError("invalid_argument", "被授权人必须是可核验的人物账号")
        capabilities = fields.get("capabilities")
        if not isinstance(capabilities, list) or not capabilities:
            capabilities = ["upload_generate"] if mode == "auto_candidate" else ["upload_generate"]
        result = policies.create_grant(
            self.store,
            access,
            person_id=person_id,
            mode=mode,
            capabilities=[str(cap) for cap in capabilities],
            revision_scope=str(fields.get("revision_scope") or ""),
            scope=str(fields.get("scope") or ""),
            expires_at=str(fields.get("expires_at") or ""),
            max_uses=None if fields.get("max_uses") is None else int(fields["max_uses"]),
            recipe=compiler.TURNAROUND_RECIPE if mode == "auto_candidate" else "",
            recipe_version=compiler.TURNAROUND_RECIPE_VERSION if mode == "auto_candidate" else "",
            source_revision=(
                None if fields.get("source_revision") is None else int(fields["source_revision"])
            ),
            evidence_ref=evidence_ref,
            grantee=str(fields.get("grantee") or ""),
        )
        return {
            "success": True,
            "status": result["status"],
            "code": "",
            "summary": "预授权或兼容许可已保存；不限制可见素材的手动共享，可用 revoke_consent 撤回",
            "data": result,
        }

    async def _action_revoke_consent(
        self,
        *,
        access: Any,
        evidence_ref: str,
        source_ref: str,
        target: dict[str, Any] | None,
        queue: Any,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        del source_ref, queue
        target = target or {"ref": "requester"}
        person_id = resolve_person(self.people, target, access, evidence_ref)
        person = person_for(self.people, person_id, access)
        if not can_maintain(self.store, self.people, person, access):
            raise VisualError("permission_denied", "只有本人或角色控制者可以撤回授权")
        require_evidence_or_admin(self.people, access, evidence_ref)
        result = policies.revoke_consent(
            self.store,
            access,
            person_id=person_id,
            grant_id=str(fields.get("grant_id") or ""),
            capabilities=fields.get("capabilities")
            if isinstance(fields.get("capabilities"), list)
            else None,
            reason=str(fields.get("reason") or ""),
        )
        cancelled: list[str] = []
        for grant_id in [*result["revoked"], *result.get("adjusted", [])]:
            for job in self.store.jobs_by_grant(grant_id):
                if str(job.get("queue_state")) in _TERMINAL_JOB_STATES:
                    continue
                cancelled.append(str(job["job_id"]))
                await self.cancel_generation(str(job["job_id"]), "cancelled_by_revocation")
        result["cancelled_jobs"] = cancelled
        return {
            "success": True,
            "status": "ok",
            "code": "",
            "summary": ("已撤回授权；未提交的任务已取消" + ("，已提交任务的结果将隔离、不启用不外发" if cancelled else "")),
            "data": result,
        }

    async def _action_retire(
        self,
        *,
        access: Any,
        evidence_ref: str,
        source_ref: str,
        target: dict[str, Any] | None,
        queue: Any,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        del source_ref, queue
        target = target or {"ref": "requester"}
        person_id = resolve_person(self.people, target, access, evidence_ref)
        person = person_for(self.people, person_id, access)
        if not can_maintain(self.store, self.people, person, access):
            raise VisualError("permission_denied", "只有本人或角色控制者可以停用形象")
        require_evidence(self.people, access, evidence_ref)
        asset_id = str(fields.get("asset_id") or "").strip()
        profile_id = str(fields.get("profile_id") or "").strip()
        revision = fields.get("revision")
        retired_jobs: list[str] = []
        if asset_id:
            asset = self.store.asset(asset_id)
            if asset is None or str(asset.get("person_id")) != person_id:
                raise VisualError("not_found_or_not_visible", "资产不存在或不属于该人物")
            self.store.set_asset_status(asset_id, "retired")
            for job in self.store.jobs_for(person_id, limit=200):
                manifest = job.get("manifest") or {}
                if any(
                    str(ref.get("asset_id")) == asset_id
                    for ref in (manifest.get("references") or [])
                    if isinstance(ref, dict)
                ):
                    retired_jobs.append(str(job["job_id"]))
            detail = {"asset_id": asset_id, "status": "retired"}
        elif profile_id and revision is not None:
            row = self.store.revision(profile_id, int(revision))
            if row is None or str(row.get("person_id")) != person_id:
                raise VisualError("not_found_or_not_visible", "修订不存在或不属于该人物")
            self.store.set_revision_status(profile_id, int(revision), "retired", retired=True)
            profile = self.store.profile(profile_id)
            if profile and profile.get("active_revision") == int(revision):
                self.store.set_profile_active(profile_id, None)
            for job in self.store.jobs_for(person_id, limit=200):
                references = (job.get("manifest") or {}).get("references", [])
                if any(
                    ref.get("profile_id") == profile_id and ref.get("revision") == int(revision)
                    for ref in references
                    if isinstance(ref, dict)
                ):
                    retired_jobs.append(str(job["job_id"]))
            detail = {"profile_id": profile_id, "revision": int(revision), "status": "retired"}
        else:
            raise VisualError("invalid_argument", "停用需要 asset_id，或 profile_id 与 revision")
        cancelled: list[str] = []
        for job_id in retired_jobs:
            job = self.store.job(job_id)
            if job is None or str(job.get("queue_state")) in _TERMINAL_JOB_STATES:
                continue
            cancelled.append(job_id)
            await self.cancel_generation(job_id, "cancelled_by_revocation")
        detail["cancelled_jobs"] = cancelled
        object_id = asset_id or f"{profile_id}:{revision}"
        _insert_audit(self.store, access, "retire", "revision", object_id, "", evidence_ref, detail)
        return {
            "success": True,
            "status": "ok",
            "code": "",
            "summary": "已停用；未来不再引用，未提交任务已取消",
            "data": detail,
        }

    async def _action_assign_draft(
        self,
        *,
        access: Any,
        evidence_ref: str,
        source_ref: str,
        target: dict[str, Any] | None,
        queue: Any,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        """管理入口：把上传者的待绑定草稿确认给目标人物。"""
        del source_ref, queue
        draft_profile_id = str(fields.get("draft_profile_id") or "").strip()
        revision_number = fields.get("revision")
        if not draft_profile_id or revision_number is None:
            raise VisualError("invalid_argument", "归属确认需要 draft_profile_id 与 revision")
        draft = self.store.profile(draft_profile_id)
        if draft is None or not str(draft.get("scope")).startswith("unbound:"):
            raise VisualError("not_found_or_not_visible", "待绑定草稿不存在")
        row = self.store.revision(draft_profile_id, int(revision_number))
        if row is None:
            raise VisualError("not_found_or_not_visible", "草稿修订不存在")
        if target is None:
            raise VisualError("invalid_argument", "归属确认需要明确目标人物")
        person_id = resolve_person(self.people, target, access, evidence_ref)
        person = person_for(self.people, person_id, access)
        require_evidence_or_admin(self.people, access, evidence_ref)
        uploader = str(row.get("created_by") or "")
        if not (
            bool(getattr(access, "is_admin", False))
            or can_maintain(self.store, self.people, person, access)
        ):
            raise VisualError("permission_denied", "需要目标人物本人/控制者或管理员确认归属")
        if (
            uploader
            and uploader != str(getattr(access, "actor_id", ""))
            and not bool(getattr(access, "is_admin", False))
        ):
            if not can_maintain(self.store, self.people, person, access):
                raise VisualError("permission_denied", "不能替他人确认归属")
        asset = self.store.asset(self._first_reference_asset(str(row.get("set_id") or "")))
        if asset is None:
            raise VisualError("required_asset_missing", "草稿原图已丢失")
        data = self._read_asset_bytes(asset)
        info = inspect_image(data)
        scope = self._default_scope(person, access)
        profile = self._profile_for_stage(
            person_id, str(draft.get("purpose") or "identity"), scope, None
        )
        with self.store.transaction():
            new_revision = self.store.bump_revision(str(profile["profile_id"]))
            new_asset = self._save_asset(
                data,
                info,
                person_id=person_id,
                access=access,
                controller_id=controller_of(self.store, person),
                purpose=str(draft.get("purpose") or "identity"),
                view=str(asset.get("view") or ""),
                origin="original",
                source_ref=evidence_ref,
                segment=person_id,
            )
            set_id = f"rs_{uuid.uuid4().hex[:12]}"
            self.store.create_reference_set(
                set_id=set_id,
                profile_id=str(profile["profile_id"]),
                revision=new_revision,
                person_id=person_id,
                purpose=str(draft.get("purpose") or "identity"),
            )
            self.store.add_reference_item(
                set_id,
                {
                    "idx": 1,
                    "asset_id": new_asset["asset_id"],
                    "person_id": person_id,
                    "revision": new_revision,
                    "purpose": str(draft.get("purpose") or "identity"),
                    "view": str(asset.get("view") or ""),
                    "required": True,
                    "sha256": new_asset["sha256"],
                },
            )
            self.store.insert_revision(
                {
                    "profile_id": profile["profile_id"],
                    "revision": new_revision,
                    "person_id": person_id,
                    "status": "draft",
                    "origin": "upload",
                    "identity_description": str(row.get("identity_description") or ""),
                    "default_outfit": str(row.get("default_outfit") or ""),
                    "variant_notes": str(row.get("variant_notes") or ""),
                    "inferred_details": str(row.get("inferred_details") or ""),
                    "set_id": set_id,
                    "created_by": str(getattr(access, "actor_id", "") or ""),
                    "created_at": now_iso(),
                }
            )
            detail = {
                "profile_id": str(profile["profile_id"]),
                "revision": new_revision,
                "asset_id": new_asset["asset_id"],
                "assigned_from": draft_profile_id,
                "status": "draft",
            }
            _insert_audit(
                self.store,
                access,
                "assign_draft",
                "revision",
                str(detail),
                "",
                evidence_ref,
                detail,
            )
        return {
            "success": True,
            "status": "draft",
            "code": "",
            "summary": "归属已确认，原图已复制为目标人物的草稿；仍需本人确认启用",
            "data": detail,
        }

    # ==================================================================
    # 激活内部实现
    # ==================================================================
    async def _activate_revision(
        self,
        *,
        person: dict[str, Any],
        profile_id: str,
        revision: int,
        expected_revision: Any,
        access: Any,
        evidence_ref: str,
        idempotency_seed: str,
    ) -> dict[str, Any]:
        profile = self.store.profile(profile_id)
        if profile is None or str(profile.get("person_id")) != str(person.get("person_id")):
            raise VisualError("not_found_or_not_visible", "视觉档案不存在或不属于该人物")
        row = self.store.revision(profile_id, revision)
        if row is None:
            raise VisualError("not_found_or_not_visible", "修订不存在")
        status = str(row.get("status") or "")
        if status in {"retired", "rejected"}:
            raise VisualError("asset_invalid", "已停用或已拒绝的修订不能直接启用")
        current = profile.get("active_revision")
        if expected_revision is not _MISSING and expected_revision != current:
            raise VisualError(
                "revision_conflict",
                "当前正式形象与本次确认的预期不一致，未执行启用",
                data={"current_revision": current, "requested_revision": revision},
            )
        if current == revision and status == "active":
            if expected_revision is _MISSING:
                raise VisualError(
                    "revision_conflict",
                    "该修订已启用，但本次请求缺少当前预期，不能作为新操作重放",
                    data={"current_revision": current},
                )
            return {
                "success": True,
                "status": "already_processed",
                "code": "already_processed",
                "data": {"profile_id": profile_id, "revision": revision},
            }
        # 旧候选不是天然失效：撤权/停用会拦截；否则按 AT-27，重新查询后
        # 有权者可以用当前指针做 CAS，明确选择未撤权的旧候选。
        key = _hash_parts(
            ["activate", profile_id, str(revision), str(current), evidence_ref, idempotency_seed]
        )
        existing = self.store.audit_by_idempotency(key)
        if existing is not None:
            return {
                "success": True,
                "status": "already_processed",
                "code": "already_processed",
                "change_id": str(existing["audit_id"]),
                "data": _audit_detail(existing),
            }
        if expected_revision is _MISSING and current is not None:
            raise VisualError(
                "revision_conflict",
                "当前正式形象已被修改；请重新查询后用当前修订作为 expected_revision 再确认",
                data={"current_revision": current, "requested_revision": revision},
            )
        with self.store.transaction():
            expected = None if expected_revision is _MISSING else expected_revision
            if not self.store.cas_active_revision(profile_id, expected, revision):
                latest = self.store.profile(profile_id) or {}
                raise VisualError(
                    "revision_conflict",
                    "正式形象指针已被其他操作修改，本次启用未生效",
                    data={"current_revision": latest.get("active_revision")},
                )
            if current is not None and int(current) != revision:
                self.store.set_revision_status(profile_id, int(current), "superseded")
            self.store.set_revision_status(profile_id, revision, "active", activated=True)
            detail = {
                "profile_id": profile_id,
                "revision": revision,
                "previous_revision": current,
                "origin": str(row.get("origin") or ""),
                "parent_revision": row.get("parent_revision"),
            }
            audit_id = _insert_audit(
                self.store,
                access,
                "activate",
                "revision",
                f"{profile_id}:{revision}",
                key,
                evidence_ref,
                detail,
            )
        note = ""
        if str(row.get("origin")) == "generated":
            note = "这是 AI 生成候选；已由有权者明确确认。生成图不等于原设计证据。"
        if (
            row.get("parent_revision") is not None
            and current is not None
            and int(current) > revision
        ):
            note = (note + " 该候选基于较旧的来源修订生成，未覆盖当前正式形象。").strip()
        return {
            "success": True,
            "status": "active",
            "code": "",
            "change_id": audit_id,
            "summary": f"已启用修订 v{revision} 为正式形象" + (f"；{note}" if note else ""),
            "data": detail,
        }

    # ==================================================================
    # 编译与输入快照
    # ==================================================================
    def has_generation_reference(self, person_id: str, access: Any) -> bool:
        """该人物在当前会话范围内有没有能进正式参考集的形象图（只看登记状态，不读图）。

        与 :meth:`compile_generation` 选修订的规则一致：可见档案的已启用修订，
        未停用、参考集里至少一张可用素材。给“先告诉用户画不画得了”用，不是授权判断。
        """
        if not person_id or self.store.subject_deleted(person_id):
            return False
        profile = self._select_profile(person_id, access)
        if profile is None or profile.get("active_revision") is None:
            return False
        revision = self.store.revision(str(profile["profile_id"]), int(profile["active_revision"]))
        if revision is None or str(revision.get("status")) in {"retired", "rejected"}:
            return False
        for item in self._revision_reference_items(revision):
            asset = self.store.asset(str(item.get("asset_id") or ""))
            if asset is not None and str(asset.get("status")) == "available":
                return True
        return False

    async def compile_generation(
        self,
        *,
        access: Any,
        subjects: list[Any],
        source_ref: str = "",
        prompt: str = "",
        extra_references: list[str] | None = None,
        purpose: str = "person_image",
        redo: bool = False,
        group_id: str = "",
    ) -> dict[str, Any]:
        """人物分支强制编译：绑定 UUID、正式修订、图序与哈希。"""
        if not subjects and purpose != "reference_image":
            raise VisualError("required_asset_missing", "人物生图必须提供 subjects")
        base_prompt = str(prompt or "").strip()
        if not base_prompt:
            raise VisualError("invalid_argument", "prompt 不能为空")
        entries: list[dict[str, Any]] = []
        subject_rows: list[dict[str, Any]] = []
        grants_by_person: dict[str, Any] = {}
        used_persons: set[str] = set()
        preview = False
        for raw in subjects:
            subject = self._normalize_subject(raw)
            target = {key: subject[key] for key in ("ref", "value") if key in subject}
            if "ref" not in target:
                target = {"ref": "person_id", "value": str(subject.get("person_id") or "")}
            person_id = resolve_person(self.people, target, access, source_ref)
            if person_id in used_persons:
                continue
            used_persons.add(person_id)
            person = person_for(self.people, person_id, access)
            self.store.require_subject_active(person_id)
            profile, revision = self._resolve_generation_revision(person, subject, access)
            grant = require_capability(
                self.store,
                self.people,
                person,
                access,
                "upload_generate",
                revision=int(revision["revision"]),
            )
            status = str(revision.get("status") or "")
            if status in {"retired", "rejected"}:
                raise VisualError("asset_invalid", "该人物正式形象已停用")
            if status in {"draft", "candidate"}:
                if not (subject.get("revision") is not None or subject.get("profile_id")):
                    raise VisualError("consent_required", "草稿或候选不能自动进入正式参考集")
                preview = True
            items = self._revision_reference_items(revision)
            if not items:
                raise VisualError(
                    "required_asset_missing",
                    f"人物「{person.get('display_name') or person_id}」没有可用参考图",
                )
            subject_row = self._subject_from_revision(person, profile, revision, subject, {})
            subject_row["grant_id"] = str(grant["grant_id"]) if grant else ""
            subject_row["capability"] = "upload_generate"
            if grant is not None:
                subject_row["grant"] = {
                    "revision_scope": str(grant.get("revision_scope") or "selected"),
                    "scope": str(grant.get("scope") or ""),
                    "expires_at": grant.get("expires_at"),
                    "max_uses": grant.get("max_uses"),
                    "source_revision": grant.get("source_revision"),
                }
            subject_rows.append(subject_row)
            grants_by_person[person_id] = grant
            for item in items:
                asset = self.store.asset(str(item["asset_id"]))
                data = self._read_asset_bytes(asset)
                entries.append(
                    {
                        "asset_id": str(item["asset_id"]),
                        "person_id": person_id,
                        "display_name": subject_row["display_name"],
                        "profile_id": str(profile["profile_id"]),
                        "revision": int(item.get("revision") or revision["revision"]),
                        "purpose": str(item.get("purpose") or "identity"),
                        "view": str(item.get("view") or ""),
                        "required": bool(int(item.get("required") or 0)),
                        "bytes": data,
                        "sha256": str(item.get("sha256") or sha256_bytes(data)),
                        "mime": str(asset.get("mime") or sniff_mime(data)),
                    }
                )
        for source in extra_references or []:
            data, asset_id, bindings = await self._load_extra_reference(source, access)
            info = inspect_image(data)
            first = bindings[0] if bindings else {}
            entries.append(
                {
                    "asset_id": asset_id,
                    "person_id": str(first.get("person_id") or ""),
                    "display_name": "",
                    "profile_id": str(first.get("profile_id") or ""),
                    "revision": first.get("revision"),
                    "bindings": bindings,
                    "purpose": "extra",
                    "view": "附加参考",
                    "required": purpose == "reference_image",
                    "bytes": data,
                    "sha256": info["sha256"],
                    "mime": info["mime"],
                }
            )
        kept, dropped = compiler.apply_reference_budget(entries)
        mapping: list[dict[str, Any]] = []
        images: list[dict[str, Any]] = []
        for index, entry in enumerate(kept, start=1):
            mapping.append(
                {
                    "index": index,
                    "person_id": entry["person_id"],
                    "display_name": entry["display_name"],
                    "purpose": entry["purpose"],
                    "view": entry["view"],
                    "extra": entry["purpose"] == "extra",
                }
            )
            images.append(
                {
                    "name": _reference_name(
                        index, entry["asset_id"] or entry["sha256"], entry["mime"]
                    ),
                    "bytes": entry["bytes"],
                    "sha256": entry["sha256"],
                    "asset_id": entry["asset_id"],
                    "person_id": entry["person_id"],
                    "profile_id": entry["profile_id"],
                    "revision": entry["revision"],
                    "purpose": entry["purpose"],
                    "view": entry["view"],
                    "required": entry["required"],
                    "bindings": entry.get("bindings", []),
                }
            )
        final_prompt = (
            compiler.build_prompt(
                base_prompt,
                subject_rows,
                mapping,
                extra_references=sum(1 for entry in kept if entry["purpose"] == "extra"),
            )
            if subject_rows
            else base_prompt
        )
        manifest = {
            "purpose": purpose,
            "preview": preview,
            "grant_id": next(
                (
                    str(row.get("grant_id") or "")
                    for row in subject_rows
                    if str(row.get("grant_id") or "")
                ),
                "",
            ),
            "capability": "upload_generate",
            "additional_subjects": [
                binding for entry in kept for binding in entry.get("bindings", [])
            ],
            "subjects": [
                {
                    "person_id": row["person_id"],
                    "display_name": row["display_name"],
                    "profile_id": row["profile_id"],
                    "revision": row["revision"],
                    "outfit_override": row.get("outfit_override", ""),
                    "capability": str(row.get("capability") or "upload_generate"),
                    "grant_id": str(row.get("grant_id") or ""),
                    "grant": row.get("grant"),
                }
                for row in subject_rows
            ],
            "references": [
                {
                    "index": image_index,
                    "asset_id": image["asset_id"],
                    "person_id": image["person_id"],
                    "profile_id": image["profile_id"],
                    "revision": image["revision"],
                    "purpose": image["purpose"],
                    "view": image["view"],
                    "sha256": image["sha256"],
                }
                for image_index, image in enumerate(images, start=1)
            ],
            "dropped_optional": dropped,
            "source_ref": source_ref,
            "persona_id": persona_id_of(self.people),
        }
        input_hash = _hash_parts([final_prompt] + [str(image["sha256"]) for image in images])
        idem_key = ""
        if purpose == "turnaround_derive" and not redo:
            first = subject_rows[0]
            idem_key = _hash_parts(
                [
                    "derive",
                    first["person_id"],
                    first["profile_id"],
                    str(first["revision"]),
                    input_hash,
                    compiler.TURNAROUND_RECIPE,
                    compiler.TURNAROUND_RECIPE_VERSION,
                ]
            )
        return {
            "prompt": final_prompt,
            "images": images,
            "manifest": manifest,
            "input_hash": input_hash,
            "idempotency_key": idem_key,
            "subjects": manifest["subjects"],
            "recipe": compiler.PERSON_RECIPE
            if purpose == "person_image"
            else compiler.TURNAROUND_RECIPE,
            "recipe_version": "1",
            "dropped_optional": dropped,
            "preview": preview,
        }

    async def begin_generation(
        self,
        job_id: str,
        plan: dict[str, Any],
        *,
        access: Any,
        source_ref: str,
        purpose: str,
        group_id: str,
        grant_id: str = "",
        attempt: int = 1,
    ) -> dict[str, Any]:
        manifest = dict(plan.get("manifest") or {})
        subjects = [
            item
            for item in [*manifest.get("subjects", []), *manifest.get("additional_subjects", [])]
            if isinstance(item, dict)
        ]
        for subject in subjects:
            self.store.require_subject_active(str(subject.get("person_id") or ""))
        first = subjects[0] if subjects else {}
        grant_ids: list[str] = []
        for subject in subjects:
            grant_id_of_subject = str(subject.get("grant_id") or "")
            if grant_id_of_subject and grant_id_of_subject not in grant_ids:
                grant_ids.append(grant_id_of_subject)
        if not grant_id and grant_ids:
            grant_id = grant_ids[0]
        # 授权次数在真正入队时预占；预占失败就不产生任务，撤权/失败时按清单精确归还。
        reserved = set(manifest.get("grants_reserved") or [])
        consumed: list[str] = [grant for grant in grant_ids if grant in reserved]
        for grant_id_of_subject in grant_ids:
            if grant_id_of_subject in reserved:
                continue
            if not self.store.consume_grant(grant_id_of_subject):
                for used in consumed:
                    self.store.refund_grant(used)
                raise VisualError("quota_exceeded", "该授权次数已用尽或被撤回，无法提交生成")
            consumed.append(grant_id_of_subject)
        manifest["grant_id"] = grant_id
        manifest["grants_consumed"] = consumed
        manifest.setdefault("deployment_charged", True)
        try:
            return lifecycle.begin_binding(
                self.store,
                job_id=job_id,
                purpose=purpose,
                actor_id=str(getattr(access, "actor_id", "") or ""),
                group_id=group_id or str(getattr(access, "group_id", "") or ""),
                prompt=str(plan.get("prompt") or ""),
                input_hash=str(plan.get("input_hash") or ""),
                manifest=manifest,
                person_id=str(first.get("person_id") or ""),
                profile_id=str(first.get("profile_id") or ""),
                revision=int(first["revision"]) if first.get("revision") is not None else None,
                source_revision=int(first["revision"])
                if first.get("revision") is not None
                else None,
                source_hash="",
                grant_id=grant_id,
                recipe=str(plan.get("recipe") or ""),
                recipe_version=str(plan.get("recipe_version") or ""),
                attempt=attempt,
                idempotency_key=str(plan.get("idempotency_key") or ""),
            )
        except Exception:
            for used in consumed:
                self.store.refund_grant(used)
            raise

    async def preflight_generation(self, job_id: str) -> dict[str, Any]:
        return lifecycle.preflight(self.store, job_id)

    async def mark_generation_enqueued(self, job_id: str) -> dict[str, Any]:
        return lifecycle.mark_enqueued(self.store, job_id)

    async def pending_generation_jobs(self) -> list[dict[str, Any]]:
        """所有尚未到终态的绑定（重启时与持久化队列对账）。"""
        return self.store.visual_jobs_in_states(tuple(lifecycle.PENDING_STATES))

    async def managed_persons_for_digest(self, digest: str) -> list[str]:
        """内容哈希回溯受管素材；仅用于阻止旧入口绕过授权，不用于身份合并。"""
        return sorted(
            {
                str(asset.get("person_id") or "")
                for asset in self.store.assets_by_sha(str(digest or ""))
                if str(asset.get("person_id") or "")
            }
        )

    async def acquire_generation_permit(self, job_id: str) -> dict[str, Any]:
        allow, code, job = lifecycle.acquire_permit(self.store, job_id)
        return {"allow": allow, "code": code, "job": job}

    async def mark_generation_submit_started(self, job_id: str) -> dict[str, Any]:
        allow, code, job = lifecycle.start_submit(self.store, job_id)
        return {"allow": allow, "code": code, "job": job}

    async def cancel_generation(self, job_id: str, code: str = "cancelled_by_revocation") -> dict:
        job = self.store.job(job_id)
        if job is None:
            return {"status": "missing", "code": code}
        if str(job.get("queue_state")) in _TERMINAL_JOB_STATES:
            return {
                "status": str(job.get("queue_state")),
                "code": str(job.get("error_code") or code),
            }
        refund = str(job.get("external_boundary")) == "not_submitted"
        cancelled = lifecycle.cancel_binding(self.store, job, code, refund=refund)
        return {
            "status": str(cancelled.get("queue_state") or "cancelled"),
            "code": code,
            "external_boundary": str(cancelled.get("external_boundary") or ""),
        }

    async def fail_generation(
        self, job_id: str, code: str, *, uncertain: bool = False
    ) -> dict[str, Any]:
        before = self.store.job(job_id)
        job = lifecycle.mark_failure(self.store, job_id, code, uncertain=uncertain)
        if job is None:
            return {"status": "missing", "code": code}
        # 本地失败（缺图、校验失败、上游明确拒绝）没有发生外部计费，应退还额度；
        # 已跨过提交边界的结果未知不能退，避免把可能已花的钱重复再用。
        if before is not None and str(before.get("external_boundary")) == "not_submitted":
            policies.release_job_budget(self.store, before)
            policies.release_job_grants(self.store, before)
        return {
            "status": str(job.get("queue_state") or ""),
            "code": code,
            "isolated": bool(int(job.get("isolated") or 0)),
        }

    async def complete_generation(
        self,
        job_id: str,
        *,
        image_bytes: bytes,
        mime_type: str,
        artifact_path: str = "",
    ) -> dict[str, Any]:
        """生成完成：只保存候选；撤权/停用时隔离，不启用不外发。"""
        job = self.store.job(job_id)
        if job is None:
            raise VisualError("outcome_uncertain", "生成绑定丢失，结果按未知状态处理")
        if (job.get("manifest") or {}).get("deleted"):
            if artifact_path:
                artifact = Path(artifact_path)
                if is_within(self.image_artifacts, artifact):
                    artifact.unlink(missing_ok=True)
            return {"isolated": True, "code": "cancelled_by_revocation", "deliverable": False}
        reason = lifecycle.revocation_reason(self.store, job)
        info = inspect_image(image_bytes)
        person_id = str(job.get("person_id") or "")
        job_actor = str(job.get("actor_id") or "")
        job_group = str(job.get("group_id") or "")
        person: dict[str, Any] | None = None
        if person_id:
            try:
                person = person_for(
                    self.people,
                    person_id,
                    _Access(actor_id=job_actor, group_id=job_group),
                )
            except VisualError:
                # 角色由控制者发起、人格自身形象都不一定能被 people 的只读接口看见；
                # 已登记的控制器关系与人格 UUID 仍可确认归属，不能把合法结果误判为隔离。
                if is_persona(self.people, person_id) or (
                    job_actor and self.store.control(person_id) == job_actor
                ):
                    person = {"person_id": person_id, "display_name": "", "kind": "character"}
                elif self.store.profiles_for(person_id):
                    # 已在本库登记视觉档案的人物：授权已在 revocation_reason 中重验，
                    # 这里只确认归属，不拿任务 actor 冒充最新的人类证据。
                    person = {"person_id": person_id, "display_name": "", "kind": "human"}
                else:
                    person = None
        else:
            person = None
        isolated = bool(reason) or person is None
        if person is None:
            person = {"person_id": person_id, "display_name": "", "kind": "human"}
        asset = self._save_asset(
            image_bytes,
            info,
            person_id=person_id,
            access=_Access(actor_id=job_actor, group_id=job_group),
            controller_id=controller_of(self.store, person),
            purpose="identity",
            view="turnaround" if str(job.get("purpose")) == "turnaround_derive" else "generated",
            origin="generated",
            source_ref=job_id,
            segment=person_id or "generated",
            status="isolated" if isolated else "available",
            kind="derived",
        )
        result: dict[str, Any] = {
            "asset_id": asset["asset_id"],
            "sha256": info["sha256"],
            "isolated": isolated,
            "code": reason or "",
            "enabled": False,
            "deliverable": not isolated,
        }
        if not isolated and str(job.get("purpose")) == "turnaround_derive":
            profile_id = str(job.get("profile_id") or "")
            profile = self.store.profile(profile_id)
            if profile is None:
                isolated = True
                result.update(isolated=True, code="cancelled_by_revocation", deliverable=False)
                self.store.set_asset_status(asset["asset_id"], "isolated")
            else:
                source_revision = int(job.get("source_revision") or 0)
                set_id = f"rs_{uuid.uuid4().hex[:12]}"
                revision_number = self.store.bump_revision(profile_id)
                self.store.create_reference_set(
                    set_id=set_id,
                    profile_id=profile_id,
                    revision=revision_number,
                    person_id=person_id,
                    purpose="identity",
                )
                self.store.add_reference_item(
                    set_id,
                    {
                        "idx": 1,
                        "asset_id": asset["asset_id"],
                        "person_id": person_id,
                        "revision": revision_number,
                        "purpose": "identity",
                        "view": "turnaround",
                        "required": True,
                        "sha256": info["sha256"],
                    },
                )
                self.store.insert_revision(
                    {
                        "profile_id": profile_id,
                        "revision": revision_number,
                        "person_id": person_id,
                        "status": "candidate",
                        "origin": "generated",
                        "identity_description": str(profile.get("identity_description") or ""),
                        "default_outfit": str(
                            self._revision(profile_id, source_revision).get("default_outfit") or ""
                        )
                        if source_revision
                        else "",
                        "variant_notes": "",
                        "inferred_details": "三视图的背面、侧面及未展示细节由 AI 补全，需人工确认",
                        "recipe": str(job.get("recipe") or compiler.TURNAROUND_RECIPE),
                        "recipe_version": str(job.get("recipe_version") or "1"),
                        "parent_revision": source_revision or None,
                        "set_id": set_id,
                        "prompt": str(job.get("prompt") or ""),
                        "input_hash": str(job.get("input_hash") or ""),
                        "created_by": str(job.get("actor_id") or ""),
                        "created_at": now_iso(),
                    }
                )
                result.update(revision=revision_number, profile_id=profile_id, candidate=True)
        lifecycle.mark_result(
            self.store,
            job_id,
            result_asset_id=asset["asset_id"],
            result_sha256=info["sha256"],
            result_path=artifact_path,
            isolated=isolated,
            revision=result.get("revision"),
        )
        _insert_audit(
            self.store,
            _Access(actor_id=str(job.get("actor_id") or "")),
            "generation_completed",
            "job",
            job_id,
            "",
            "",
            {
                "isolated": isolated,
                "asset_id": asset["asset_id"],
                "revision": result.get("revision"),
            },
        )
        return result

    async def recover_generation(self, job_id: str) -> dict[str, Any]:
        """进程重启后的恢复：已触达上游的按未知处理，不自动重放付费请求。"""
        job = self.store.job(job_id)
        if job is None:
            return {"status": "missing", "code": "outcome_uncertain"}
        state = str(job.get("queue_state"))
        boundary = str(job.get("external_boundary") or "")
        if state in _TERMINAL_JOB_STATES:
            return {"status": state, "code": str(job.get("error_code") or "")}
        if boundary in {"submit_started", "submitted"}:
            updated = lifecycle.mark_failure(
                self.store, job_id, "outcome_uncertain", uncertain=True
            )
            return {
                "status": str((updated or {}).get("queue_state") or "outcome_uncertain"),
                "code": "outcome_uncertain",
                "refund": False,
            }
        refund = boundary == "not_submitted"
        cancelled = lifecycle.cancel_binding(self.store, job, "outcome_uncertain", refund=refund)
        return {
            "status": str(cancelled.get("queue_state") or "cancelled"),
            "code": "outcome_uncertain",
            "refund": refund,
        }

    # ==================================================================
    # 受管路径守卫
    # ==================================================================
    def _asset_bindings(
        self, asset: dict[str, Any], access: Any, capability: str
    ) -> list[dict[str, Any]]:
        """裸资产也必须落回可见档案和具体修订，成品保留所有来源的权限边界。"""
        lineage = self._asset_lineage(asset)
        if len(lineage) > 1:
            return [
                binding
                for source in lineage
                for binding in self._asset_bindings(
                    {**source, "parent_asset_id": None}, access, capability
                )
            ]
        person_id = str(asset.get("person_id") or "")
        self.store.require_subject_active(person_id)
        person = person_for(self.people, person_id, access)
        if str(asset.get("status")) != "available":
            raise VisualError("consent_required", "资产已停用或隔离")
        job = self.store.job(str(asset.get("source_ref") or ""))
        if job and str(job.get("result_asset_id") or "") == str(asset["asset_id"]):
            if lifecycle.revocation_reason(self.store, job):
                raise VisualError("cancelled_by_revocation", "来源任务授权已失效")
            bindings = []
            manifest = job.get("manifest") or {}
            for source in [*manifest.get("subjects", []), *manifest.get("additional_subjects", [])]:
                source_person = person_for(self.people, str(source.get("person_id") or ""), access)
                self.store.require_subject_active(str(source_person["person_id"]))
                profile = self.store.profile(str(source.get("profile_id") or ""))
                if profile is None:
                    raise VisualError("consent_required", "无法验证成品的来源档案")
                if not self._profile_visible(profile, access):
                    raise VisualError("consent_required", "来源档案不在当前会话范围内")
                grant = require_capability(
                    self.store,
                    self.people,
                    source_person,
                    access,
                    capability,
                    revision=source.get("revision"),
                )
                bindings.append(
                    {
                        **source,
                        "capability": capability,
                        "grant_id": grant["grant_id"] if grant else "",
                    }
                )
            if bindings:
                return bindings
            raise VisualError("consent_required", "无法验证成品的来源主体")
        rows = self.store.fetchall(
            "SELECT DISTINCT p.profile_id,p.scope,r.revision,r.status FROM vs_reference_item i"
            " JOIN vs_reference_set s ON s.set_id=i.set_id"
            " JOIN vs_profile p ON p.profile_id=s.profile_id"
            " JOIN vs_revision r ON r.profile_id=p.profile_id AND r.revision=s.revision"
            " WHERE i.asset_id=? AND p.person_id=?",
            (asset["asset_id"], person_id),
        )
        for row in rows:
            if row["status"] in {"retired", "rejected"}:
                continue
            if not self._profile_visible(row, access):
                continue
            ok, _, grant = authorize_with_grant(
                self.store,
                self.people,
                person,
                access,
                capability,
                asset=asset,
                revision=int(row["revision"]),
            )
            if ok:
                return [
                    {
                        "person_id": person_id,
                        "profile_id": row["profile_id"],
                        "revision": int(row["revision"]),
                        "grant_id": grant["grant_id"] if grant else "",
                        "capability": capability,
                    }
                ]
        raise VisualError("consent_required", "该资产的档案超出当前范围或修订不可用")

    def _consume_bindings(self, bindings: list[dict[str, Any]]) -> None:
        with self.store.transaction():
            for grant_id in {str(item.get("grant_id") or "") for item in bindings} - {""}:
                if not self.store.consume_grant(grant_id):
                    raise VisualError("consent_required", "授权次数已用尽或已撤回")

    async def open_asset_for_read(self, asset_id: str, access: Any) -> dict[str, Any]:
        """读取当前可见共享素材；来源范围和停用/删除状态仍须通过验证。"""
        asset = self.store.asset(str(asset_id or ""))
        if asset is None or str(asset.get("status")) != "available":
            raise VisualError("not_found_or_not_visible", "资产不存在或不可用")
        bindings = self._asset_bindings(asset, access, "vision_read")
        data = self._read_asset_bytes(asset)
        self._consume_bindings(bindings)
        return {
            "asset_id": str(asset["asset_id"]),
            "person_id": str(asset.get("person_id") or ""),
            "bytes": data,
            "mime": str(asset.get("mime") or ""),
            "sha256": str(asset.get("sha256") or ""),
        }

    async def guard_path(
        self, path: str | Path, access: Any, capability: str, *, consume: bool = False
    ) -> dict[str, Any]:
        """按受管对象与实际能力校验任意旧入口拿到的裸路径。"""
        capability = str(capability or "").strip()
        if capability not in _VISUAL_CAPABILITIES:
            return {
                "managed": False,
                "allowed": False,
                "code": "invalid_argument",
                "capability": capability,
                "asset_id": "",
            }
        match = self._match_managed(path)
        if not match["managed"]:
            return {
                "managed": False,
                "allowed": True,
                "code": "",
                "capability": capability,
                "asset_id": "",
            }
        person_ids = match["person_ids"]
        asset = match.get("asset")
        allowed = True
        code = ""
        if match.get("unverifiable") or not [pid for pid in person_ids if pid]:
            # 受管目录里但无法验证归属/授权的文件：不提供可用结论，按不可见拒绝。
            return {
                "managed": True,
                "allowed": False,
                "code": "not_found_or_not_visible",
                "capability": capability,
                "asset_id": str((asset or {}).get("asset_id") or ""),
                "job_id": match.get("job_id", ""),
            }
        bindings: list[dict[str, Any]] = []
        try:
            job_id = str(match.get("job_id") or "")
            if job_id:
                job = self.store.job(job_id)
                if job and lifecycle.revocation_reason(self.store, job):
                    raise VisualError("cancelled_by_revocation", "来源任务授权已失效")
            if asset is None:
                raise VisualError("not_found_or_not_visible", "无法确认素材来源修订")
            for matched_asset in match.get("assets") or [asset]:
                bindings.extend(self._asset_bindings(matched_asset, access, capability))
            if consume:
                self._consume_bindings(bindings)
        except VisualError as exc:
            allowed, code = False, exc.code
        return {
            "managed": True,
            "allowed": allowed,
            "code": code,
            "capability": capability,
            "asset_id": str((asset or {}).get("asset_id") or ""),
            "job_id": match.get("job_id", ""),
            "bindings": bindings if allowed else [],
        }

    def _match_managed(self, path: str | Path) -> dict[str, Any]:
        """把任意入口拿到的路径规范成候选真实路径，再判断是否受管。

        绝对路径、相对路径、``~``（人格 home）与 ``file://`` URI 指向同一文件时
        必须得到同一结论；受管目录里查不到登记记录的文件也不能因为“没有元数据”
        就默认放行（PRD NFR-02／AT-41）。
        """
        raw = str(path or "").strip()
        if raw.startswith("base64://") or (raw.startswith("data:") and ";base64," in raw):
            encoded = raw[9:] if raw.startswith("base64://") else raw.split(";base64,", 1)[1]
            if len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 8:
                return self._unknown_managed("")
            try:
                return self._managed_digest(sha256_bytes(base64.b64decode(encoded, validate=True)))
            except (ValueError, binascii.Error):
                return self._unknown_managed("")
        candidates = self._candidate_paths(path)
        for candidate in candidates:
            match = self._match_candidate(candidate)
            if match.get("managed"):
                return match
        for candidate in candidates:
            if candidate.is_file() and candidate.stat().st_size <= MAX_IMAGE_BYTES:
                match = self._managed_by_digest(candidate)
                if match.get("managed"):
                    return match
        return {"managed": False, "person_ids": []}

    def _candidate_paths(self, path: str | Path) -> list[Path]:
        raw = str(path or "").strip()
        if not raw or raw.lower().startswith(("http://", "https://", "data:", "base64://", "//")):
            return []
        candidates: list[Path] = []
        lowered = raw.lower()
        if lowered.startswith("file:"):
            parsed = urlparse(raw)
            local = unquote(parsed.path or "")
            if parsed.netloc and parsed.netloc.lower() != "localhost":
                local = f"//{parsed.netloc}{local}"
            if re.match(r"^/[A-Za-z]:", local):
                local = local[1:]
            candidates.append(Path(local))
        else:
            candidate = Path(raw)
            if raw.startswith("~"):
                rest = raw[2:] if raw[1:2] in {"/", "\\"} else raw[1:]
                if rest:
                    candidates.append(self.persona_home / rest)
                candidates.append(Path(raw).expanduser())
            elif candidate.is_absolute():
                candidates.append(candidate)
            else:
                for base in (Path.cwd(), self.work_path, self.persona_home):
                    candidates.append(base / candidate)
        resolved: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                absolute = candidate.resolve(strict=False)
            except OSError:
                absolute = candidate.absolute()
            key = os.path.normcase(str(absolute))
            if key in seen:
                continue
            seen.add(key)
            resolved.append(absolute)
        return resolved

    def _match_candidate(self, candidate: Path) -> dict[str, Any]:
        visual_root = self.assets_root
        if is_within(visual_root, candidate):
            try:
                rel_path = candidate.relative_to(visual_root.resolve(strict=False)).as_posix()
            except ValueError:
                rel_path = ""
            if not rel_path or rel_path.startswith("__deleted__"):
                return self._unknown_managed("")
            asset = self.store.asset_by_rel(rel_path)
            if asset is None:
                # 受管根内的孤儿/已删除文件：没有可验证的归属与授权，一律拒绝。
                return self._unknown_managed("")
            return {
                "managed": True,
                "person_ids": [str(asset.get("person_id") or "")],
                "asset": asset,
                "job_id": "",
            }
        if is_within(self.image_artifacts, candidate):
            try:
                rel = candidate.relative_to(self.image_artifacts.resolve(strict=False))
            except ValueError:
                rel = None
            parts = rel.parts if rel is not None else ()
            if parts and parts[0] == "references" and len(parts) >= 2:
                return self._managed_task_copy(str(parts[1]), candidate)
            if parts and parts[0] == "images" and len(parts) >= 2:
                job_id = Path(parts[1]).stem
                match = self._managed_job_result(job_id)
                if match.get("managed"):
                    return match
                return self._managed_by_digest(candidate)
        if is_within(self.read_image_artifacts, candidate):
            try:
                rel = candidate.relative_to(self.read_image_artifacts.resolve(strict=False))
            except ValueError:
                rel = None
            parts = rel.parts if rel is not None else ()
            if parts and parts[0] == "normalized":
                return self._managed_by_digest(candidate)
        return {"managed": False, "person_ids": []}

    def _unknown_managed(self, asset_id: str) -> dict[str, Any]:
        return {
            "managed": True,
            "person_ids": [],
            "asset": {"asset_id": asset_id, "status": "deleted"},
            "job_id": "",
            "unverifiable": True,
        }

    def _managed_by_digest(self, candidate: Path) -> dict[str, Any]:
        try:
            digest = sha256_bytes(candidate.read_bytes())
        except OSError:
            return {"managed": False, "person_ids": []}
        return self._managed_digest(digest)

    def _managed_digest(self, digest: str) -> dict[str, Any]:
        assets = self.store.assets_by_sha(digest)
        assigned = [asset for asset in assets if asset.get("person_id")]
        if assigned:
            assets = assigned
        if not assets:
            return {"managed": False, "person_ids": []}
        return {
            "managed": True,
            "person_ids": [str(asset.get("person_id") or "") for asset in assets],
            "asset": assets[0],
            "assets": assets,
            "job_id": "",
        }

    def _managed_task_copy(self, job_id: str, candidate: Path) -> dict[str, Any]:
        match = self._managed_by_digest(candidate)
        if match.get("managed"):
            match["job_id"] = job_id
            return match
        if self.store.job(job_id) is not None:
            return self._unknown_managed("")
        return match

    def _managed_job_result(self, job_id: str) -> dict[str, Any]:
        job = self.store.job(job_id)
        if job is None:
            return {"managed": False, "person_ids": []}
        asset = self.store.asset(str(job.get("result_asset_id") or ""))
        person_ids = [str(job.get("person_id") or "")]
        return {
            "managed": True,
            "person_ids": [person_id for person_id in person_ids if person_id],
            "asset": asset,
            "job_id": job_id,
        }

    # ==================================================================
    # 外发回执
    # ==================================================================
    async def record_delivery(
        self,
        *,
        access: Any,
        asset_id: str = "",
        job_id: str = "",
        channel: str = "",
        target: str = "",
        message_id: str = "",
        success: bool = False,
        code: str = "",
    ) -> dict[str, Any]:
        delivery_id = f"vd_{uuid.uuid4().hex[:12]}"
        self.store.insert_delivery(
            {
                "delivery_id": delivery_id,
                "asset_id": asset_id,
                "job_id": job_id,
                "actor_id": str(getattr(access, "actor_id", "") or ""),
                "channel": channel,
                "target": target,
                "message_id": message_id,
                "success": success,
                "code": code,
            }
        )
        return {"recorded": True, "delivery_id": delivery_id, "success": success}

    # ==================================================================
    # 管理入口
    # ==================================================================
    async def set_deployment_policy(self, access: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            evidence_ref = str(kwargs.pop("evidence_ref", "") or "")
            with self.store.transaction():
                result = policies.set_deployment_policy(
                    self.store, access, evidence_ref=evidence_ref, **kwargs
                )
            return {"success": True, **result}
        except VisualError as exc:
            return exc.to_result()

    async def management_state(self, access: Any) -> dict[str, Any]:
        if not bool(getattr(access, "is_admin", False)):
            return VisualError("permission_denied", "只有可信应用管理员可以查看管理状态").to_result()
        return {
            "success": True,
            "status": "ok",
            "data": {
                "deployment": policies.deployment_state(self.store),
                "persona_id": persona_id_of(self.people),
                "asset_root": "visual_assets",
            },
        }

    async def prepare_deletion(
        self,
        subject: dict[str, Any] | str,
        access: Any,
        *,
        scope: str = "person",
        evidence_ref: str = "",
    ) -> dict[str, Any]:
        try:
            target = (
                subject
                if isinstance(subject, dict)
                else {"ref": "person_id", "value": str(subject)}
            )
            person_id = resolve_person(self.people, target, access, evidence_ref)
            person = person_for(self.people, person_id, access)
            if not (
                can_maintain(self.store, self.people, person, access)
                or bool(getattr(access, "is_admin", False))
            ):
                raise VisualError("permission_denied", "只有本人/控制者或管理员可以删除资产")
            require_evidence_or_admin(self.people, access, evidence_ref)
            result = retention.prepare_deletion(
                self.store,
                self.work_path,
                person_id=person_id,
                actor_id=str(getattr(access, "actor_id", "") or ""),
                scope=scope,
            )
            return {"success": True, "status": result["status"], "data": result}
        except VisualError as exc:
            return exc.to_result()

    async def execute_deletion(
        self, confirm_token: str, access: Any, *, evidence_ref: str = ""
    ) -> dict[str, Any]:
        try:
            require_evidence_or_admin(self.people, access, evidence_ref)
            deletion = self.store.deletion(str(confirm_token or ""))
            if deletion:
                person = person_for(self.people, str(deletion["subject_id"]), access)
                if not (
                    can_maintain(self.store, self.people, person, access)
                    or getattr(access, "is_admin", False)
                ):
                    raise VisualError("permission_denied", "删除前控制权已失效，请重新申请")
            result = retention.execute_deletion(
                self.store,
                self.work_path,
                token=str(confirm_token or ""),
                actor_id=str(getattr(access, "actor_id", "") or ""),
            )
            return {"success": True, "status": result["status"], "data": result}
        except VisualError as exc:
            return exc.to_result()

    # ==================================================================
    # 自动候选控制器
    # ==================================================================
    async def auto_candidates(self, queue: Any, *, limit: int = 1) -> dict[str, Any]:
        """后台控制器：按有效授权与预算补齐缺项，只生成候选。

        它不是新队列：通过同一个领域服务把受限授权转成现有 image_generate 任务。
        """
        if not policies.deployment_auto_ready(self.store):
            return {"status": "skipped", "reason": "deployment_budget_not_ready", "submitted": []}
        grants = self.store.auto_grants()
        submitted: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for grant in grants:
            if len(submitted) >= limit:
                break
            person_id = str(grant.get("person_id") or "")
            group_id = _group_from_scope(str(grant.get("scope") or ""))
            access = _Access(
                actor_id=self.store.grantee_for(grant),
                group_id=group_id,
                is_admin=False,
                self_initiated=False,
            )
            try:
                person_for(self.people, person_id, access)
                profile, revision = self._select_source_revision(person_id, access, {})
            except VisualError as exc:
                skipped.append({"person_id": person_id, "reason": exc.code})
                continue
            if not policies.auto_grant_applies(grant, int(revision["revision"]), group_id):
                skipped.append({"person_id": person_id, "reason": "grant_not_applicable"})
                continue
            if self._turnaround_candidate_exists(profile["profile_id"], int(revision["revision"])):
                skipped.append({"person_id": person_id, "reason": "candidate_exists"})
                continue
            try:
                result = await self.manage(
                    "derive",
                    access=access,
                    target={"ref": "person_id", "value": person_id},
                    queue=queue,
                    auto=True,
                    grant_id=str(grant.get("grant_id") or ""),
                )
            except VisualError as exc:
                skipped.append({"person_id": person_id, "reason": exc.code})
                continue
            if result.get("success") and result.get("status") == "already_pending":
                skipped.append({"person_id": person_id, "reason": "already_pending"})
                continue
            if result.get("success"):
                submitted.append({"person_id": person_id, "data": result.get("data")})
            else:
                skipped.append(
                    {"person_id": person_id, "reason": str(result.get("code") or "error")}
                )
        return {"status": "ok", "submitted": submitted, "skipped": skipped}

    def _turnaround_candidate_exists(self, profile_id: str, source_revision: int) -> bool:
        for revision in self.store.revisions_for(
            str((self.store.profile(profile_id) or {}).get("person_id") or ""),
            statuses=("candidate", "active"),
            limit=20,
        ):
            if (
                str(revision.get("profile_id")) == profile_id
                and revision.get("parent_revision") == source_revision
                and str(revision.get("origin")) == "generated"
            ):
                return True
        return False

    # ==================================================================
    # 内部工具
    # ==================================================================
    def _normalize_subject(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, str):
            return {"ref": "person_id", "value": raw.strip()}
        if not isinstance(raw, dict):
            raise VisualError("invalid_argument", "subjects 每项必须是主体引用对象")
        subject = dict(raw)
        if "ref" not in subject and "person_id" in subject:
            subject["ref"] = "person_id"
            subject["value"] = subject.get("person_id")
        if "outfit" in subject and "outfit_override" not in subject:
            subject["outfit_override"] = subject["outfit"]
        return subject

    def _default_scope(self, person: dict[str, Any], access: Any) -> str:
        if is_persona(self.people, str(person.get("person_id") or "")):
            return "persona"
        group_id = str(getattr(access, "group_id", "") or "").strip()
        return f"group:{group_id}" if group_id else "persona"

    def _profile_for_stage(
        self, person_id: str, purpose: str, scope: str, requested_profile_id: Any
    ) -> dict[str, Any]:
        if requested_profile_id:
            profile = self.store.profile(str(requested_profile_id))
            if (
                profile is None
                or str(profile.get("person_id")) != person_id
                or str(profile.get("scope")) != scope
                or str(profile.get("purpose")) != purpose
            ):
                raise VisualError("not_found_or_not_visible", "视觉档案与本次目标、用途或范围不符")
            return profile
        profile = self.store.profile_by_key(person_id, purpose, scope)
        return profile or self.store.create_profile(
            person_id=person_id, purpose=purpose, scope=scope
        )

    def _revision(self, profile_id: str, revision: int) -> dict[str, Any]:
        row = self.store.revision(profile_id, int(revision))
        return row or {}

    def _load_source(
        self,
        source: Any,
        *,
        access: Any,
        evidence_ref: str,
        attachments: Any,
    ) -> tuple[bytes, dict[str, Any]]:
        """只接受本回合聊天附件或当前可见共享资产；路径/URL 不作为入库凭证。"""
        spec = source if isinstance(source, dict) else {"kind": "attachment"}
        kind = str(spec.get("kind") or "attachment").strip()
        if kind == "attachment":
            items = _image_attachments(attachments)
            if not items:
                raise VisualError(
                    "asset_invalid",
                    "本回合没有可入库的图片附件；请先发送图片，或使用当前可见的受管素材",
                )
            index = spec.get("index")
            try:
                position = 0 if index is None else int(index)
            except (TypeError, ValueError) as exc:
                raise VisualError("invalid_argument", "附件序号无效") from exc
            if position < 0 or position >= len(items):
                raise VisualError("asset_invalid", "附件序号超出本回合图片范围")
            item = items[position]
            local_path = str(item.get("file_path") or item.get("value") or "").strip()
            if not local_path or local_path.startswith(("http://", "https://", "data:")):
                raise VisualError("asset_invalid", "附件没有本地可读副本，不能入库")
            path = Path(local_path).expanduser()
            if not path.is_file():
                raise VisualError("asset_invalid", "附件本地副本不存在或已过期")
            size = path.stat().st_size
            if size > MAX_IMAGE_BYTES:
                raise VisualError("asset_invalid", "附件超过单张 20MB 限制")
            data = path.read_bytes()
            matched_assets = [
                asset
                for asset in self.store.assets_by_sha(sha256_bytes(data))
                if asset.get("person_id")
            ]
            for asset in matched_assets:
                self._asset_bindings(asset, access, "upload_generate")
            return data, {
                "kind": "attachment",
                "index": position,
                "asset_id": str(matched_assets[0]["asset_id"]) if matched_assets else "",
            }
        if kind == "asset":
            asset_id = str(spec.get("asset_id") or "").strip()
            asset = self.store.asset(asset_id)
            if asset is None or str(asset.get("status")) != "available":
                raise VisualError("asset_invalid", "资产不存在或不可用")
            self._asset_bindings(asset, access, "upload_generate")
            return self._read_asset_bytes(asset), {"kind": "asset", "asset_id": asset_id}
        if kind in {"url", "remote"}:
            raise VisualError(
                "asset_invalid",
                "为避免 SSRF 与任意外链入库，视觉资产不接受远程 URL；请先把图片作为聊天附件发送，" "或引用当前可见的受管素材",
            )
        if kind == "path":
            if not bool(getattr(access, "self_service", False)):
                raise VisualError(
                    "asset_invalid",
                    "不能按任意本地路径入库；请使用本回合聊天附件或当前可见的受管素材",
                )
            raw_path = str(spec.get("path") or "").strip()
            if not raw_path:
                raise VisualError("invalid_argument", "path 来源需要 path 参数")
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = self.work_path / path
            try:
                resolved = path.resolve()
            except OSError as exc:
                raise VisualError("asset_invalid", "本地图片路径无效") from exc
            if not resolved.is_file():
                raise VisualError("asset_invalid", "本地图片不存在")
            if resolved.stat().st_size > MAX_IMAGE_BYTES:
                raise VisualError("asset_invalid", "图片超过单张 20MB 限制")
            return resolved.read_bytes(), {"kind": "path", "path": str(resolved)}
        raise VisualError("invalid_argument", f"不支持的素材来源类型: {kind}")

    def _resolve_generation_revision(
        self,
        person: dict[str, Any],
        subject: dict[str, Any],
        access: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        person_id = str(person.get("person_id") or "")
        requested_profile = str(subject.get("profile_id") or "").strip()
        requested_revision = subject.get("revision")
        if requested_profile:
            profile = self.store.profile(requested_profile)
            if profile is None or str(profile.get("person_id")) != person_id:
                raise VisualError("not_found_or_not_visible", "视觉档案不存在或不属于该人物")
            if not self._profile_visible(profile, access):
                raise VisualError("consent_required", "该档案不在当前会话授权范围内")
            if requested_revision is None:
                requested_revision = profile.get("active_revision")
        else:
            profile = self._select_profile(person_id, access)
            if profile is None:
                raise VisualError(
                    "required_asset_missing",
                    f"人物「{person.get('display_name') or person_id}」还没有正式视觉形象",
                )
            if requested_revision is None:
                requested_revision = profile.get("active_revision")
        if requested_revision is None:
            raise VisualError("required_asset_missing", "该视觉档案还没有可用的正式修订")
        revision = self.store.revision(str(profile["profile_id"]), int(requested_revision))
        if revision is None:
            raise VisualError("not_found_or_not_visible", "视觉修订不存在")
        if str(revision.get("status")) in {"retired", "rejected"}:
            raise VisualError("asset_invalid", "该修订已停用或拒绝，不能用于生成")
        return profile, revision

    def _select_profile(self, person_id: str, access: Any) -> dict[str, Any] | None:
        profiles = self.store.profiles_for(person_id)
        matching = [profile for profile in profiles if self._profile_visible(profile, access)]
        active = [profile for profile in matching if profile.get("active_revision") is not None]
        for profile in active:
            if str(profile.get("purpose")) == "identity":
                return profile
        return active[0] if active else (matching[0] if matching else None)

    def _profile_visible(self, profile: dict[str, Any], access: Any) -> bool:
        scope = str(profile.get("scope") or "")
        if scope in {"", "persona"}:
            return True
        group_id = str(getattr(access, "group_id", "") or "")
        return scope in {group_id, f"group:{group_id}"}

    def _select_source_revision(
        self, person_id: str, access: Any, fields: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        profile_id = str(fields.get("profile_id") or "").strip()
        revision = fields.get("revision")
        if profile_id:
            profile = self.store.profile(profile_id)
        else:
            profile = self._select_profile(person_id, access)
        if profile is None or str(profile.get("person_id")) != person_id:
            raise VisualError("required_asset_missing", "该人物还没有正式视觉档案")
        if not self._profile_visible(profile, access):
            raise VisualError("consent_required", "该视觉档案不在当前会话范围内")
        number = revision if revision is not None else profile.get("active_revision")
        if number is None:
            raise VisualError("required_asset_missing", "该人物还没有正式形象")
        row = self.store.revision(str(profile["profile_id"]), int(number))
        if row is None:
            raise VisualError("not_found_or_not_visible", "视觉修订不存在")
        if str(row.get("status")) in {"retired", "rejected"}:
            raise VisualError("asset_invalid", "该修订已停用，不能作为派生来源")
        return profile, row

    def _revision_reference_items(self, revision: dict[str, Any]) -> list[dict[str, Any]]:
        return self.store.reference_items(str(revision.get("set_id") or ""))

    def _subject_from_revision(
        self,
        person: dict[str, Any],
        profile: dict[str, Any],
        revision: dict[str, Any],
        subject: dict[str, Any],
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "person_id": str(person.get("person_id") or ""),
            "display_name": str(person.get("display_name") or ""),
            "profile_id": str(profile["profile_id"]),
            "revision": int(revision["revision"]),
            "identity_description": str(revision.get("identity_description") or ""),
            "default_outfit": str(revision.get("default_outfit") or ""),
            "variant_notes": str(revision.get("variant_notes") or ""),
            "inferred_details": str(revision.get("inferred_details") or ""),
            "outfit_override": compiler.clean_text(
                subject.get("outfit_override") or fields.get("outfit_override"), 300
            ),
        }

    def _save_asset(
        self,
        data: bytes,
        info: dict[str, Any],
        *,
        person_id: str,
        access: Any,
        controller_id: str,
        purpose: str,
        view: str,
        origin: str,
        source_ref: str,
        segment: str,
        status: str = "available",
        kind: str = "originals",
        parent_asset_id: str | None = None,
    ) -> dict[str, Any]:
        rel_path = relative_asset_path(segment, info["sha256"], info["ext"], kind=kind)
        target = self.assets_root / rel_path
        if not is_within(self.assets_root, target):
            raise VisualError("asset_invalid", "资产路径越界，已拒绝")
        existing = self.store.asset_by_rel(rel_path)
        if existing is not None and not (
            str(existing.get("status")) == status == "available"
            and str(existing.get("origin")) == origin == "original"
        ):
            # 成品保留独立任务来源；已停用的资产永不因同字节重入而复活。
            base = Path(rel_path)
            rel_path = base.with_name(f"{base.stem}_{uuid.uuid4().hex[:8]}{base.suffix}").as_posix()
            target = self.assets_root / rel_path
            existing = None
        if not target.is_file():
            atomic_write_bytes(target, data)
        elif sha256_bytes(target.read_bytes()) != info["sha256"]:
            atomic_write_bytes(target, data)
        if existing is not None:
            return existing
        asset_id = f"va_{uuid.uuid4().hex[:16]}"
        row = {
            "asset_id": asset_id,
            "person_id": person_id,
            "uploader_id": str(getattr(access, "actor_id", "") or ""),
            "actor_id": str(getattr(access, "actor_id", "") or ""),
            "controller_id": controller_id,
            "rel_path": rel_path,
            "sha256": info["sha256"],
            "mime": info["mime"],
            "bytes": info["bytes"],
            "width": info["width"],
            "height": info["height"],
            "view": view,
            "purpose": purpose,
            "origin": origin,
            "parent_asset_id": parent_asset_id,
            "status": status,
            "source_ref": source_ref,
            "created_at": now_iso(),
        }
        self.store.insert_asset(row)
        row["bytes"] = info["bytes"]
        return row

    def _asset_lineage(self, asset: dict[str, Any] | None) -> list[dict[str, Any]]:
        """复用素材保留来源链；复制字节或换目标不能复活已停用/删除的来源。"""
        lineage: list[dict[str, Any]] = []
        seen: set[str] = set()
        while asset is not None:
            asset_id = str(asset.get("asset_id") or "")
            if not asset_id or asset_id in seen:
                raise VisualError("asset_invalid", "素材来源链无效")
            seen.add(asset_id)
            if str(asset.get("status")) != "available":
                raise VisualError("cancelled_by_revocation", "素材或其来源已停用/隔离")
            self.store.require_subject_active(str(asset.get("person_id") or ""))
            lineage.append(asset)
            parent_id = str(asset.get("parent_asset_id") or "")
            if not parent_id:
                return lineage
            asset = self.store.asset(parent_id)
        raise VisualError("required_asset_missing", "参考图来源已不可用")

    def _read_asset_bytes(self, asset: dict[str, Any] | None) -> bytes:
        self._asset_lineage(asset)
        assert asset is not None
        rel_path = str(asset.get("rel_path") or "")
        target = self.assets_root / rel_path
        if not rel_path or not is_within(self.assets_root, target) or not target.is_file():
            raise VisualError("required_asset_missing", "参考图文件已丢失")
        data = target.read_bytes()
        expected = str(asset.get("sha256") or "")
        if expected and sha256_bytes(data) != expected:
            raise VisualError("asset_invalid", "参考图内容与登记哈希不一致")
        if len(data) > MAX_IMAGE_BYTES:
            raise VisualError("asset_invalid", "参考图超过单张 20MB 限制")
        return data

    def _first_reference_asset(self, set_id: str) -> str:
        items = self.store.reference_items(set_id)
        return str(items[0]["asset_id"]) if items else ""

    async def _load_extra_reference(
        self, source: str, access: Any
    ) -> tuple[bytes, str, list[dict[str, Any]]]:
        raw = str(source or "").strip()
        if not raw:
            raise VisualError("invalid_argument", "附加参考不能为空")
        if raw.startswith(("http://", "https://", "data:", "base64://")):
            raise VisualError(
                "asset_invalid",
                "人物生图的附加参考请使用本地缓存路径或受管资产句柄；远程 URL 请先由聊天缓存",
            )
        guard = await self.guard_path(raw, access, "upload_generate")
        if guard["managed"]:
            if not guard["allowed"]:
                raise VisualError(guard["code"] or "consent_required", "附加参考不可用或不在当前会话范围内")
            asset = self.store.asset(str(guard.get("asset_id") or ""))
            return (
                self._read_asset_bytes(asset),
                str(guard.get("asset_id") or ""),
                guard["bindings"],
            )
        path = self._local_path(raw)
        if not path.is_file():
            raise VisualError("asset_invalid", "附加参考文件不存在")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise VisualError("asset_invalid", "附加参考超过单张 20MB 限制")
        data = path.read_bytes()
        inspect_image(data)
        return data, "", []

    def _local_path(self, raw: str) -> Path:
        """把 ``~`` 展开到人格 home，并接受 ``file://`` 形式，与旧入口一致。"""
        text = str(raw or "").strip()
        if text.lower().startswith("file:"):
            parsed = urlparse(text)
            local = unquote(parsed.path or "")
            if parsed.netloc and parsed.netloc.lower() != "localhost":
                local = f"//{parsed.netloc}{local}"
            if re.match(r"^/[A-Za-z]:", local):
                local = local[1:]
            return Path(local)
        if text.startswith("~"):
            rest = text[2:] if text[1:2] in {"/", "\\"} else text[1:]
            return (self.persona_home / rest) if rest else self.persona_home
        return Path(text).expanduser()

    def _auto_grant_id(self, person_id: str, revision: int, access: Any) -> str:
        group_id = str(getattr(access, "group_id", "") or "")
        for grant in self.store.grants_for(person_id, status="active"):
            if self.store.grantee_for(grant) == str(
                getattr(access, "actor_id", "")
            ) and policies.auto_grant_applies(grant, revision, group_id):
                return str(grant["grant_id"])
        return ""

    def close(self) -> None:
        self.store.close()


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
class _Access:
    """后台/内部路径使用的轻量访问身份（字段与 PersonAccess 对齐）。"""

    __slots__ = (
        "actor_id",
        "group_id",
        "evidence_refs",
        "is_admin",
        "self_initiated",
        "self_service",
    )

    def __init__(
        self,
        *,
        actor_id: str = "",
        group_id: str = "",
        evidence_refs: tuple[str, ...] = (),
        is_admin: bool = False,
        self_initiated: bool = False,
        self_service: bool = False,
    ) -> None:
        self.actor_id = actor_id
        self.group_id = group_id
        self.evidence_refs = evidence_refs
        self.is_admin = is_admin
        self.self_initiated = self_initiated
        self.self_service = self_service


def access_group(access: Any) -> str:
    return str(getattr(access, "group_id", "") or "")


def _hash_parts(parts: list[str]) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def _reference_name(index: int, token: str, mime: str) -> str:
    extension = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/bmp": "bmp",
    }.get(str(mime or ""), "png")
    return f"{index:02d}_{safe_segment(token[-10:] or str(index), 'ref')}.{extension}"


def _image_attachments(attachments: Any) -> list[dict[str, Any]]:
    if not isinstance(attachments, list):
        return []
    images: list[dict[str, Any]] = []
    for item in attachments:
        if isinstance(item, str):
            images.append({"type": "image", "value": item, "file_path": item})
            continue
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "image") != "image":
            continue
        if str(item.get("sub_type") or "") == "1":
            # 表情包不是人物形象证据；仍保留为图片，但由调用方显式选择序号。
            pass
        images.append(item)
    return images


def _insert_audit(
    store: VisualStore,
    access: Any,
    action: str,
    object_type: str,
    object_id: str,
    key: str,
    evidence_ref: str,
    detail: dict[str, Any],
) -> str:
    audit_id = f"va_{uuid.uuid4().hex[:16]}"
    store.insert_audit(
        {
            "audit_id": audit_id,
            "actor_id": str(getattr(access, "actor_id", "") or ""),
            "action": action,
            "object_type": object_type,
            "object_id": object_id,
            "idempotency_key": key,
            "evidence_ref": evidence_ref,
            "detail": detail,
            "created_at": now_iso(),
        }
    )
    return audit_id


def _audit_detail(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("detail") or "{}"
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(str(raw))
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _cancel_and_refund(store: VisualStore, job_id: str, code: str) -> None:
    """未外部提交的取消按持久化边界退还额度，避免重复扣费。"""
    job = store.job(job_id)
    if job is not None:
        lifecycle.cancel_binding(store, job, code, refund=True)


_MISSING = object()


def _group_from_scope(scope: str) -> str:
    text = str(scope or "")
    if text.startswith("group:"):
        return text.split(":", 1)[1]
    return ""
