"""删除/停用清理：影响预览、一次性确认与实际清理范围。

PRD AUTH-04 要求如实说明边界：本地原图、派生、任务副本与索引引用可以清理；
外部服务副本、已发送消息与离线备份不在本进程控制范围内，不能声称彻底删除。
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.visual.errors import VisualError
from sirius_pulse.visual.images import is_within, sha256_bytes
from sirius_pulse.visual.store import VisualStore, now_iso

CONFIRM_TTL_MINUTES = 15

_UNRESOLVED = [
    "外部生成服务已收到的请求与其副本不在本地清理范围",
    "已发送到聊天平台的消息与平台侧缓存无法撤回",
    "离线备份需按部署方保留策略单独处理",
    "逻辑字段擦除不等于 SQLite 空闲页、WAL、磁盘历史或已进入执行内存的安全擦除",
]
_RETAINED = [
    "人物身份、称呼、声明、原聊天和来源证据不在本次视觉资产删除范围",
    "仅保留阻止重放与核对计费所需的 ID、哈希、状态和脱敏操作墓碑",
]


def _artifact_root(work_path: Path, tool: str) -> Path:
    return work_path / "tool_data" / "artifacts" / tool


def _person_asset_rows(store: VisualStore, person_id: str) -> list[dict[str, Any]]:
    """返回人物资产及所有 parent descendants，跨主体复制也随来源删除。"""
    assets = {
        row["asset_id"]: row
        for row in store.fetchall("SELECT * FROM vs_asset WHERE person_id=?", (person_id,))
    }
    for job in store.jobs_for(person_id, limit=-1):
        result = store.asset(str(job.get("result_asset_id") or ""))
        if result:
            assets[result["asset_id"]] = result
    all_assets = store.fetchall("SELECT * FROM vs_asset")
    changed = True
    while changed:
        changed = False
        for row in all_assets:
            asset_id = str(row.get("asset_id") or "")
            parent_id = str(row.get("parent_asset_id") or "")
            if asset_id and parent_id in assets and asset_id not in assets:
                assets[asset_id] = row
                changed = True
    return list(assets.values())


def _person_job_rows(
    store: VisualStore, person_id: str, assets: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """返回人物任务及引用其资产的任务，清理副本和队列墓碑不留孤儿。"""
    asset_ids = {str(asset.get("asset_id") or "") for asset in assets}
    source_job_ids = {
        str(asset.get("source_ref") or "") for asset in assets if str(asset.get("source_ref") or "")
    }
    jobs: dict[str, dict[str, Any]] = {}
    for job in store.jobs_for(person_id, limit=-1):
        jobs[str(job.get("job_id") or "")] = job
    for row in store.fetchall("SELECT job_id FROM vs_job"):
        job_id = str(row.get("job_id") or "")
        job = store.job(job_id)
        if job is None:
            continue
        manifest = job.get("manifest") if isinstance(job.get("manifest"), dict) else {}
        references = (
            manifest.get("references") if isinstance(manifest.get("references"), list) else []
        )
        referenced_ids = {
            str(ref.get("asset_id") or "") for ref in references if isinstance(ref, dict)
        }
        if (
            str(job.get("person_id") or "") == person_id
            or str(job.get("result_asset_id") or "") in asset_ids
            or job_id in source_job_ids
            or bool(asset_ids & referenced_ids)
        ):
            jobs[job_id] = job
    return list(jobs.values())


def _job_ids(store: VisualStore, person_id: str) -> list[str]:
    assets = _person_asset_rows(store, person_id)
    return [str(job.get("job_id") or "") for job in _person_job_rows(store, person_id, assets)]


def _counts(store: VisualStore, work_path: Path, person_id: str) -> dict[str, Any]:
    assets = _person_asset_rows(store, person_id)
    originals = [row for row in assets if str(row.get("origin")) == "original"]
    derived = [row for row in assets if str(row.get("origin")) != "original"]
    job_ids = _job_ids(store, person_id)
    task_copies = 0
    image_root = _artifact_root(work_path, "image_generate")
    for job_id in job_ids:
        reference_dir = image_root / "references" / job_id
        if reference_dir.is_dir():
            task_copies += sum(1 for item in reference_dir.iterdir() if item.is_file())
    return {
        "originals": len(originals),
        "derived": len(derived),
        "assets": len(assets),
        "jobs": len(job_ids),
        "task_copies": task_copies,
        "revisions": len(store.revisions_for(person_id, limit=-1)),
        "grants": len(store.grants_for(person_id)),
        "bytes": sum(int(row.get("bytes") or 0) for row in assets),
    }


def prepare_deletion(
    store: VisualStore,
    work_path: Path,
    *,
    person_id: str,
    actor_id: str,
    scope: str = "person",
) -> dict[str, Any]:
    """返回影响预览与一次性确认引用；此时不删除任何东西。"""
    if scope != "person":
        raise VisualError("invalid_argument", "当前仅支持按人物删除视觉资产，不支持身份或聊天删除")
    token = f"del_{uuid.uuid4().hex}"
    created = datetime.now(timezone.utc)
    expires = created + timedelta(minutes=CONFIRM_TTL_MINUTES)
    preview = {
        "scope": scope,
        "counts": _counts(store, work_path, person_id),
        "unresolved": list(_UNRESOLVED),
        "retained": list(_RETAINED),
        "note": "确认后清理本地视觉文件、修订描述、任务输入及引用链接；只留脱敏墓碑。",
    }
    store.insert_deletion(
        {
            "confirm_token": token,
            "actor_id": actor_id,
            "subject_id": person_id,
            "scope": scope,
            "preview": preview,
            "created_at": created.isoformat(),
            "expires_at": expires.isoformat(),
        }
    )
    return {
        "status": "confirm_required",
        "confirm_token": token,
        "expires_at": expires.isoformat(),
        "preview": preview,
    }


def _scrub_queue(work_path: Path, job_ids: set[str]) -> int:
    """复用原队列格式与原子保存；终态墓碑不再携带输入或触发回调。"""
    from sirius_pulse.tools.data_store import ToolDataStore

    path = work_path / "tool_data" / "image_generate.json"
    if not path.is_file():
        return 0
    if not isinstance(json.loads(path.read_text(encoding="utf-8")), dict):
        raise ValueError("生图队列格式无效")
    queue = ToolDataStore(path)
    with queue.transaction():
        queue.reload()
        jobs = queue.get("jobs", [])
        if not isinstance(jobs, list):
            raise ValueError("生图队列格式无效")
        scrubbed = 0
        replacements = []
        for job in jobs:
            if isinstance(job, dict) and str(job.get("id") or "") in job_ids:
                replacements.append(
                    {
                        "id": job["id"],
                        "status": "cancelled",
                        "deleted": True,
                        "isolated": True,
                        "artifact_suppressed": True,
                        "error_code": "cancelled_by_revocation",
                        "visual": {"deleted": True},
                        "callback_state": "skipped",
                        "notified": True,
                    }
                )
                scrubbed += 1
            else:
                replacements.append(job)
        if scrubbed:
            queue.set("jobs", replacements)
            queue.save()
        return scrubbed


def _scrub_metadata(
    store: VisualStore, person_id: str, assets: list[dict[str, Any]], jobs: list[dict[str, Any]]
) -> dict[str, int]:
    """擦除视觉内容与派生链接，只保留撤权、幂等和计费核对所需墓碑。"""
    revisions = {
        (row["profile_id"], int(row["revision"]))
        for row in store.revisions_for(person_id, limit=-1)
    }
    sets = {
        row["set_id"]
        for row in store.fetchall(
            "SELECT set_id FROM vs_reference_set WHERE person_id=?", (person_id,)
        )
    }
    identifiers = {person_id}
    for asset in assets:
        identifiers.add(str(asset["asset_id"]))
        for row in store.fetchall(
            "SELECT s.set_id,s.profile_id,s.revision FROM vs_reference_set s"
            " JOIN vs_reference_item i ON i.set_id=s.set_id WHERE i.asset_id=?",
            (asset["asset_id"],),
        ):
            sets.add(row["set_id"])
            if row["profile_id"] and row["revision"] is not None:
                revisions.add((row["profile_id"], int(row["revision"])))
        store.conn.execute("UPDATE vs_asset SET view='' WHERE asset_id=?", (asset["asset_id"],))
    for profile_id, revision in revisions:
        identifiers.add(profile_id)
        store.conn.execute(
            "UPDATE vs_revision SET identity_description='',default_outfit='',variant_notes='',"
            " inferred_details='',prompt='',set_id='',status='retired',retired_at=?"
            " WHERE profile_id=? AND revision=?",
            (now_iso(), profile_id, revision),
        )
        store.conn.execute(
            "UPDATE vs_profile SET active_revision=NULL WHERE profile_id=? AND active_revision=?",
            (profile_id, revision),
        )
    for job in jobs:
        identifiers.add(str(job["job_id"]))
        # 同时保存预算是否已退标记；删除不能使未知上游被重放或被重复退款。
        current = store.job(job["job_id"]) or job
        manifest = current.get("manifest") or {}
        tombstone = {
            "deleted": True,
            **{
                key: bool(manifest[key])
                for key in (
                    "budget_refunded",
                    "grants_refunded",
                    "deployment_charged",
                    "auto_reserved",
                )
                if key in manifest
            },
        }
        store.update_job(
            job["job_id"], prompt="", manifest=json.dumps(tombstone), result_path="", isolated=1
        )
    for set_id in sets:
        identifiers.add(set_id)
        store.conn.execute("DELETE FROM vs_reference_item WHERE set_id=?", (set_id,))
        store.conn.execute("DELETE FROM vs_reference_set WHERE set_id=?", (set_id,))
    for grant in store.grants_for(person_id):
        identifiers.add(str(grant["grant_id"]))
        store.conn.execute(
            "UPDATE vs_grant SET revoke_reason='',evidence_ref='' WHERE grant_id=?",
            (grant["grant_id"],),
        )
    audits = 0
    for audit in store.fetchall("SELECT audit_id,object_id,detail FROM vs_audit"):
        content = str(audit.get("object_id") or "") + str(audit.get("detail") or "")
        if not any(identifier and identifier in content for identifier in identifiers):
            continue
        object_id = str(audit.get("object_id") or "")
        if object_id not in identifiers and object_id.split(":", 1)[0] not in identifiers:
            object_id = "[redacted]"
        store.conn.execute(
            "UPDATE vs_audit SET detail=?,evidence_ref='',object_id=? WHERE audit_id=?",
            ('{"redacted":true}', object_id, audit["audit_id"]),
        )
        audits += 1
    return {
        "revisions_scrubbed": len(revisions),
        "jobs_scrubbed": len(jobs),
        "reference_sets_removed": len(sets),
        "audits_redacted": audits,
    }


def execute_deletion(
    store: VisualStore, work_path: Path, *, token: str, actor_id: str
) -> dict[str, Any]:
    """执行一次性确认的删除；返回实际完成范围与未完成项。"""
    row = store.deletion(token)
    if row is None:
        raise VisualError("confirm_invalid", "删除确认不存在或已过期")
    if str(row.get("actor_id")) != actor_id:
        raise VisualError("permission_denied", "删除确认不属于当前操作者")
    if int(row.get("used") or 0):
        raise VisualError("confirm_invalid", "该删除确认已经执行过")
    try:
        expires = datetime.fromisoformat(str(row.get("expires_at")))
    except ValueError:
        expires = datetime.now(timezone.utc) - timedelta(seconds=1)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        raise VisualError("confirm_invalid", "删除确认已过期，请重新预览")
    person_id = str(row.get("subject_id") or "")
    with store.transaction():
        if not store.mark_deletion_used(token):
            raise VisualError("confirm_invalid", "该删除确认已经执行过")
        store.set_setting(f"deleted_subject:{person_id}", True, actor_id=actor_id)
    for profile in store.profiles_for(person_id):
        store.set_profile_active(str(profile["profile_id"]), None)
    assets = _person_asset_rows(store, person_id)
    sha_set = {str(asset.get("sha256") or "") for asset in assets if asset.get("sha256")}
    visual_root = work_path / "visual_assets"
    removed_files = 0
    removed_dirs = 0
    task_copies = 0
    leftovers = 0

    for asset in assets:
        rel_path = str(asset.get("rel_path") or "")
        if not rel_path:
            continue
        target = visual_root / rel_path
        if is_within(visual_root, target) and target.is_file():
            try:
                target.unlink()
                removed_files += 1
            except OSError:
                leftovers += 1
        store.mark_asset_deleted(str(asset["asset_id"]))

    person_dir = visual_root / _safe_segment(person_id)
    if person_id and is_within(visual_root, person_dir) and person_dir.is_dir():
        try:
            shutil.rmtree(person_dir)
            removed_dirs += 1
        except OSError:
            leftovers += 1

    image_root = _artifact_root(work_path, "image_generate")
    jobs = _person_job_rows(store, person_id, assets)
    for job in jobs:
        job_id = str(job.get("job_id") or "")
        reference_dir = image_root / "references" / job_id
        if job_id and is_within(image_root, reference_dir) and reference_dir.is_dir():
            try:
                copied = sum(1 for item in reference_dir.iterdir() if item.is_file())
                shutil.rmtree(reference_dir)
                removed_dirs += 1
                task_copies += copied
            except OSError:
                leftovers += 1
        result_prefix = image_root / "images"
        if job_id and result_prefix.is_dir():
            for item in result_prefix.glob(f"{job_id}.*"):
                try:
                    item.unlink()
                    removed_files += 1
                except OSError:
                    leftovers += 1
        state = str(job.get("queue_state") or "")
        if state in {"prepared", "enqueued", "dispatching", "permit_acquired"}:
            from sirius_pulse.visual.lifecycle import cancel_binding

            cancel_binding(store, job, "cancelled_by_revocation", refund=True)
        elif state in {"submitted", "done"}:
            store.update_job(job_id, queue_state="isolated", isolated=1)

    # read_image 的历史归一化副本可能是受管图片的另存版本；按哈希清理。
    if sha_set:
        for directory_name in ("normalized", "downloads"):
            directory = _artifact_root(work_path, "read_image") / directory_name
            if not directory.is_dir():
                continue
            for item in directory.iterdir():
                try:
                    if item.is_file() and sha256_bytes(item.read_bytes()) in sha_set:
                        item.unlink(missing_ok=True)
                        removed_files += 1
                except OSError:
                    leftovers += 1
                    continue

    for profile in store.profiles_for(person_id):
        profile_id = str(profile["profile_id"])
        store.set_profile_active(profile_id, None)
        for revision in store.revisions_for(person_id, limit=-1):
            if str(revision["profile_id"]) == profile_id:
                store.set_revision_status(
                    profile_id, int(revision["revision"]), "retired", retired=True
                )

    revoked: list[str] = []
    for grant in store.grants_for(person_id, status="active"):
        if store.revoke_grant(str(grant["grant_id"]), "资产删除"):
            revoked.append(str(grant["grant_id"]))

    with store.transaction():
        metadata = _scrub_metadata(store, person_id, assets, jobs)
    unresolved = list(_UNRESOLVED)
    queue_scrubbed = 0
    try:
        queue_scrubbed = _scrub_queue(work_path, {str(job["job_id"]) for job in jobs})
    except (OSError, ValueError, TypeError):
        unresolved.append("本地生图队列快照擦除未完成；任务已阻断，需人工清理队列文件")
    if leftovers:
        unresolved.append(f"{leftovers} 项本地文件因占用或权限未能删除，需要人工清理")

    store.insert_audit(
        {
            "audit_id": f"va_{uuid.uuid4().hex[:16]}",
            "actor_id": actor_id,
            "action": "execute_deletion",
            "object_type": "person",
            "object_id": person_id,
            "detail": {
                "removed_files": removed_files,
                "removed_dirs": removed_dirs,
                "revoked_grants": len(revoked),
                "assets": len(assets),
                "leftovers": leftovers,
            },
            "created_at": now_iso(),
        }
    )
    return {
        "status": "deleted",
        "cleaned": {
            "files": removed_files,
            "directories": removed_dirs,
            "assets": len(assets),
            "task_copies": task_copies,
            "grants_revoked": len(revoked),
            "jobs_isolated": len(jobs),
            "queue_snapshots_scrubbed": queue_scrubbed,
            **metadata,
        },
        "unresolved": unresolved,
        "retained": list(_RETAINED),
        "generation_blocked": True,
    }


def _safe_segment(value: str) -> str:
    from sirius_pulse.visual.images import safe_segment

    return safe_segment(value, "person")
