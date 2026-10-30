"""视觉资产的 SQLite 存储层。

长期权威元数据放在当前人格的同一个 ``persona.db`` 里（PRD DATA-01／13.1），
不另建第二套身份库；表统一使用 ``vs_`` 前缀避免与既有表冲突。本层只做 SQL，
权限、幂等与生命周期语义在 ``service``／``policies``／``lifecycle`` 中。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sirius_pulse.utils.sqlite_base import open_sqlite_connection

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vs_profile (
    profile_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'identity',
    scope TEXT NOT NULL DEFAULT 'persona',
    active_revision INTEGER,
    head_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(person_id, purpose, scope)
);

CREATE TABLE IF NOT EXISTS vs_revision (
    profile_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    person_id TEXT NOT NULL,
    status TEXT NOT NULL,
    origin TEXT NOT NULL,
    identity_description TEXT NOT NULL DEFAULT '',
    default_outfit TEXT NOT NULL DEFAULT '',
    variant_notes TEXT NOT NULL DEFAULT '',
    inferred_details TEXT NOT NULL DEFAULT '',
    recipe TEXT NOT NULL DEFAULT '',
    recipe_version TEXT NOT NULL DEFAULT '',
    parent_revision INTEGER,
    set_id TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    input_hash TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    activated_at TEXT,
    retired_at TEXT,
    PRIMARY KEY (profile_id, revision)
);

CREATE TABLE IF NOT EXISTS vs_asset (
    asset_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL DEFAULT '',
    uploader_id TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL DEFAULT '',
    controller_id TEXT NOT NULL DEFAULT '',
    rel_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    mime TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL DEFAULT 0,
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    view TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT 'identity',
    origin TEXT NOT NULL DEFAULT 'original',
    parent_asset_id TEXT,
    status TEXT NOT NULL DEFAULT 'available',
    source_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS vs_asset_person ON vs_asset(person_id);
CREATE INDEX IF NOT EXISTS vs_asset_sha ON vs_asset(person_id, sha256);

CREATE TABLE IF NOT EXISTS vs_reference_set (
    set_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL DEFAULT '',
    revision INTEGER,
    person_id TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT 'identity',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vs_reference_item (
    set_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    person_id TEXT NOT NULL DEFAULT '',
    revision INTEGER,
    purpose TEXT NOT NULL DEFAULT 'identity',
    view TEXT NOT NULL DEFAULT '',
    required INTEGER NOT NULL DEFAULT 1,
    sha256 TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (set_id, idx)
);

CREATE TABLE IF NOT EXISTS vs_grant (
    grant_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    grantee_id TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL,
    capabilities TEXT NOT NULL,
    revision_scope TEXT NOT NULL DEFAULT 'selected',
    source_revision INTEGER,
    recipe TEXT NOT NULL DEFAULT '',
    recipe_version TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    expires_at TEXT,
    max_uses INTEGER,
    used_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT NOT NULL DEFAULT '',
    evidence_ref TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS vs_grant_person ON vs_grant(person_id, status);

CREATE TABLE IF NOT EXISTS vs_job (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    purpose TEXT NOT NULL,
    person_id TEXT NOT NULL DEFAULT '',
    profile_id TEXT NOT NULL DEFAULT '',
    revision INTEGER,
    source_revision INTEGER,
    source_hash TEXT NOT NULL DEFAULT '',
    recipe TEXT NOT NULL DEFAULT '',
    recipe_version TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    input_hash TEXT NOT NULL DEFAULT '',
    manifest TEXT NOT NULL DEFAULT '{}',
    grant_id TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL DEFAULT '',
    group_id TEXT NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 1,
    queue_state TEXT NOT NULL DEFAULT 'prepared',
    external_boundary TEXT NOT NULL DEFAULT 'not_submitted',
    permit_at TEXT,
    submit_started_at TEXT,
    submitted_at TEXT,
    result_asset_id TEXT NOT NULL DEFAULT '',
    result_sha256 TEXT NOT NULL DEFAULT '',
    result_path TEXT NOT NULL DEFAULT '',
    isolated INTEGER NOT NULL DEFAULT 0,
    error_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS vs_job_person ON vs_job(person_id);
CREATE INDEX IF NOT EXISTS vs_job_grant ON vs_job(grant_id);

CREATE TABLE IF NOT EXISTS vs_audit (
    audit_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    object_type TEXT NOT NULL DEFAULT '',
    object_id TEXT NOT NULL DEFAULT '',
    expected_revision INTEGER,
    result_revision INTEGER,
    idempotency_key TEXT UNIQUE,
    evidence_ref TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vs_quota (
    bucket TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vs_setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS vs_deletion (
    confirm_token TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'person',
    preview TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    executed_at TEXT
);

CREATE TABLE IF NOT EXISTS vs_control (
    person_id TEXT PRIMARY KEY,
    controller_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vs_delivery (
    delivery_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL DEFAULT 0,
    code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VisualStore:
    """视觉资产的持久化仓库（共享人格 SQLite 连接）。"""

    def __init__(self, work_path: str | Path, *, conn: sqlite3.Connection | None = None) -> None:
        self.work_path = Path(work_path)
        self._own_conn = conn is None
        self.conn = conn or open_sqlite_connection(
            self.work_path / "persona.db",
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self._tx_depth = 0
        self._create_tables()
        self._migrate_tables()

    def _migrate_tables(self) -> None:
        """既有未带被授权人列的许可不得被任何登录用户继承。"""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(vs_grant)")}
        if "grantee_id" not in columns:
            self.conn.execute("ALTER TABLE vs_grant ADD COLUMN grantee_id TEXT NOT NULL DEFAULT ''")
            self.conn.execute("UPDATE vs_grant SET grantee_id=granted_by WHERE grantee_id=''")
            self.conn.commit()

    def subject_deleted(self, person_id: str) -> bool:
        return self.setting(f"deleted_subject:{person_id}") is not None

    def require_subject_active(self, person_id: str) -> None:
        from sirius_pulse.visual.errors import VisualError

        if self.subject_deleted(person_id):
            raise VisualError("permission_denied", "该人物资产已删除，不能重新入库或继续使用")

    def grantee_for(self, grant: dict[str, Any]) -> str:
        return str(grant.get("grantee_id") or grant.get("granted_by") or "")

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------
    def _commit(self) -> None:
        """事务内不提前提交：COMMIT 会释放 SAVEPOINT，使回滚失效。"""
        if self._tx_depth == 0:
            self.conn.commit()

    def _create_tables(self) -> None:
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        if self._own_conn:
            self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """用 SAVEPOINT 保证多语句原子性，且不影响共享连接上的既有事务。"""
        name = f"vs_tx_{self._tx_depth}"
        self.conn.execute(f"SAVEPOINT {name}")
        self._tx_depth += 1
        try:
            yield
        except Exception:
            self._tx_depth -= 1
            self.conn.execute(f"ROLLBACK TO {name}")
            self.conn.execute(f"RELEASE {name}")
            raise
        else:
            self._tx_depth -= 1
            self.conn.execute(f"RELEASE {name}")
            if self._tx_depth == 0:
                self.conn.commit()

    def fetchone(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------
    # 视觉档案与修订
    # ------------------------------------------------------------------
    def profile(self, profile_id: str) -> dict[str, Any] | None:
        return self.fetchone("SELECT * FROM vs_profile WHERE profile_id=?", (profile_id,))

    def profile_by_key(self, person_id: str, purpose: str, scope: str) -> dict[str, Any] | None:
        return self.fetchone(
            "SELECT * FROM vs_profile WHERE person_id=? AND purpose=? AND scope=?",
            (person_id, purpose, scope),
        )

    def profiles_for(self, person_id: str) -> list[dict[str, Any]]:
        return self.fetchall(
            "SELECT * FROM vs_profile WHERE person_id=? ORDER BY updated_at DESC", (person_id,)
        )

    def create_profile(self, *, person_id: str, purpose: str, scope: str) -> dict[str, Any]:
        profile_id = f"vp_{_random_id()}"
        now = now_iso()
        self.conn.execute(
            "INSERT OR IGNORE INTO vs_profile"
            "(profile_id, person_id, purpose, scope, active_revision, head_revision,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, NULL, 0, ?, ?)",
            (profile_id, person_id, purpose, scope, now, now),
        )
        self._commit()
        row = self.profile_by_key(person_id, purpose, scope)
        assert row is not None
        return row

    def bump_revision(self, profile_id: str) -> int:
        """在单条 UPDATE 内自增修订号，避免并发分配重复修订。"""
        now = now_iso()
        self.conn.execute(
            "UPDATE vs_profile SET head_revision=head_revision+1, updated_at=? WHERE profile_id=?",
            (now, profile_id),
        )
        row = self.fetchone(
            "SELECT head_revision FROM vs_profile WHERE profile_id=?", (profile_id,)
        )
        self._commit()
        if row is None:
            raise KeyError(profile_id)
        return int(row["head_revision"])

    def cas_active_revision(self, profile_id: str, expected: int | None, revision: int) -> bool:
        """预期修订 CAS：只有当前指针仍等于 expected 时才切换。"""
        now = now_iso()
        cursor = self.conn.execute(
            "UPDATE vs_profile SET active_revision=?, updated_at=?"
            " WHERE profile_id=? AND active_revision IS ?",
            (revision, now, profile_id, expected),
        )
        self._commit()
        return cursor.rowcount > 0

    def set_profile_active(self, profile_id: str, revision: int | None) -> None:
        """删除/停用流程使用：不经 CAS 直接清空或设置当前指针。"""
        self.conn.execute(
            "UPDATE vs_profile SET active_revision=?, updated_at=? WHERE profile_id=?",
            (revision, now_iso(), profile_id),
        )
        self._commit()

    def insert_revision(self, row: dict[str, Any]) -> None:
        columns = (
            "profile_id, revision, person_id, status, origin, identity_description,"
            " default_outfit, variant_notes, inferred_details, recipe, recipe_version,"
            " parent_revision, set_id, prompt, input_hash, created_by, created_at"
        )
        values = [
            row.get("profile_id"),
            int(row["revision"]),
            str(row.get("person_id") or ""),
            str(row.get("status") or "draft"),
            str(row.get("origin") or "upload"),
            str(row.get("identity_description") or ""),
            str(row.get("default_outfit") or ""),
            str(row.get("variant_notes") or ""),
            str(row.get("inferred_details") or ""),
            str(row.get("recipe") or ""),
            str(row.get("recipe_version") or ""),
            row.get("parent_revision"),
            str(row.get("set_id") or ""),
            str(row.get("prompt") or ""),
            str(row.get("input_hash") or ""),
            str(row.get("created_by") or ""),
            str(row.get("created_at") or now_iso()),
        ]
        self.conn.execute(
            f"INSERT INTO vs_revision({columns}) VALUES ({', '.join('?' for _ in values)})",
            values,
        )
        self._commit()

    def revision(self, profile_id: str, revision: int) -> dict[str, Any] | None:
        return self.fetchone(
            "SELECT * FROM vs_revision WHERE profile_id=? AND revision=?",
            (profile_id, revision),
        )

    def revisions_for(
        self, person_id: str, *, statuses: tuple[str, ...] | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM vs_revision WHERE person_id=?"
        params: list[Any] = [person_id]
        if statuses:
            sql += f" AND status IN ({', '.join('?' for _ in statuses)})"
            params.extend(statuses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        return self.fetchall(sql, params)

    def set_revision_status(
        self,
        profile_id: str,
        revision: int,
        status: str,
        *,
        activated: bool = False,
        retired: bool = False,
    ) -> None:
        fields = ["status=?"]
        params: list[Any] = [status]
        if activated:
            fields.append("activated_at=?")
            params.append(now_iso())
        if retired:
            fields.append("retired_at=?")
            params.append(now_iso())
        params.extend([profile_id, revision])
        self.conn.execute(
            f"UPDATE vs_revision SET {', '.join(fields)} WHERE profile_id=? AND revision=?",
            params,
        )
        self._commit()

    def set_revision_input(
        self,
        profile_id: str,
        revision: int,
        *,
        prompt: str,
        input_hash: str,
        set_id: str,
    ) -> None:
        """只允许在修订尚未提交（draft）时写输入快照；已提交内容不可改写。"""
        self.conn.execute(
            "UPDATE vs_revision SET prompt=?, input_hash=?, set_id=?"
            " WHERE profile_id=? AND revision=? AND status='draft'",
            (prompt, input_hash, set_id, profile_id, revision),
        )
        self._commit()

    # ------------------------------------------------------------------
    # 资产
    # ------------------------------------------------------------------
    def insert_asset(self, row: dict[str, Any]) -> None:
        columns = (
            "asset_id, person_id, uploader_id, actor_id, controller_id, rel_path, sha256,"
            " mime, bytes, width, height, view, purpose, origin, parent_asset_id, status,"
            " source_ref, created_at"
        )
        values = [row.get(key) for key in columns.split(", ")]
        self.conn.execute(
            f"INSERT INTO vs_asset({columns}) VALUES ({', '.join('?' for _ in values)})",
            values,
        )
        self._commit()

    def asset(self, asset_id: str) -> dict[str, Any] | None:
        return self.fetchone("SELECT * FROM vs_asset WHERE asset_id=?", (asset_id,))

    def asset_by_rel(self, rel_path: str) -> dict[str, Any] | None:
        return self.fetchone("SELECT * FROM vs_asset WHERE rel_path=?", (rel_path,))

    def assets_for(
        self, person_id: str, *, status: str = "available", limit: int = 50
    ) -> list[dict[str, Any]]:
        return self.fetchall(
            "SELECT * FROM vs_asset WHERE person_id=? AND status=?"
            " ORDER BY created_at DESC LIMIT ?",
            (person_id, status, int(limit)),
        )

    def assets_by_sha(self, sha256: str) -> list[dict[str, Any]]:
        return self.fetchall("SELECT * FROM vs_asset WHERE sha256=?", (sha256,))

    def set_asset_status(self, asset_id: str, status: str) -> None:
        self.conn.execute("UPDATE vs_asset SET status=? WHERE asset_id=?", (status, asset_id))
        self._commit()

    def mark_asset_deleted(self, asset_id: str) -> None:
        self.conn.execute(
            "UPDATE vs_asset SET status='deleted', rel_path=? WHERE asset_id=?",
            (f"__deleted__/{asset_id}", asset_id),
        )
        self._commit()

    # ------------------------------------------------------------------
    # 参考集
    # ------------------------------------------------------------------
    def create_reference_set(
        self, *, set_id: str, profile_id: str, revision: int | None, person_id: str, purpose: str
    ) -> None:
        self.conn.execute(
            "INSERT INTO vs_reference_set(set_id, profile_id, revision, person_id, purpose,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (set_id, profile_id, revision, person_id, purpose, now_iso()),
        )
        self._commit()

    def add_reference_item(self, set_id: str, item: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO vs_reference_item(set_id, idx, asset_id, person_id, revision,"
            " purpose, view, required, sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                set_id,
                int(item["idx"]),
                item["asset_id"],
                item.get("person_id", ""),
                item.get("revision"),
                item.get("purpose", "identity"),
                item.get("view", ""),
                1 if item.get("required", True) else 0,
                item.get("sha256", ""),
            ),
        )
        self._commit()

    def reference_set(self, set_id: str) -> dict[str, Any] | None:
        return self.fetchone("SELECT * FROM vs_reference_set WHERE set_id=?", (set_id,))

    def reference_items(self, set_id: str) -> list[dict[str, Any]]:
        return self.fetchall(
            "SELECT * FROM vs_reference_item WHERE set_id=? ORDER BY idx", (set_id,)
        )

    # ------------------------------------------------------------------
    # 授权
    # ------------------------------------------------------------------
    def insert_grant(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO vs_grant(grant_id, person_id, granted_by, grantee_id, mode, capabilities,"
            " revision_scope, source_revision, recipe, recipe_version, scope, expires_at,"
            " max_uses, used_count, status, created_at, evidence_ref, idempotency_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'active', ?, ?, ?)",
            (
                row["grant_id"],
                row["person_id"],
                row["granted_by"],
                self.grantee_for(row),
                row["mode"],
                json.dumps(list(row.get("capabilities") or []), ensure_ascii=False),
                row.get("revision_scope", "selected"),
                row.get("source_revision"),
                row.get("recipe", ""),
                row.get("recipe_version", ""),
                row.get("scope", ""),
                row.get("expires_at"),
                row.get("max_uses"),
                row.get("created_at", now_iso()),
                row.get("evidence_ref", ""),
                row.get("idempotency_key") or None,
            ),
        )
        self._commit()

    def grant(self, grant_id: str) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM vs_grant WHERE grant_id=?", (grant_id,))
        if row is not None:
            try:
                row["capabilities"] = json.loads(row.get("capabilities") or "[]")
            except json.JSONDecodeError:
                row["capabilities"] = []
        return row

    def grants_for(self, person_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM vs_grant WHERE person_id=?"
        params: list[Any] = [person_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        rows = self.fetchall(sql, params)
        for row in rows:
            try:
                row["capabilities"] = json.loads(row.get("capabilities") or "[]")
            except json.JSONDecodeError:
                row["capabilities"] = []
        return rows

    def revoke_grant(self, grant_id: str, reason: str = "") -> bool:
        cursor = self.conn.execute(
            "UPDATE vs_grant SET status='revoked', revoked_at=?, revoke_reason=?"
            " WHERE grant_id=? AND status='active'",
            (now_iso(), reason, grant_id),
        )
        self._commit()
        return cursor.rowcount > 0

    def consume_grant(self, grant_id: str) -> bool:
        cursor = self.conn.execute(
            "UPDATE vs_grant SET used_count=used_count+1 WHERE grant_id=? AND status='active'"
            " AND (max_uses IS NULL OR used_count < max_uses)",
            (grant_id,),
        )
        self._commit()
        return cursor.rowcount > 0

    def refund_grant(self, grant_id: str) -> bool:
        """归还未真正提交上游的一次授权使用；下限 0，不能变成负计数。"""
        cursor = self.conn.execute(
            "UPDATE vs_grant SET used_count=used_count-1 WHERE grant_id=? AND used_count>0",
            (grant_id,),
        )
        self._commit()
        return cursor.rowcount > 0

    def update_grant_capabilities(self, grant_id: str, capabilities: list[str]) -> bool:
        cursor = self.conn.execute(
            "UPDATE vs_grant SET capabilities=? WHERE grant_id=? AND status='active'",
            (json.dumps(list(capabilities), ensure_ascii=False), grant_id),
        )
        self._commit()
        return cursor.rowcount > 0

    def grant_by_idempotency(self, key: str) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM vs_grant WHERE idempotency_key=?", (key,))
        if row is not None:
            try:
                row["capabilities"] = json.loads(row.get("capabilities") or "[]")
            except json.JSONDecodeError:
                row["capabilities"] = []
        return row

    def auto_grants(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """所有仍有效的自动候选授权（后台控制器扫描用）。"""
        rows = self.fetchall(
            "SELECT * FROM vs_grant WHERE status='active' AND mode='auto_candidate'"
            " ORDER BY created_at ASC LIMIT ?",
            (int(limit),),
        )
        for row in rows:
            try:
                row["capabilities"] = json.loads(row.get("capabilities") or "[]")
            except json.JSONDecodeError:
                row["capabilities"] = []
        return rows

    # ------------------------------------------------------------------
    # 生成绑定
    # ------------------------------------------------------------------
    def insert_job(self, row: dict[str, Any]) -> None:
        columns = (
            "job_id, idempotency_key, purpose, person_id, profile_id, revision,"
            " source_revision, source_hash, recipe, recipe_version, prompt, input_hash,"
            " manifest, grant_id, actor_id, group_id, attempt, queue_state,"
            " external_boundary, created_at, updated_at"
        )
        values = [
            row.get("job_id"),
            row.get("idempotency_key") or None,
            row["purpose"],
            row.get("person_id", ""),
            row.get("profile_id", ""),
            row.get("revision"),
            row.get("source_revision"),
            row.get("source_hash", ""),
            row.get("recipe", ""),
            row.get("recipe_version", ""),
            row.get("prompt", ""),
            row.get("input_hash", ""),
            json.dumps(row.get("manifest") or {}, ensure_ascii=False),
            row.get("grant_id", ""),
            row.get("actor_id", ""),
            row.get("group_id", ""),
            int(row.get("attempt", 1)),
            row.get("queue_state", "prepared"),
            row.get("external_boundary", "not_submitted"),
            row.get("created_at", now_iso()),
            row.get("updated_at", now_iso()),
        ]
        self.conn.execute(
            f"INSERT INTO vs_job({columns}) VALUES ({', '.join('?' for _ in values)})",
            values,
        )
        self._commit()

    def _decode_job(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        try:
            row["manifest"] = json.loads(row.get("manifest") or "{}")
        except json.JSONDecodeError:
            row["manifest"] = {}
        return row

    def job(self, job_id: str) -> dict[str, Any] | None:
        return self._decode_job(self.fetchone("SELECT * FROM vs_job WHERE job_id=?", (job_id,)))

    def job_by_idempotency(self, key: str) -> dict[str, Any] | None:
        if not key:
            return None
        return self._decode_job(
            self.fetchone("SELECT * FROM vs_job WHERE idempotency_key=?", (key,))
        )

    def update_job(
        self,
        job_id: str,
        *,
        only_states: tuple[str, ...] | None = None,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """按状态守卫更新任务，返回更新后的行；状态不匹配返回 None。"""
        fields = dict(fields)
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{key}=?" for key in fields)
        params: list[Any] = list(fields.values())
        sql = f"UPDATE vs_job SET {assignments} WHERE job_id=?"
        params.append(job_id)
        if only_states:
            sql += f" AND queue_state IN ({', '.join('?' for _ in only_states)})"
            params.extend(only_states)
        cursor = self.conn.execute(sql, params)
        self._commit()
        if cursor.rowcount <= 0:
            return None
        return self.job(job_id)

    def jobs_for(self, person_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.fetchall(
            "SELECT * FROM vs_job WHERE person_id=?"
            " OR EXISTS (SELECT 1 FROM json_each(vs_job.manifest,'$.subjects') s"
            " WHERE json_extract(s.value,'$.person_id')=?)"
            " OR EXISTS (SELECT 1 FROM json_each(vs_job.manifest,'$.additional_subjects') s"
            " WHERE json_extract(s.value,'$.person_id')=?)"
            " ORDER BY created_at DESC LIMIT ?",
            (person_id, person_id, person_id, int(limit)),
        )
        return [self._decode_job(row) or row for row in rows]

    def jobs_by_grant(self, grant_id: str) -> list[dict[str, Any]]:
        rows = self.fetchall(
            "SELECT * FROM vs_job WHERE grant_id=?"
            " OR EXISTS (SELECT 1 FROM json_each(vs_job.manifest,'$.subjects') s"
            " WHERE json_extract(s.value,'$.grant_id')=?)"
            " OR EXISTS (SELECT 1 FROM json_each(vs_job.manifest,'$.additional_subjects') s"
            " WHERE json_extract(s.value,'$.grant_id')=?)",
            (grant_id, grant_id, grant_id),
        )
        return [self._decode_job(row) or row for row in rows]

    def visual_jobs_in_states(self, states: tuple[str, ...], *, limit: int = 50) -> list[dict]:
        if not states:
            return []
        sql = (
            "SELECT * FROM vs_job WHERE queue_state IN ("
            + ", ".join("?" for _ in states)
            + ") ORDER BY created_at ASC LIMIT ?"
        )
        rows = self.fetchall(sql, (*states, int(limit)))
        return [self._decode_job(row) or row for row in rows]

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def audit_by_idempotency(self, key: str) -> dict[str, Any] | None:
        if not key:
            return None
        return self.fetchone("SELECT * FROM vs_audit WHERE idempotency_key=?", (key,))

    def insert_audit(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO vs_audit(audit_id, actor_id, action, object_type, object_id,"
            " expected_revision, result_revision, idempotency_key, evidence_ref, detail,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["audit_id"],
                row.get("actor_id", ""),
                row["action"],
                row.get("object_type", ""),
                row.get("object_id", ""),
                row.get("expected_revision"),
                row.get("result_revision"),
                row.get("idempotency_key") or None,
                row.get("evidence_ref", ""),
                json.dumps(row.get("detail") or {}, ensure_ascii=False),
                row.get("created_at", now_iso()),
            ),
        )
        self._commit()

    # ------------------------------------------------------------------
    # 配额与设置
    # ------------------------------------------------------------------
    def reserve_quota(self, bucket: str, day: str, limit: int) -> bool:
        """原子预占一次额度；同一天超过 limit 返回 False。"""
        cursor = self.conn.execute(
            "INSERT INTO vs_quota(bucket, day, used) VALUES (?, ?, 1)"
            " ON CONFLICT(bucket) DO UPDATE SET day=excluded.day,"
            " used=CASE WHEN vs_quota.day=excluded.day THEN vs_quota.used+1 ELSE 1 END"
            " WHERE vs_quota.day<>excluded.day OR vs_quota.used < ?",
            (bucket, day, int(limit)),
        )
        self._commit()
        return cursor.rowcount > 0

    def quota_used(self, bucket: str, day: str) -> int:
        row = self.fetchone("SELECT used FROM vs_quota WHERE bucket=? AND day=?", (bucket, day))
        return int(row["used"]) if row else 0

    def release_quota(self, bucket: str, day: str) -> None:
        """只用于「尚未触达外部提交」的取消；未知上游结果不返还。"""
        self.conn.execute(
            "UPDATE vs_quota SET used=CASE WHEN used>0 THEN used-1 ELSE 0 END"
            " WHERE bucket=? AND day=?",
            (bucket, day),
        )
        self._commit()

    def setting(self, key: str) -> Any:
        row = self.fetchone("SELECT value FROM vs_setting WHERE key=?", (key,))
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set_setting(self, key: str, value: Any, *, actor_id: str = "") -> None:
        self.conn.execute(
            "INSERT INTO vs_setting(key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (key, json.dumps(value, ensure_ascii=False), now_iso(), actor_id),
        )
        self._commit()

    # ------------------------------------------------------------------
    # 删除确认、控制者、外发回执
    # ------------------------------------------------------------------
    def insert_deletion(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO vs_deletion(confirm_token, actor_id, subject_id, scope, preview,"
            " created_at, expires_at, used) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (
                row["confirm_token"],
                row["actor_id"],
                row["subject_id"],
                row.get("scope", "person"),
                json.dumps(row.get("preview") or {}, ensure_ascii=False),
                row["created_at"],
                row["expires_at"],
            ),
        )
        self._commit()

    def deletion(self, token: str) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM vs_deletion WHERE confirm_token=?", (token,))
        if row is not None:
            try:
                row["preview"] = json.loads(row.get("preview") or "{}")
            except json.JSONDecodeError:
                row["preview"] = {}
        return row

    def mark_deletion_used(self, token: str) -> bool:
        cursor = self.conn.execute(
            "UPDATE vs_deletion SET used=1, executed_at=? WHERE confirm_token=? AND used=0",
            (now_iso(), token),
        )
        self._commit()
        return cursor.rowcount > 0

    def set_control(self, person_id: str, controller_id: str) -> None:
        self.conn.execute(
            "INSERT INTO vs_control(person_id, controller_id, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT(person_id) DO UPDATE SET controller_id=excluded.controller_id",
            (person_id, controller_id, now_iso()),
        )
        self._commit()

    def control(self, person_id: str) -> str:
        row = self.fetchone("SELECT controller_id FROM vs_control WHERE person_id=?", (person_id,))
        return str(row["controller_id"]) if row else ""

    def insert_delivery(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO vs_delivery(delivery_id, asset_id, job_id, actor_id, channel,"
            " target, message_id, success, code, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["delivery_id"],
                row.get("asset_id", ""),
                row.get("job_id", ""),
                row.get("actor_id", ""),
                row.get("channel", ""),
                row.get("target", ""),
                row.get("message_id", ""),
                1 if row.get("success") else 0,
                row.get("code", ""),
                row.get("created_at", now_iso()),
            ),
        )
        self._commit()


def _random_id() -> str:
    import uuid

    return uuid.uuid4().hex[:16]
