"""SQLite 统一存储层。

提供统一的用户数据持久化。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryStorage:
    """SQLite 存储层。

    统一管理用户数据、别名索引、语义画像。
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._ensure_connection()
        self._create_tables()

    def _ensure_connection(self) -> None:
        """确保数据库连接。"""
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")

    def _create_tables(self) -> None:
        """创建表结构。"""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                persona TEXT DEFAULT '',
                identities TEXT DEFAULT '{}',
                aliases TEXT DEFAULT '[]',
                traits TEXT DEFAULT '[]',
                group_memberships TEXT DEFAULT '{}',
                metadata TEXT DEFAULT '{}',
                identity_anchors TEXT DEFAULT '[]',
                relationships TEXT DEFAULT '[]',
                short_bio TEXT DEFAULT '',
                affinity_score REAL DEFAULT 0.0,
                pending_messages TEXT DEFAULT '[]',
                pending_message_count INTEGER DEFAULT 0,
                distilled_points TEXT DEFAULT '[]',
                last_distill_at TEXT DEFAULT '',
                bio_token_estimate INTEGER DEFAULT 0,
                bio_token_budget INTEGER DEFAULT 500,
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS user_identities (
                platform TEXT NOT NULL,
                platform_uid TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                created_at TEXT DEFAULT '',
                PRIMARY KEY (platform, platform_uid)
            );

            CREATE INDEX IF NOT EXISTS idx_user_identities_user
                ON user_identities(user_id);

            CREATE TABLE IF NOT EXISTS group_members (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                joined_at TEXT DEFAULT '',
                PRIMARY KEY (group_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_group_members_group
                ON group_members(group_id);

            CREATE INDEX IF NOT EXISTS idx_group_members_user
                ON group_members(user_id);

            CREATE TABLE IF NOT EXISTS aliases (
                alias TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                user_name TEXT NOT NULL DEFAULT '',
                weight REAL DEFAULT 1.0,
                groups TEXT DEFAULT '[]',
                mentioned_count INTEGER DEFAULT 1,
                confidence REAL DEFAULT 0.5,
                first_seen_at TEXT DEFAULT '',
                last_seen_at TEXT DEFAULT '',
                source TEXT DEFAULT 'napcat',
                created_at TEXT DEFAULT '',
                PRIMARY KEY (alias, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_aliases_user
                ON aliases(user_id);

            CREATE INDEX IF NOT EXISTS idx_aliases_alias
                ON aliases(alias);

            CREATE TABLE IF NOT EXISTS semantic_profiles (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                profile_data TEXT DEFAULT '{}',
                updated_at TEXT DEFAULT '',
                PRIMARY KEY (group_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_semantic_profiles_user
                ON semantic_profiles(user_id);

            CREATE TABLE IF NOT EXISTS group_semantic_profiles (
                group_id TEXT PRIMARY KEY,
                profile_data TEXT DEFAULT '{}',
                updated_at TEXT DEFAULT ''
            );
        """)

    def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── 用户 CRUD ─────────────────────────────────────────

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        """获取用户。"""
        row = self._conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    def save_user(self, user: dict[str, Any]) -> None:
        """保存用户（插入或更新）。"""
        now = _now_iso()
        self._conn.execute(
            """
            INSERT INTO users (
                user_id, name, persona, identities, aliases, traits,
                group_memberships, metadata, identity_anchors, relationships,
                short_bio, affinity_score, pending_messages, pending_message_count,
                distilled_points, last_distill_at, bio_token_estimate, bio_token_budget,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name,
                persona=excluded.persona,
                identities=excluded.identities,
                aliases=excluded.aliases,
                traits=excluded.traits,
                group_memberships=excluded.group_memberships,
                metadata=excluded.metadata,
                identity_anchors=excluded.identity_anchors,
                relationships=excluded.relationships,
                short_bio=excluded.short_bio,
                affinity_score=excluded.affinity_score,
                pending_messages=excluded.pending_messages,
                pending_message_count=excluded.pending_message_count,
                distilled_points=excluded.distilled_points,
                last_distill_at=excluded.last_distill_at,
                bio_token_estimate=excluded.bio_token_estimate,
                bio_token_budget=excluded.bio_token_budget,
                updated_at=excluded.updated_at
            """,
            (
                user.get("user_id", ""),
                user.get("name", ""),
                user.get("persona", ""),
                json.dumps(user.get("identities", {}), ensure_ascii=False),
                json.dumps(user.get("aliases", []), ensure_ascii=False),
                json.dumps(user.get("traits", []), ensure_ascii=False),
                json.dumps(user.get("group_memberships", {}), ensure_ascii=False),
                json.dumps(user.get("metadata", {}), ensure_ascii=False),
                json.dumps(user.get("identity_anchors", []), ensure_ascii=False),
                json.dumps(user.get("relationships", []), ensure_ascii=False),
                user.get("short_bio", ""),
                float(user.get("affinity_score", 0.0)),
                json.dumps(user.get("pending_messages", []), ensure_ascii=False),
                int(user.get("pending_message_count", 0)),
                json.dumps(user.get("distilled_points", []), ensure_ascii=False),
                user.get("last_distill_at", ""),
                int(user.get("bio_token_estimate", 0)),
                int(user.get("bio_token_budget", 500)),
                user.get("created_at", now),
                now,
            ),
        )
        self._conn.commit()

    def delete_user(self, user_id: str) -> None:
        """删除用户。"""
        self._conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        self._conn.commit()

    def list_users(self) -> list[dict[str, Any]]:
        """列出所有用户。"""
        rows = self._conn.execute("SELECT * FROM users").fetchall()
        return [self._row_to_user(row) for row in rows]

    def _row_to_user(self, row: sqlite3.Row) -> dict[str, Any]:
        """将数据库行转换为用户字典。"""
        return {
            "user_id": row["user_id"],
            "name": row["name"],
            "persona": row["persona"],
            "identities": json.loads(row["identities"]),
            "aliases": json.loads(row["aliases"]),
            "traits": json.loads(row["traits"]),
            "group_memberships": json.loads(row["group_memberships"]),
            "metadata": json.loads(row["metadata"]),
            "identity_anchors": json.loads(row["identity_anchors"]),
            "relationships": json.loads(row["relationships"]),
            "short_bio": row["short_bio"],
            "affinity_score": row["affinity_score"],
            "pending_messages": json.loads(row["pending_messages"]),
            "pending_message_count": row["pending_message_count"],
            "distilled_points": json.loads(row["distilled_points"]),
            "last_distill_at": row["last_distill_at"],
            "bio_token_estimate": row["bio_token_estimate"],
            "bio_token_budget": row["bio_token_budget"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ── 平台身份 CRUD ─────────────────────────────────────

    def get_user_by_identity(self, platform: str, platform_uid: str) -> str | None:
        """通过平台身份查找用户 ID。"""
        row = self._conn.execute(
            "SELECT user_id FROM user_identities WHERE platform = ? AND platform_uid = ?",
            (platform, platform_uid),
        ).fetchone()
        return row["user_id"] if row else None

    def save_identity(self, platform: str, platform_uid: str, user_id: str) -> None:
        """保存平台身份。"""
        self._conn.execute(
            """
            INSERT INTO user_identities (platform, platform_uid, user_id, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(platform, platform_uid) DO UPDATE SET user_id=excluded.user_id
            """,
            (platform, platform_uid, user_id, _now_iso()),
        )
        self._conn.commit()

    def get_identities_for_user(self, user_id: str) -> list[dict[str, str]]:
        """获取用户的所有平台身份。"""
        rows = self._conn.execute(
            "SELECT platform, platform_uid FROM user_identities WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return [{"platform": r["platform"], "platform_uid": r["platform_uid"]} for r in rows]

    # ── 群组成员 CRUD ─────────────────────────────────────

    def add_group_member(self, group_id: str, user_id: str) -> None:
        """添加群组成员。"""
        self._conn.execute(
            """
            INSERT INTO group_members (group_id, user_id, joined_at)
            VALUES (?, ?, ?)
            ON CONFLICT(group_id, user_id) DO NOTHING
            """,
            (group_id, user_id, _now_iso()),
        )
        self._conn.commit()

    def get_group_members(self, group_id: str) -> list[str]:
        """获取群组的所有成员 ID。"""
        rows = self._conn.execute(
            "SELECT user_id FROM group_members WHERE group_id = ?",
            (group_id,),
        ).fetchall()
        return [r["user_id"] for r in rows]

    def get_user_groups(self, user_id: str) -> list[str]:
        """获取用户所属的所有群组 ID。"""
        rows = self._conn.execute(
            "SELECT group_id FROM group_members WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return [r["group_id"] for r in rows]

    def remove_group_member(self, group_id: str, user_id: str) -> None:
        """移除群组成员。"""
        self._conn.execute(
            "DELETE FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        self._conn.commit()

    # ── 别名 CRUD ─────────────────────────────────────────

    def get_alias_entries(self, alias: str) -> list[dict[str, Any]]:
        """获取别名的所有条目。"""
        rows = self._conn.execute(
            "SELECT * FROM aliases WHERE alias = ?", (alias,)
        ).fetchall()
        return [self._row_to_alias(row) for row in rows]

    def save_alias_entry(self, entry: dict[str, Any]) -> None:
        """保存别名条目。"""
        self._conn.execute(
            """
            INSERT INTO aliases (
                alias, user_id, user_name, weight, groups, mentioned_count,
                confidence, first_seen_at, last_seen_at, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(alias, user_id) DO UPDATE SET
                user_name=excluded.user_name,
                weight=excluded.weight,
                groups=excluded.groups,
                mentioned_count=excluded.mentioned_count,
                confidence=excluded.confidence,
                last_seen_at=excluded.last_seen_at,
                source=excluded.source
            """,
            (
                entry.get("alias", ""),
                entry.get("user_id", ""),
                entry.get("user_name", ""),
                float(entry.get("weight", 1.0)),
                json.dumps(entry.get("groups", []), ensure_ascii=False),
                int(entry.get("mentioned_count", 1)),
                float(entry.get("confidence", 0.5)),
                entry.get("first_seen_at", ""),
                entry.get("last_seen_at", ""),
                entry.get("source", "napcat"),
                entry.get("created_at", _now_iso()),
            ),
        )
        self._conn.commit()

    def delete_alias_entry(self, alias: str, user_id: str) -> None:
        """删除别名条目。"""
        self._conn.execute(
            "DELETE FROM aliases WHERE alias = ? AND user_id = ?",
            (alias, user_id),
        )
        self._conn.commit()

    def get_aliases_for_group(self, group_id: str) -> dict[str, str]:
        """获取群组相关的别名速查表。"""
        rows = self._conn.execute(
            """
            SELECT DISTINCT alias, user_name FROM aliases
            WHERE groups LIKE ?
            """,
            (f'%"{group_id}"%',),
        ).fetchall()
        return {r["alias"]: r["user_name"] for r in rows}

    def get_all_aliases(self) -> dict[str, list[dict[str, Any]]]:
        """获取所有别名索引。"""
        rows = self._conn.execute("SELECT * FROM aliases").fetchall()
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            alias = row["alias"]
            if alias not in result:
                result[alias] = []
            result[alias].append(self._row_to_alias(row))
        return result

    def _row_to_alias(self, row: sqlite3.Row) -> dict[str, Any]:
        """将数据库行转换为别名字典。"""
        return {
            "alias": row["alias"],
            "user_id": row["user_id"],
            "user_name": row["user_name"],
            "weight": row["weight"],
            "groups": json.loads(row["groups"]),
            "mentioned_count": row["mentioned_count"],
            "confidence": row["confidence"],
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "source": row["source"],
        }

    # ── 语义画像 CRUD ─────────────────────────────────────

    def get_semantic_profile(self, group_id: str, user_id: str) -> dict[str, Any] | None:
        """获取语义画像。"""
        row = self._conn.execute(
            "SELECT profile_data FROM semantic_profiles WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["profile_data"])

    def save_semantic_profile(self, group_id: str, user_id: str, profile: dict[str, Any]) -> None:
        """保存语义画像。"""
        self._conn.execute(
            """
            INSERT INTO semantic_profiles (group_id, user_id, profile_data, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                profile_data=excluded.profile_data,
                updated_at=excluded.updated_at
            """,
            (group_id, user_id, json.dumps(profile, ensure_ascii=False), _now_iso()),
        )
        self._conn.commit()

    def list_semantic_profiles(self, group_id: str) -> list[dict[str, Any]]:
        """列出群组的所有语义画像。"""
        rows = self._conn.execute(
            "SELECT user_id, profile_data FROM semantic_profiles WHERE group_id = ?",
            (group_id,),
        ).fetchall()
        return [
            {"user_id": r["user_id"], **json.loads(r["profile_data"])}
            for r in rows
        ]

    def get_group_semantic_profile(self, group_id: str) -> dict[str, Any] | None:
        """获取群组语义画像。"""
        row = self._conn.execute(
            "SELECT profile_data FROM group_semantic_profiles WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["profile_data"])

    def save_group_semantic_profile(self, group_id: str, profile: dict[str, Any]) -> None:
        """保存群组语义画像。"""
        self._conn.execute(
            """
            INSERT INTO group_semantic_profiles (group_id, profile_data, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                profile_data=excluded.profile_data,
                updated_at=excluded.updated_at
            """,
            (group_id, json.dumps(profile, ensure_ascii=False), _now_iso()),
        )
        self._conn.commit()


__all__ = ["MemoryStorage"]
