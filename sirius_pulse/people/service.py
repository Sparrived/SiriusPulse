"""Persona-scoped identities, scoped profiles and auditable evidence-bound changes."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True, slots=True)
class PersonAccess:
    actor_id: str = ""
    group_id: str = ""
    evidence_refs: tuple[str, ...] = ()
    is_admin: bool = False
    self_initiated: bool = False
    self_service: bool = False


class PersonError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class PersonService:
    """The authoritative person service; names and readable history are not credentials."""

    def __init__(
        self,
        work_path: Path | str | None,
        *,
        user_manager: Any = None,
        persona_name: str = "",
        persona_aliases: list[str] | None = None,
        conn: sqlite3.Connection | None = None,
        memory_units: Any = None,
    ) -> None:
        self.user_manager = user_manager
        self.memory_units = memory_units
        self.work_path = Path(work_path) if work_path is not None else None
        if conn is None:
            if user_manager is not None:
                conn = user_manager._storage.conn
            elif work_path is not None:
                from sirius_pulse.utils.sqlite_base import open_sqlite_connection

                conn = open_sqlite_connection(Path(work_path) / "memory.db")
            else:
                conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._persona_name = persona_name.strip()
        self._persona_aliases = tuple(persona_aliases or ())
        self._init_schema()
        with self._transaction():
            row = self.conn.execute(
                "SELECT person_id FROM people WHERE kind='persona' LIMIT 1"
            ).fetchone()
            self.persona_id = row[0] if row else str(uuid.uuid4())
            self.conn.execute(
                "INSERT OR IGNORE INTO people(person_id,display_name,kind,revision)"
                " VALUES(?,?,?,1)",
                (self.persona_id, self._persona_name, "persona"),
            )
            if self._persona_name:
                self.conn.execute(
                    "UPDATE people SET display_name=? WHERE person_id=?",
                    (self._persona_name, self.persona_id),
                )
            for index, name in enumerate((self._persona_name, *self._persona_aliases)):
                self._insert_name(
                    self.persona_id,
                    name,
                    "preferred" if index == 0 else "alias",
                    "persona",
                    "configured",
                    replace=True,
                )

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS people(
                    person_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                    controller_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS person_accounts(
                    platform TEXT NOT NULL, platform_uid TEXT NOT NULL, person_id TEXT NOT NULL,
                    PRIMARY KEY(platform,platform_uid),
                    FOREIGN KEY(person_id) REFERENCES people(person_id)
                );
                CREATE TABLE IF NOT EXISTS person_names(
                    id INTEGER PRIMARY KEY, person_id TEXT NOT NULL, name TEXT NOT NULL,
                    normalized TEXT NOT NULL, name_type TEXT NOT NULL, scope TEXT NOT NULL,
                    source TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS person_names_search ON person_names(normalized,active);
                CREATE TABLE IF NOT EXISTS person_evidence(
                    evidence_ref TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    platform TEXT NOT NULL, platform_uid TEXT NOT NULL, message_id TEXT NOT NULL,
                    content TEXT NOT NULL, attachments TEXT NOT NULL DEFAULT '[]',
                    adapter_route_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS person_claims(
                    claim_id TEXT PRIMARY KEY, person_id TEXT NOT NULL, field TEXT NOT NULL,
                    value TEXT NOT NULL, source TEXT NOT NULL, source_ref TEXT NOT NULL,
                    scope TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                    replaces TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS person_changes(
                    change_id TEXT PRIMARY KEY, idem_key TEXT UNIQUE NOT NULL,
                    person_id TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL, source_ref TEXT NOT NULL, revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS person_controllers(
                    person_id TEXT NOT NULL, actor_id TEXT NOT NULL, PRIMARY KEY(person_id,actor_id)
                );
                CREATE TABLE IF NOT EXISTS person_ask_state(
                    actor_id TEXT PRIMARY KEY, last_asked TEXT NOT NULL DEFAULT '',
                    declined INTEGER NOT NULL DEFAULT 0
                );
            """
            )
            additions = {
                "people": {"legacy_user_id": "TEXT NOT NULL DEFAULT ''"},
                "person_evidence": {
                    "created_at": "TEXT NOT NULL DEFAULT ''",
                    "account_bound": "INTEGER NOT NULL DEFAULT 0",
                    "person_id": "TEXT NOT NULL DEFAULT ''",
                },
                "person_names": {"source_ref": "TEXT NOT NULL DEFAULT ''"},
                "person_claims": {"replaces": "TEXT NOT NULL DEFAULT ''"},
                "person_changes": {
                    "expected_revision": "INTEGER",
                    "result_revision": "INTEGER",
                    "created_at": "TEXT NOT NULL DEFAULT ''",
                    "migration_id": "TEXT NOT NULL DEFAULT ''",
                },
            }
            for table, columns in additions.items():
                existing = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
                for name, definition in columns.items():
                    if name not in existing:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS people_legacy_user ON people(legacy_user_id)"
                " WHERE legacy_user_id!=''"
            )
            # 可见性检查（_visible / get_person）按 person_id + group_id 查证据；没有索引时
            # 每条消息都要全表扫一遍，而证据表每条消息都会长一行。
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS person_evidence_person_group"
                " ON person_evidence(person_id, group_id)"
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS person_migrations(
                    migration_id TEXT PRIMARY KEY, report TEXT NOT NULL, backup_path TEXT NOT NULL,
                    created_at TEXT NOT NULL, rolled_back INTEGER NOT NULL DEFAULT 0
                )
            """
            )
            self.conn.commit()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Compose with visual store savepoints on the shared SQLite connection."""
        with self._lock:
            outermost = not self.conn.in_transaction
            savepoint = "people_write"
            self.conn.execute("BEGIN IMMEDIATE" if outermost else f"SAVEPOINT {savepoint}")
            try:
                yield
            except Exception:
                if outermost:
                    self.conn.rollback()
                else:
                    self.conn.execute(f"ROLLBACK TO {savepoint}")
                    self.conn.execute(f"RELEASE {savepoint}")
                raise
            else:
                if outermost:
                    self.conn.commit()
                else:
                    self.conn.execute(f"RELEASE {savepoint}")

    @staticmethod
    def _normalize(value: str) -> str:
        return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _text(value: Any, limit: int) -> str:
        return str(value or "").strip()[:limit]

    def _hash(self, *parts: Any) -> str:
        return hashlib.sha256(
            json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

    def lookup_account(self, platform: str, platform_uid: str) -> str | None:
        row = self.conn.execute(
            "SELECT person_id FROM person_accounts WHERE platform=? AND platform_uid=?",
            (str(platform).strip().casefold(), str(platform_uid).strip()),
        ).fetchone()
        return row[0] if row else None

    def person_id_for_user(self, user_id: str) -> str | None:
        if not user_id:
            return None
        row = self.conn.execute(
            "SELECT person_id FROM people WHERE person_id=? OR legacy_user_id=?",
            (user_id, user_id),
        ).fetchone()
        return row[0] if row else None

    def record_evidence(
        self,
        *,
        group_id: str,
        user_id: str,
        platform: str,
        platform_uid: str,
        message_id: str,
        content: str,
        attachments: list[Any] | None = None,
        adapter_route_id: str = "",
    ) -> str:
        """Record an observation. An unbound message is never a write authority."""
        platform, platform_uid = (
            str(platform or "").strip().casefold(),
            str(platform_uid or "").strip(),
        )
        bound = bool(platform and platform_uid and user_id)
        with self._transaction():
            actor = (
                self._resolve_or_create_account(platform, platform_uid, user_id, "")
                if bound
                else ""
            )
            namespace = group_id if bound else str(uuid.uuid4())
            ref = "ev_" + self._hash(platform, platform_uid, group_id, message_id, namespace)[:32]
            self.conn.execute(
                "INSERT OR IGNORE INTO person_evidence"
                "(evidence_ref,group_id,user_id,platform,platform_uid,message_id,content,"
                "attachments,adapter_route_id,created_at,account_bound,person_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ref,
                    str(group_id),
                    str(user_id),
                    platform,
                    platform_uid,
                    str(message_id),
                    self._text(content, 20000),
                    json.dumps(attachments or [], ensure_ascii=False),
                    str(adapter_route_id or ""),
                    self._now(),
                    int(bound),
                    actor,
                ),
            )
            if bound:
                profile = None
                if self.user_manager is not None:
                    try:
                        profile = self.user_manager.get_user(user_id, group_id)
                    except Exception:
                        profile = None
                display_name = getattr(profile, "name", "") if profile else ""
                if display_name:
                    self._insert_name(actor, display_name, "observed", str(group_id), "platform")
            return ref

    def _resolve_or_create_account(
        self, platform: str, uid: str, user_id: str, display_name: str
    ) -> str:
        if not platform or not uid or not user_id:
            raise PersonError("account_unbound")
        existing = self.lookup_account(platform, uid)
        if existing:
            return existing
        person_id = self.person_id_for_user(user_id) or str(uuid.uuid4())
        self.conn.execute(
            "INSERT OR IGNORE INTO people(person_id,display_name,kind,legacy_user_id)"
            " VALUES(?,?,?,?)",
            (person_id, self._text(display_name, 160), "human", user_id),
        )
        self.conn.execute(
            "INSERT INTO person_accounts(platform,platform_uid,person_id) VALUES(?,?,?)",
            (platform, uid, person_id),
        )
        return person_id

    def get_evidence(self, evidence_ref: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM person_evidence WHERE evidence_ref=?", (evidence_ref,)
        ).fetchone()
        if not row:
            raise PersonError("evidence_invalid")
        result = dict(row)
        result["attachments"] = json.loads(result["attachments"])
        result["account_bound"] = bool(result.get("account_bound"))
        result["actor_id"] = (
            result.get("person_id") or self.person_id_for_user(result["user_id"]) or ""
        )
        return result

    def _trusted_evidence(self, ref: str, access: PersonAccess) -> dict[str, Any]:
        if access.self_initiated or ref not in access.evidence_refs:
            raise PersonError("evidence_invalid")
        evidence = self.get_evidence(ref)
        if evidence["group_id"] != access.group_id:
            raise PersonError("permission_denied")
        return evidence

    def _authoritative_evidence(self, ref: str, access: PersonAccess) -> dict[str, Any]:
        evidence = self._trusted_evidence(ref, access)
        if not evidence.get("account_bound") or not evidence["actor_id"]:
            raise PersonError("account_unbound")
        if not access.is_admin and evidence["actor_id"] != access.actor_id:
            raise PersonError("permission_denied")
        return evidence

    def _visible(self, person_id: str, access: PersonAccess) -> bool:
        if access.is_admin or person_id in {access.actor_id, self.persona_id}:
            return True
        if access.self_initiated and not getattr(access, "self_service", False):
            return False
        if getattr(access, "self_service", False):
            return True
        if self._is_controller(person_id, access.actor_id):
            return True
        if self.conn.execute(
            "SELECT 1 FROM person_names WHERE person_id=? AND scope IN (?,'persona')"
            " AND active=1 LIMIT 1",
            (person_id, access.group_id),
        ).fetchone():
            return True
        if self.conn.execute(
            "SELECT 1 FROM person_evidence WHERE person_id=? AND group_id=? LIMIT 1",
            (person_id, access.group_id),
        ).fetchone():
            return True
        scope, params = self._scope_clause(access)
        if self.conn.execute(
            f"SELECT 1 FROM person_claims WHERE person_id=? AND {scope} LIMIT 1",
            (person_id, *params),
        ).fetchone():
            return True
        tables = {
            row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        legacy_id = self._person_row(person_id)["legacy_user_id"]
        return bool(
            legacy_id
            and "group_members" in tables
            and self.conn.execute(
                "SELECT 1 FROM group_members WHERE user_id=? AND group_id=?",
                (legacy_id, access.group_id),
            ).fetchone()
        )

    def _scope_clause(self, access: PersonAccess) -> tuple[str, tuple[str, ...]]:
        if access.is_admin:
            return "1=1", ()
        return "scope IN (?, 'persona')", (access.group_id,)

    def _person_row(self, person_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM people WHERE person_id=?", (person_id,)).fetchone()
        if not row:
            raise PersonError("not_found_or_not_visible")
        return row

    def get_person(self, person_id: str, access: PersonAccess) -> dict[str, Any]:
        if not self._visible(person_id, access):
            raise PersonError("not_found_or_not_visible")
        row = self._person_row(person_id)
        scope, params = self._scope_clause(access)
        names = self.conn.execute(
            f"SELECT id,name,name_type,source,scope FROM person_names"
            f" WHERE person_id=? AND active=1 AND ({scope} OR person_id IN (?)) ORDER BY id",
            (person_id, *params, access.actor_id),
        ).fetchall()
        local = [n for n in names if n["scope"] == access.group_id]
        preferred = next((n["name"] for n in local if n["name_type"] == "preferred"), "")
        if not preferred:
            preferred = next((n["name"] for n in names if n["name_type"] == "preferred"), "")
        display = preferred or next((n["name"] for n in names if n["name_type"] == "observed"), "")
        if not display and (
            person_id == access.actor_id or self._is_controller(person_id, access.actor_id)
        ):
            display = row["display_name"][:160]
        own_claims = (
            "(? IN (?) OR source_ref IN (SELECT evidence_ref FROM person_evidence"
            " WHERE person_id=?) OR status='active')"
        )
        claims = self.conn.execute(
            f"SELECT claim_id,field,value,source,scope,status,replaces FROM person_claims"
            f" WHERE person_id=? AND status IN ('active','pending') AND {scope}"
            f" AND {own_claims} ORDER BY created_at DESC LIMIT 50",
            (person_id, *params, person_id, access.actor_id, access.actor_id),
        ).fetchall()
        controller = self._controller_label(row["controller_id"], access)
        return {
            "person_id": row["person_id"],
            "display_name": display or row["display_name"][:160],
            "kind": row["kind"],
            "revision": row["revision"],
            "controller_id": controller,
            "names": [dict(n) for n in names],
            "claims": [dict(c) for c in claims],
            "relationships": self._relationships(row, access),
            "memories": self._memory_summary(row, access),
        }

    def _controller_label(self, controller: str, access: PersonAccess) -> str:
        if not controller or controller.startswith(("legacy:", "web:")):
            return "" if controller.startswith("legacy:") else controller if access.is_admin else ""
        if access.is_admin or controller == access.actor_id:
            return controller
        try:
            person = self.get_person(controller, access)
            return person["display_name"]
        except PersonError:
            return ""

    def _relationships(self, row: sqlite3.Row, access: PersonAccess) -> list[dict[str, Any]]:
        if not self.user_manager:
            return []
        try:
            user_id = row["legacy_user_id"] or row["person_id"]
            user = self.user_manager.get_user(user_id, access.group_id)
            result = []
            for item in getattr(user, "relationships", []) or []:
                result.append(
                    {
                        "kind": self._text(
                            getattr(item, "relation", "")
                            if not isinstance(item, dict)
                            else item.get("kind") or item.get("type") or item.get("relation"),
                            60,
                        ),
                        "note": self._text(
                            getattr(item, "fact_hint", "")
                            if not isinstance(item, dict)
                            else item.get("note") or item.get("summary") or item.get("fact_hint"),
                            200,
                        ),
                    }
                )
            return result[:10]
        except Exception:
            return []

    def _memory_summary(self, row: sqlite3.Row, access: PersonAccess) -> dict[str, Any]:
        return {
            "availability": "existing memory retrieval only; no full history",
            "scope": access.group_id,
            "person_id": row["person_id"],
            "status": "unavailable",
            "items": [],
        }

    def memory_summary(
        self, person_id: str, access: PersonAccess, query: str = ""
    ) -> dict[str, Any]:
        self.get_person(person_id, access)
        row = self._person_row(person_id)
        result = self._memory_summary(row, access)
        result["query"] = self._text(query, 120)
        user_id = row["legacy_user_id"] or person_id
        tables = {
            item[0]
            for item in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "semantic_profiles" in tables:
            profile = self.conn.execute(
                "SELECT name,engagement_rate,interaction_count,last_interaction_at"
                " FROM semantic_profiles WHERE user_id=? AND group_id=?",
                (user_id, access.group_id),
            ).fetchone()
            if profile:
                result["profile"] = {
                    "display_name": self._text(profile["name"], 160),
                    "interaction_count": profile["interaction_count"],
                    "last_interaction_at": profile["last_interaction_at"],
                }
        if self.user_manager and (access.actor_id == person_id or access.is_admin):
            try:
                user = self.user_manager.get_user(user_id, access.group_id)
                result["traits"] = [self._text(item, 100) for item in user.traits[:10]]
            except Exception:
                pass
        if self.memory_units is None:
            return result
        try:
            accounts = self.conn.execute(
                "SELECT platform,platform_uid FROM person_accounts WHERE person_id=?", (person_id,)
            ).fetchall()
            identity_keys = [person_id]
            if row["legacy_user_id"]:
                identity_keys.append(row["legacy_user_id"])
            for account in accounts:
                identity_keys.extend(
                    [account["platform_uid"], f"{account['platform']}:{account['platform_uid']}"]
                )
            units = self.memory_units.retrieve_for_person(
                group_id=access.group_id,
                identity_keys=identity_keys,
                query=result["query"],
                include_user_scope=access.actor_id == person_id or access.is_admin,
                limit=3,
            )
            result["items"] = [
                {
                    "unit_id": unit.unit_id,
                    "summary": self._text(unit.summary, 180),
                    "scope": unit.scope,
                }
                for unit in units[:3]
            ]
            result["status"] = "available" if result["items"] else "empty"
        except Exception:
            result["items"] = []
            result["status"] = "unavailable"
        return result

    def search_names(
        self,
        name: str,
        access: PersonAccess,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        scope, params = self._scope_clause(access)
        rows = self.conn.execute(
            f"SELECT person_id FROM person_names WHERE normalized=? AND active=1"
            f" AND ({scope} OR person_id IN (?)) ORDER BY id LIMIT ? OFFSET ?",
            (self._normalize(name), *params, access.actor_id, limit, offset),
        ).fetchall()
        result = []
        for row in rows:
            try:
                person = self.get_person(row[0], access)
            except PersonError:
                continue
            if not any(item["person_id"] == person["person_id"] for item in result):
                result.append(
                    {
                        "person_id": person["person_id"],
                        "display_name": person["display_name"],
                        "kind": person["kind"],
                    }
                )
        return result

    def name_page(
        self, name: str, access: PersonAccess, limit: int = 5, cursor: str = ""
    ) -> dict[str, Any]:
        """Opaque cursors bind pagination to the normalized query and visibility scope."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
            raise PersonError("invalid_argument")
        offset = 0
        signature = self._hash(
            self._normalize(name), access.group_id, access.actor_id, access.is_admin
        )
        if cursor:
            try:
                raw = json.loads(base64.urlsafe_b64decode(cursor.encode() + b"==="))
                if raw["signature"] != signature:
                    raise ValueError
                offset = int(raw["offset"])
                if offset < 0:
                    raise ValueError
            except (ValueError, KeyError, TypeError) as exc:
                raise PersonError("cursor_invalid") from exc
        candidates = self.search_names(name, access, limit=limit + 1, offset=offset)
        page = candidates[:limit]
        next_cursor = ""
        if len(candidates) > limit:
            next_cursor = (
                base64.urlsafe_b64encode(
                    json.dumps(
                        {"signature": signature, "offset": offset + limit}, separators=(",", ":")
                    ).encode()
                )
                .decode()
                .rstrip("=")
            )
        return {
            "status": "ambiguous"
            if len(candidates) > 1 or offset
            else "found"
            if candidates
            else "not_found_or_not_visible",
            "candidates": page,
            "next_cursor": next_cursor,
        }

    def resolve_target(
        self, target: dict[str, Any], access: PersonAccess, source_ref: str = ""
    ) -> str:
        if not isinstance(target, dict):
            raise PersonError("target_invalid")
        ref, value = str(target.get("ref", "")), self._text(target.get("value"), 256)
        if ref == "requester":
            chosen = source_ref or str(target.get("evidence_ref") or "")
            if chosen:
                evidence = self._trusted_evidence(chosen, access)
                if not access.is_admin and evidence.get("actor_id") != access.actor_id:
                    raise PersonError("permission_denied")
            else:
                refs = [
                    ref
                    for ref in access.evidence_refs
                    if self._evidence_visible_for_access(ref, access)
                ]
                if len(refs) != 1:
                    raise PersonError("source_ambiguous")
                evidence = self._trusted_evidence(refs[0], access)
            if not evidence.get("account_bound") or not evidence["actor_id"]:
                raise PersonError("account_unbound")
            return evidence["actor_id"]
        if ref == "persona":
            return self.persona_id
        if ref == "person_id":
            return self.get_person(value, access)["person_id"]
        if ref == "mention_ref":
            evidence = self._trusted_evidence(value, access)
            if not evidence["actor_id"]:
                raise PersonError("account_unbound")
            return evidence["actor_id"]
        if ref == "name":
            candidates = self.search_names(value, access)
            if len(candidates) != 1:
                raise PersonError("subject_ambiguous" if candidates else "not_found_or_not_visible")
            return candidates[0]["person_id"]
        raise PersonError("target_invalid")

    def _evidence_visible_for_access(self, ref: str, access: PersonAccess) -> bool:
        try:
            return (
                self._trusted_evidence(ref, access).get("actor_id") in {access.actor_id}
                or access.is_admin
            )
        except PersonError:
            return False

    def require_write(
        self, person_id: str, access: PersonAccess, evidence_ref: str = ""
    ) -> dict[str, Any]:
        evidence = self._authoritative_evidence(evidence_ref, access)
        row = self._person_row(person_id)
        if (
            not access.is_admin
            and person_id != evidence["actor_id"]
            and not self._is_controller(person_id, evidence["actor_id"])
        ):
            raise PersonError("permission_denied")
        if not access.is_admin and row["kind"] == "persona":
            # 人格自己的自主维护：允许月白在自主回合登记/更新自己的形象资料。
            if not (getattr(access, "self_service", False) and person_id == evidence["actor_id"]):
                raise PersonError("permission_denied")
        evidence["person_id"] = person_id
        return evidence

    def _is_controller(self, person_id: str, actor_id: str) -> bool:
        if not actor_id:
            return False
        if self.conn.execute(
            "SELECT 1 FROM person_controllers WHERE person_id=? AND actor_id=?",
            (person_id, actor_id),
        ).fetchone():
            return True
        row = self.conn.execute(
            "SELECT controller_id FROM people WHERE person_id=?", (person_id,)
        ).fetchone()
        return bool(row and row[0] == actor_id)

    def _insert_name(
        self,
        person_id: str,
        name: str,
        kind: str,
        scope: str,
        source: str,
        source_ref: str = "",
        *,
        replace: bool = False,
    ) -> None:
        name = self._text(name, 160)
        if not name or not scope:
            return
        if replace:
            self.conn.execute(
                "UPDATE person_names SET active=0 WHERE person_id=? AND scope=?"
                " AND name_type='preferred' AND active=1",
                (person_id, scope),
            )
        self.conn.execute(
            "UPDATE person_names SET active=0 WHERE person_id=? AND normalized=? AND scope=?",
            (person_id, self._normalize(name), scope),
        )
        self.conn.execute(
            "INSERT INTO person_names(person_id,name,normalized,name_type,scope,source,"
            "active,source_ref)"
            " VALUES(?,?,?,?,?,?,1,?)",
            (person_id, name, self._normalize(name), kind, scope, source, source_ref),
        )

    def _change_key(
        self, evidence: dict[str, Any], person_id: str, action: str, payload: dict[str, Any]
    ) -> str:
        return self._hash(evidence["evidence_ref"], person_id, action, payload)

    def _existing_change(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM person_changes WHERE idem_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def _record_change(
        self,
        key: str,
        person_id: str,
        action: str,
        payload: dict[str, Any],
        evidence: dict[str, Any],
        status: str,
        expected: int | None,
        result: int | None,
        change_id: str | None = None,
    ) -> str:
        change_id = change_id or str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO person_changes(change_id,idem_key,person_id,action,payload,status,"
            "source_ref,revision,expected_revision,result_revision,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                change_id,
                key,
                person_id,
                action,
                json.dumps(payload, ensure_ascii=False),
                status,
                evidence["evidence_ref"],
                result or 0,
                expected,
                result,
                self._now(),
            ),
        )
        return change_id

    def _bump_revision(self, person_id: str, expected: int | None) -> int:
        if expected is None:
            row = self._person_row(person_id)
            expected = int(row["revision"])
        cur = self.conn.execute(
            "UPDATE people SET revision=revision+1 WHERE person_id=? AND revision=?",
            (person_id, expected),
        )
        if cur.rowcount != 1:
            raise PersonError("revision_conflict")
        return int(expected) + 1

    def create_character(
        self, name: str, access: PersonAccess, evidence_ref: str
    ) -> dict[str, Any]:
        name = self._text(name, 80)
        if not name:
            raise PersonError("invalid_argument", "角色名称不能为空")
        with self._transaction():
            if getattr(access, "self_service", False) and not str(evidence_ref or "").strip():
                evidence = {
                    "evidence_ref": f"self:{uuid.uuid4().hex[:12]}",
                    "actor_id": str(access.actor_id),
                    "group_id": str(access.group_id),
                    "account_bound": True,
                    "attachments": [],
                }
            else:
                evidence = self._authoritative_evidence(evidence_ref, access)
            payload = {"name": name}
            key = self._change_key(evidence, "", "create_character", payload)
            old = self._existing_change(key)
            if old:
                return {
                    **json.loads(old["payload"]),
                    **self.get_person(old["person_id"], access),
                    "status": "already_processed",
                    "change_id": old["change_id"],
                }
            person_id = str(uuid.uuid4())
            actor = evidence["actor_id"]
            self.conn.execute(
                "INSERT INTO people(person_id,display_name,kind,controller_id) VALUES(?,?,?,?)",
                (person_id, name, "character", actor),
            )
            self.conn.execute("INSERT INTO person_controllers VALUES(?,?)", (person_id, actor))
            self._insert_name(
                person_id, name, "preferred", evidence["group_id"], "self_reported", evidence_ref
            )
            change_id = self._record_change(
                key, person_id, "create_character", payload, evidence, "applied", None, 1
            )
            return {
                **self.get_person(person_id, access),
                "status": "applied",
                "change_id": change_id,
            }

    def update_names(
        self,
        person_id: str,
        access: PersonAccess,
        evidence_ref: str,
        *,
        preferred_name: str = "",
        add_aliases: list[str] | None = None,
        retire_names: list[str] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        payload = {
            "preferred_name": self._text(preferred_name, 160),
            "add_aliases": [self._text(n, 160) for n in add_aliases or []],
            "retire_names": [self._text(n, 160) for n in retire_names or []],
        }
        if not any(payload.values()):
            raise PersonError("invalid_argument")
        if any(not n for values in payload.values() if isinstance(values, list) for n in values):
            raise PersonError("invalid_argument")
        with self._transaction():
            evidence = self.require_write(person_id, access, evidence_ref)
            key = self._change_key(evidence, person_id, "set_names", payload)
            old = self._existing_change(key)
            if old:
                return {
                    "status": "already_processed",
                    "change_id": old["change_id"],
                    **self.get_person(person_id, access),
                }
            scope = evidence["group_id"]
            if payload["preferred_name"]:
                self._insert_name(
                    person_id,
                    payload["preferred_name"],
                    "preferred",
                    scope,
                    "self_reported",
                    evidence_ref,
                    replace=True,
                )
            for name in payload["add_aliases"]:
                if self._normalize(name) != self._normalize(payload["preferred_name"]):
                    self._insert_name(
                        person_id, name, "alias", scope, "self_reported", evidence_ref
                    )
            for name in payload["retire_names"]:
                self.conn.execute(
                    "UPDATE person_names SET active=0 WHERE person_id=? AND normalized=?"
                    " AND scope=?",
                    (person_id, self._normalize(name), scope),
                )
            revision = self._bump_revision(person_id, expected_revision)
            change_id = self._record_change(
                key,
                person_id,
                "set_names",
                payload,
                evidence,
                "applied",
                expected_revision,
                revision,
            )
            result = self.get_person(person_id, access)
            self.conn.execute(
                "UPDATE person_ask_state SET declined=0 WHERE actor_id=?", (evidence["actor_id"],)
            )
            return {"status": "applied", "change_id": change_id, **result}

    def propose_claim(
        self,
        person_id: str,
        field: str,
        value: str,
        access: PersonAccess,
        evidence_ref: str,
        *,
        source: str = "self_reported",
        scope: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        field, value = self._text(field, 80), self._text(value, 1000)
        if (
            not field
            or not value
            or field in {"is_admin", "is_developer", "controller_id", "accounts", "person_id"}
        ):
            raise PersonError("invalid_argument")
        if source not in {
            "self_reported",
            "third_party",
            "inferred",
            "platform",
            "configured",
            "legacy_unknown",
        }:
            raise PersonError("source_invalid")
        if scope not in {None, "", access.group_id, "persona"}:
            raise PersonError("scope_invalid")
        with self._transaction():
            evidence = self._authoritative_evidence(evidence_ref, access)
            if not access.is_admin and source in {"platform", "configured", "legacy_unknown"}:
                # Only a trusted importer may attribute a claim to platform/config/legacy.
                source = "self_reported"
            if not access.is_admin and not self._is_controller(person_id, access.actor_id):
                if not self._visible(person_id, access):
                    raise PersonError("not_found_or_not_visible")
                person_id = evidence["actor_id"] if person_id == access.actor_id else person_id
            if scope == "persona" and not access.is_admin:
                raise PersonError("permission_denied", "共享资料需要明确管理配置")
            if not self._visible(person_id, access):
                raise PersonError("not_found_or_not_visible")
            if person_id != evidence["actor_id"] and not self._is_controller(
                person_id, evidence["actor_id"]
            ):
                source = "third_party"
            actual_scope = scope or evidence["group_id"]
            payload = {"field": field, "value": value, "source": source, "scope": actual_scope}
            key = self._change_key(evidence, person_id, "propose_claim", payload)
            old = self._existing_change(key)
            if old:
                return {
                    "status": "already_processed",
                    "change_id": old["change_id"],
                    "claim_id": old["result_revision"]
                    and json.loads(old["payload"]).get("claim_id", ""),
                }
            claim_id = str(uuid.uuid4())
            payload["claim_id"] = claim_id
            status = "pending" if source in {"third_party", "inferred"} else "applied"
            if status == "applied" and source == "self_reported":
                self.conn.execute(
                    "UPDATE person_claims SET status='superseded' WHERE person_id=?"
                    " AND field=? AND scope=? AND status='active' AND source='self_reported'",
                    (person_id, field, actual_scope),
                )
            self.conn.execute(
                "INSERT INTO person_claims(claim_id,person_id,field,value,source,source_ref,"
                "scope,status) VALUES(?,?,?,?,?,?,?,?)",
                (
                    claim_id,
                    person_id,
                    field,
                    value,
                    source,
                    evidence_ref,
                    actual_scope,
                    "pending" if status == "pending" else "active",
                ),
            )
            revision = self._bump_revision(person_id, expected_revision)
            change_id = self._record_change(
                key,
                person_id,
                "propose_claim",
                payload,
                evidence,
                status,
                expected_revision,
                revision,
            )
            return {"status": status, "change_id": change_id, "claim_id": claim_id}

    def change_claim(
        self,
        action: str,
        person_id: str,
        claim_id: str,
        value: str,
        access: PersonAccess,
        evidence_ref: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if action not in {"correct_claim", "retire_claim"} or not claim_id:
            raise PersonError("invalid_argument")
        if action == "correct_claim" and not self._text(value, 1000):
            raise PersonError("invalid_argument")
        with self._transaction():
            evidence = self._authoritative_evidence(evidence_ref, access)
            claim = self.conn.execute(
                "SELECT * FROM person_claims WHERE claim_id=? AND person_id=?",
                (claim_id, person_id),
            ).fetchone()
            if claim is None:
                raise PersonError("claim_not_found")
            can_maintain = person_id == evidence["actor_id"] or self._is_controller(
                person_id, evidence["actor_id"]
            )
            direct = can_maintain and claim["source"] not in {"platform", "configured"}
            status = "applied" if direct else "pending"
            if not can_maintain and not access.is_admin and not self._visible(person_id, access):
                raise PersonError("not_found_or_not_visible")
            payload = {"claim_id": claim_id, "value": self._text(value, 1000), "status": status}
            key = self._change_key(evidence, person_id, action, payload)
            old = self._existing_change(key)
            if old:
                return {
                    "status": "already_processed",
                    "change_id": old["change_id"],
                    "claim_id": claim_id,
                }
            if direct:
                self.conn.execute(
                    "UPDATE person_claims SET status='superseded' WHERE claim_id=?", (claim_id,)
                )
                new_claim = str(uuid.uuid4())
                if action == "correct_claim":
                    self.conn.execute(
                        "INSERT INTO person_claims(claim_id,person_id,field,value,source,"
                        "source_ref,scope,status,replaces) VALUES(?,?,?,?,?,?,?,'active',?)",
                        (
                            new_claim,
                            person_id,
                            claim["field"],
                            payload["value"],
                            "self_reported",
                            evidence_ref,
                            claim["scope"],
                            claim_id,
                        ),
                    )
                payload["new_claim_id"] = new_claim
            else:
                new_claim = claim_id
            revision = self._bump_revision(person_id, expected_revision)
            change_id = self._record_change(
                key, person_id, action, payload, evidence, status, expected_revision, revision
            )
            return {"status": status, "change_id": change_id, "claim_id": new_claim}

    def confirm_change(
        self,
        person_id: str,
        change_id: str,
        access: PersonAccess,
        evidence_ref: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with self._transaction():
            evidence = self.require_write(person_id, access, evidence_ref)
            row = self.conn.execute(
                "SELECT * FROM person_changes WHERE change_id=? AND person_id=?",
                (change_id, person_id),
            ).fetchone()
            if row is None:
                raise PersonError("change_not_found")
            if row["status"] == "applied":
                return {
                    "status": "already_processed",
                    "change_id": change_id,
                    "claim_id": json.loads(row["payload"]).get(
                        "new_claim_id", json.loads(row["payload"]).get("claim_id", "")
                    ),
                }
            if row["status"] != "pending":
                raise PersonError("change_not_confirmable")
            payload = json.loads(row["payload"])
            claim_id = payload.get("claim_id", "")
            claim = self.conn.execute(
                "SELECT * FROM person_claims WHERE claim_id=? AND person_id=?",
                (claim_id, person_id),
            ).fetchone()
            if claim is None:
                raise PersonError("claim_not_found")
            if row["action"] not in {"propose_claim", "correct_claim", "retire_claim"}:
                raise PersonError("change_not_confirmable")
            if row["action"] == "propose_claim":
                if claim["source"] == "third_party" and person_id == evidence["actor_id"]:
                    self.conn.execute(
                        "UPDATE person_claims SET status='superseded' WHERE person_id=?"
                        " AND field=? AND scope=? AND status='active' AND source='self_reported'",
                        (person_id, claim["field"], claim["scope"]),
                    )
                self.conn.execute(
                    "UPDATE person_claims SET status='active' WHERE claim_id=?"
                    " AND status='pending'",
                    (claim_id,),
                )
            else:
                if row["action"] == "correct_claim":
                    new_id = str(uuid.uuid4())
                    self.conn.execute(
                        "INSERT INTO person_claims(claim_id,person_id,field,value,source,"
                        "source_ref,scope,status,replaces) VALUES(?,?,?,?,?,?,?,'active',?)",
                        (
                            new_id,
                            person_id,
                            claim["field"],
                            payload["value"],
                            "self_reported",
                            evidence_ref,
                            claim["scope"],
                            claim_id,
                        ),
                    )
                    payload["new_claim_id"] = new_id
                self.conn.execute(
                    "UPDATE person_claims SET status='superseded' WHERE claim_id=?", (claim_id,)
                )
            revision = self._bump_revision(person_id, expected_revision)
            self.conn.execute(
                "UPDATE person_changes SET status='applied',payload=?,result_revision=?"
                " WHERE change_id=? AND status='pending'",
                (json.dumps(payload, ensure_ascii=False), revision, change_id),
            )
            return {
                "status": "applied",
                "change_id": change_id,
                "claim_id": payload.get("new_claim_id", claim_id),
            }

    def list_changes(self, person_id: str, access: PersonAccess) -> list[dict[str, Any]]:
        self.get_person(person_id, access)
        if (
            not access.is_admin
            and access.actor_id != person_id
            and not self._is_controller(person_id, access.actor_id)
        ):
            raise PersonError("permission_denied")
        rows = self.conn.execute(
            "SELECT change_id,action,status,expected_revision,result_revision,payload,created_at"
            " FROM person_changes WHERE person_id=? AND migration_id=''"
            " ORDER BY created_at DESC LIMIT 30",
            (person_id,),
        ).fetchall()
        return [
            {
                **dict(row),
                "payload": {
                    key: value
                    for key, value in json.loads(row["payload"]).items()
                    if key != "source"
                },
            }
            for row in rows
        ]

    def should_ask_name(self, actor_id: str, *, declined: bool = False) -> bool:
        with self._lock:
            if declined:
                self.conn.execute(
                    "INSERT INTO person_ask_state(actor_id,last_asked,declined) VALUES(?,?,1)"
                    " ON CONFLICT(actor_id) DO UPDATE SET declined=1,"
                    "last_asked=excluded.last_asked",
                    (actor_id, self._now()),
                )
                self.conn.commit()
                return False
            if self.conn.execute(
                "SELECT 1 FROM person_names WHERE person_id=? AND name_type='preferred'"
                " AND active=1 LIMIT 1",
                (actor_id,),
            ).fetchone():
                return False
            row = self.conn.execute(
                "SELECT last_asked,declined FROM person_ask_state WHERE actor_id=?", (actor_id,)
            ).fetchone()
            if row and row[1]:
                return False
            if row and row[0]:
                try:
                    if datetime.now(timezone.utc) - datetime.fromisoformat(row[0]) < timedelta(
                        days=7
                    ):
                        return False
                except ValueError:
                    return False
            return True

    def mark_name_asked(self, actor_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO person_ask_state(actor_id,last_asked) VALUES(?,?)"
                " ON CONFLICT(actor_id) DO UPDATE SET last_asked=excluded.last_asked",
                (actor_id, self._now()),
            )
            self.conn.commit()

    def _migration_users(self) -> list[dict[str, Any]]:
        tables = {
            row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "users" not in tables:
            return []
        rows = self.conn.execute(
            "SELECT user_id,name,identities FROM users ORDER BY user_id"
        ).fetchall()
        result = []
        for row in rows:
            try:
                identities = json.loads(row["identities"] or "{}")
            except (ValueError, TypeError):
                identities = {}
            result.append(
                {"user_id": row["user_id"], "name": row["name"], "identities": identities}
            )
        return result

    def _migration_conflicts(self, users: list[dict[str, Any]]) -> list[dict[str, Any]]:
        conflicts = []
        for user in users:
            identities = user["identities"]
            if not isinstance(identities, dict):
                continue
            for platform, uid in identities.items():
                if not uid:
                    continue
                person_id = self.lookup_account(platform, uid)
                if person_id and self.person_id_for_user(user["user_id"]) not in {None, person_id}:
                    conflicts.append(
                        {
                            "user_id": user["user_id"],
                            "platform": platform,
                            "reason": "legacy_account_conflict",
                        }
                    )
        return conflicts

    def migration_dry_run(self) -> dict[str, Any]:
        users = self._migration_users()
        mapped = [u["user_id"] for u in users if self.person_id_for_user(u["user_id"])]
        return {
            "users": len(users),
            "already_mapped": len(mapped),
            "pending": len(users) - len(mapped),
            "conflicts": self._migration_conflicts(users),
            "backup_required": True,
            "rollback": "仅回滚指定 migration_id 创建且之后未活动的记录；保留原 user_id 和账号映射",
        }

    def migration_apply(self) -> dict[str, Any]:
        users = self._migration_users()
        report = self.migration_dry_run()
        pending = [u for u in users if not self.person_id_for_user(u["user_id"])]
        if not pending:
            report.update(
                {
                    "success": True,
                    "status": "already_processed",
                    "migration_id": "",
                    "backup_path": "",
                }
            )
            return report
        database = self.conn.execute("PRAGMA database_list").fetchone()
        if database[2] == ":memory:":
            raise PersonError("backup_unavailable", "迁移前需要文件数据库备份")
        backup_dir = (self.work_path or Path(database[2]).parent) / "migration_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        migration_id = str(uuid.uuid4())
        backup_path = backup_dir / f"people-{migration_id}.db"
        backup_conn = sqlite3.connect(backup_path)
        try:
            self.conn.commit()
            self.conn.backup(backup_conn)
        finally:
            backup_conn.close()
        created: list[str] = []
        try:
            with self._transaction():
                for user in users:
                    if self.person_id_for_user(user["user_id"]):
                        continue
                    person_id = str(uuid.uuid4())
                    self.conn.execute(
                        "INSERT INTO people(person_id,display_name,kind,legacy_user_id)"
                        " VALUES(?,?,'human',?)",
                        (person_id, self._text(user["name"], 160), user["user_id"]),
                    )
                    for label in {user["name"], user["user_id"]}:
                        self._insert_name(person_id, label, "legacy", "persona", "legacy_unknown")
                    if isinstance(user["identities"], dict):
                        for platform, uid in user["identities"].items():
                            if not platform or not uid:
                                continue
                            existing = self.lookup_account(platform, uid)
                            if existing and existing != person_id:
                                report["conflicts"].append(
                                    {
                                        "user_id": user["user_id"],
                                        "platform": platform,
                                        "reason": "legacy_account_conflict",
                                    }
                                )
                                continue
                            self.conn.execute(
                                "INSERT OR IGNORE INTO person_accounts VALUES(?,?,?)",
                                (str(platform).casefold(), str(uid), person_id),
                            )
                    created.append(person_id)
                    self.conn.execute(
                        "INSERT INTO person_changes(change_id,idem_key,person_id,action,payload,"
                        "status,source_ref,revision,created_at,migration_id)"
                        " VALUES(?,?,?,'migrate',?, 'applied','migration',1,?,?)",
                        (
                            str(uuid.uuid4()),
                            f"migration:{migration_id}:{person_id}",
                            person_id,
                            json.dumps({"legacy_user_id": user["user_id"]}),
                            self._now(),
                            migration_id,
                        ),
                    )
                self.conn.execute(
                    "INSERT INTO person_migrations VALUES(?,?,?,?,0)",
                    (
                        migration_id,
                        json.dumps({**report, "created_person_ids": created}),
                        str(backup_path),
                        self._now(),
                    ),
                )
        except Exception:
            backup_path.unlink(missing_ok=True)
            raise
        report.update(
            {
                "success": True,
                "status": "applied",
                "migration_id": migration_id,
                "created": len(created),
                "backup_path": str(backup_path),
            }
        )
        return report

    def migration_rollback(self, migration: Any) -> dict[str, Any]:
        """Rollback only this migration's untouched new people; never delete later activity."""
        if isinstance(migration, dict):
            migration_id = str(migration.get("migration_id") or "")
        elif isinstance(migration, list):
            # Compatibility for the original user_ids API: only unused mappings are removed.
            with self._transaction():
                removed = 0
                for user_id in migration:
                    row = self.conn.execute(
                        "SELECT person_id FROM people WHERE legacy_user_id=? AND kind='human'",
                        (str(user_id),),
                    ).fetchone()
                    person_id = row[0] if row else None
                    if (
                        person_id
                        and not self.conn.execute(
                            "SELECT 1 FROM person_accounts WHERE person_id=?", (person_id,)
                        ).fetchone()
                    ):
                        removed += self.conn.execute(
                            "DELETE FROM people WHERE person_id=? AND revision=1", (person_id,)
                        ).rowcount
                return {"success": True, "removed": removed}
        else:
            migration_id = str(migration or "")
        row = self.conn.execute(
            "SELECT * FROM person_migrations WHERE migration_id=?", (migration_id,)
        ).fetchone()
        if row is None:
            raise PersonError("migration_not_found")
        if row["rolled_back"]:
            return {
                "success": True,
                "status": "already_processed",
                "migration_id": migration_id,
                "removed": 0,
            }
        report = json.loads(row["report"])
        kept = []
        for person_id in report.get("created_person_ids", []):
            if self.conn.execute(
                "SELECT 1 FROM person_accounts WHERE person_id=?", (person_id,)
            ).fetchone():
                kept.append(person_id)
            if self.conn.execute(
                "SELECT 1 FROM people WHERE person_id=? AND revision!=1", (person_id,)
            ).fetchone():
                kept.append(person_id)
            if self.conn.execute(
                "SELECT 1 FROM person_names WHERE person_id=? AND source!='legacy_unknown'",
                (person_id,),
            ).fetchone():
                kept.append(person_id)
        kept = list(set(kept))
        if kept:
            raise PersonError(
                "migration_has_new_activity", json.dumps({"retained_person_ids": kept})
            )
        with self._transaction():
            removed = 0
            for person_id in report.get("created_person_ids", []):
                self.conn.execute(
                    "DELETE FROM person_names WHERE person_id=? AND source='legacy_unknown'",
                    (person_id,),
                )
                self.conn.execute(
                    "DELETE FROM person_changes WHERE migration_id=? AND person_id=?",
                    (migration_id, person_id),
                )
                removed += self.conn.execute(
                    "DELETE FROM people WHERE person_id=? AND revision=1", (person_id,)
                ).rowcount
            self.conn.execute(
                "UPDATE person_migrations SET rolled_back=1 WHERE migration_id=?", (migration_id,)
            )
        return {
            "success": True,
            "status": "rolled_back",
            "migration_id": migration_id,
            "removed": removed,
            "backup_path": row["backup_path"],
            "note": "仅删除本次未活动新增映射，原用户与外部备份保留",
        }
