from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from sirius_pulse.memory.profile.models import UserPersonaProfile, now_iso


class UserPersonaProfileStore:
    """SQLite-backed store for model-maintained user persona profiles."""

    def __init__(
        self, db_path: Path | str | None = None, *, conn: sqlite3.Connection | None = None
    ) -> None:
        self._owns_conn = conn is None
        if conn is not None:
            self.conn = conn
        else:
            resolved = Path(db_path or ":memory:")
            if str(resolved) != ":memory:":
                resolved.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(resolved)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        if self._owns_conn:
            self.conn.close()

    def _init_schema(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_persona_profiles (
                persona_name TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                profile_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                version INTEGER DEFAULT 1,
                PRIMARY KEY (persona_name, group_id, user_id)
            )
            """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profile_events (
                event_id TEXT PRIMARY KEY,
                persona_name TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                patch_json TEXT NOT NULL,
                evidence_message_ids TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_persona_profiles_user
                ON user_persona_profiles(persona_name, user_id)
            """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_profile_events_user
                ON user_profile_events(persona_name, group_id, user_id, created_at)
            """)
        self.conn.commit()

    def get_profile(
        self, persona_name: str, group_id: str, user_id: str
    ) -> UserPersonaProfile | None:
        row = self.conn.execute(
            """
            SELECT profile_json FROM user_persona_profiles
            WHERE persona_name = ? AND group_id = ? AND user_id = ?
            """,
            (persona_name, group_id, user_id),
        ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["profile_json"] or "{}")
        except json.JSONDecodeError:
            return None
        return UserPersonaProfile.from_dict(data)

    def save_profile(self, persona_name: str, profile: UserPersonaProfile) -> None:
        profile.last_updated_at = now_iso()
        self.conn.execute(
            """
            INSERT INTO user_persona_profiles (
                persona_name, group_id, user_id, display_name, profile_json, updated_at, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(persona_name, group_id, user_id) DO UPDATE SET
                display_name = excluded.display_name,
                profile_json = excluded.profile_json,
                updated_at = excluded.updated_at,
                version = excluded.version
            """,
            (
                persona_name,
                profile.group_id,
                profile.user_id,
                profile.display_name,
                json.dumps(profile.to_dict(), ensure_ascii=False),
                profile.last_updated_at,
                profile.version,
            ),
        )
        self.conn.commit()

    def list_group_profiles(self, persona_name: str, group_id: str) -> list[UserPersonaProfile]:
        rows = self.conn.execute(
            """
            SELECT profile_json FROM user_persona_profiles
            WHERE persona_name = ? AND group_id = ?
            ORDER BY updated_at DESC
            """,
            (persona_name, group_id),
        ).fetchall()
        profiles: list[UserPersonaProfile] = []
        for row in rows:
            try:
                profiles.append(
                    UserPersonaProfile.from_dict(json.loads(row["profile_json"] or "{}"))
                )
            except json.JSONDecodeError:
                continue
        return profiles

    def append_event(
        self,
        *,
        event_id: str,
        persona_name: str,
        group_id: str,
        user_id: str,
        event_type: str,
        patch: dict[str, Any],
        evidence_message_ids: list[str],
        created_by: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO user_profile_events (
                event_id, persona_name, group_id, user_id, event_type,
                patch_json, evidence_message_ids, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                persona_name,
                group_id,
                user_id,
                event_type,
                json.dumps(patch, ensure_ascii=False),
                json.dumps(evidence_message_ids, ensure_ascii=False),
                created_by,
                now_iso(),
            ),
        )
        self.conn.commit()

    def list_events(
        self, persona_name: str, group_id: str, user_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM user_profile_events
            WHERE persona_name = ? AND group_id = ? AND user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (persona_name, group_id, user_id, max(1, min(100, limit))),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                patch = json.loads(row["patch_json"] or "{}")
                evidence = json.loads(row["evidence_message_ids"] or "[]")
            except json.JSONDecodeError:
                patch = {}
                evidence = []
            events.append(
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "patch": patch,
                    "evidence_message_ids": evidence,
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                }
            )
        return events
