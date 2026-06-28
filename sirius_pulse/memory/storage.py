"""SQLite 统一存储层。

提供统一的用户数据持久化。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from sirius_pulse.utils.sqlite_base import BaseSqliteStore

logger = logging.getLogger(__name__)

__all__ = ["MemoryStorage"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryStorage(BaseSqliteStore):
    """SQLite 存储层。

    统一管理用户数据、别名索引、语义画像。
    继承自 BaseSqliteStore，复用连接管理和基础操作。
    """

    def _create_tables(self) -> None:
        """创建表结构。"""
        self.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                persona TEXT DEFAULT '',
                identities TEXT DEFAULT '{}',
                traits TEXT DEFAULT '[]',
                group_memberships TEXT DEFAULT '{}',
                metadata TEXT DEFAULT '{}',
                identity_anchors TEXT DEFAULT '[]',
                relationships TEXT DEFAULT '[]',
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


            CREATE TABLE IF NOT EXISTS semantic_profiles (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                name TEXT DEFAULT '',
                engagement_rate REAL DEFAULT 0.0,
                interaction_count INTEGER DEFAULT 0,
                first_interaction_at TEXT DEFAULT '',
                last_interaction_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT '',
                PRIMARY KEY (group_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS response_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                sent_at TEXT DEFAULT '',
                target_user_id TEXT DEFAULT '',
                topic_hint TEXT DEFAULT '',
                response_length INTEGER DEFAULT 0,
                was_engaged INTEGER DEFAULT 0,
                engagement_latency_s REAL DEFAULT 0.0,
                FOREIGN KEY (group_id, user_id) REFERENCES semantic_profiles(group_id, user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_response_records_user
                ON response_records(group_id, user_id);

            CREATE INDEX IF NOT EXISTS idx_semantic_profiles_user
                ON semantic_profiles(user_id);

            CREATE TABLE IF NOT EXISTS group_semantic_profiles (
                group_id TEXT PRIMARY KEY,
                group_name TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS group_pending_ai_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                sent_at TEXT DEFAULT '',
                target_user_id TEXT DEFAULT '',
                topic_hint TEXT DEFAULT '',
                response_length INTEGER DEFAULT 0,
                was_engaged INTEGER DEFAULT 0,
                engagement_latency_s REAL DEFAULT 0.0,
                FOREIGN KEY (group_id) REFERENCES group_semantic_profiles(group_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_group_pending_ai_responses_group
                ON group_pending_ai_responses(group_id);

            CREATE TABLE IF NOT EXISTS diary_meta (
                group_id TEXT PRIMARY KEY,
                last_tail_sources TEXT DEFAULT '[]',
                updated_at TEXT DEFAULT ''
            );
        """
        )

    # ── 用户 CRUD ─────────────────────────────────────────

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        """获取用户。"""
        row = self.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    def save_user(self, user: dict[str, Any]) -> None:
        """保存用户（插入或更新）。"""
        now = _now_iso()
        self.execute(
            """
            INSERT INTO users (
                user_id, name, persona, identities, traits,
                group_memberships, metadata, identity_anchors, relationships,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name,
                persona=excluded.persona,
                identities=excluded.identities,
                traits=excluded.traits,
                group_memberships=excluded.group_memberships,
                metadata=excluded.metadata,
                identity_anchors=excluded.identity_anchors,
                relationships=excluded.relationships,
                updated_at=excluded.updated_at
            """,
            (
                user.get("user_id", ""),
                user.get("name", ""),
                user.get("persona", ""),
                json.dumps(user.get("identities", {}), ensure_ascii=False),
                json.dumps(user.get("traits", []), ensure_ascii=False),
                json.dumps(user.get("group_memberships", {}), ensure_ascii=False),
                json.dumps(user.get("metadata", {}), ensure_ascii=False),
                json.dumps(user.get("identity_anchors", []), ensure_ascii=False),
                json.dumps(user.get("relationships", []), ensure_ascii=False),
                user.get("created_at", now),
                now,
            ),
        )
        self.commit()

    def delete_user(self, user_id: str) -> None:
        """删除用户。"""
        self.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        self.commit()

    def list_users(self) -> list[dict[str, Any]]:
        """列出所有用户。"""
        rows = self.execute("SELECT * FROM users").fetchall()
        return [self._row_to_user(row) for row in rows]

    def _row_to_user(self, row: sqlite3.Row) -> dict[str, Any]:
        """将数据库行转换为用户字典。"""
        return {
            "user_id": row["user_id"],
            "name": row["name"],
            "persona": row["persona"],
            "identities": json.loads(row["identities"]),
            "traits": json.loads(row["traits"]),
            "group_memberships": json.loads(row["group_memberships"]),
            "metadata": json.loads(row["metadata"]),
            "identity_anchors": json.loads(row["identity_anchors"]),
            "relationships": json.loads(row["relationships"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ── 平台身份 CRUD ─────────────────────────────────────

    def get_user_by_identity(self, platform: str, platform_uid: str) -> str | None:
        """通过平台身份查找用户 ID。"""
        row = self.execute(
            "SELECT user_id FROM user_identities WHERE platform = ? AND platform_uid = ?",
            (platform, platform_uid),
        ).fetchone()
        return row["user_id"] if row else None

    def save_identity(self, platform: str, platform_uid: str, user_id: str) -> None:
        """保存平台身份。"""
        self.execute(
            """
            INSERT INTO user_identities (platform, platform_uid, user_id, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(platform, platform_uid) DO UPDATE SET user_id=excluded.user_id
            """,
            (platform, platform_uid, user_id, _now_iso()),
        )
        self.commit()

    def get_identities_for_user(self, user_id: str) -> list[dict[str, str]]:
        """获取用户的所有平台身份。"""
        rows = self.execute(
            "SELECT platform, platform_uid FROM user_identities WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return [{"platform": r["platform"], "platform_uid": r["platform_uid"]} for r in rows]

    # ── 群组成员 CRUD ─────────────────────────────────────

    def add_group_member(self, group_id: str, user_id: str) -> None:
        """添加群组成员。"""
        self.execute(
            """
            INSERT INTO group_members (group_id, user_id, joined_at)
            VALUES (?, ?, ?)
            ON CONFLICT(group_id, user_id) DO NOTHING
            """,
            (group_id, user_id, _now_iso()),
        )
        self.commit()

    def get_group_members(self, group_id: str) -> list[str]:
        """获取群组的所有成员 ID。"""
        rows = self.execute(
            "SELECT user_id FROM group_members WHERE group_id = ?",
            (group_id,),
        ).fetchall()
        return [r["user_id"] for r in rows]

    def get_user_groups(self, user_id: str) -> list[str]:
        """获取用户所属的所有群组 ID。"""
        rows = self.execute(
            "SELECT group_id FROM group_members WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return [r["group_id"] for r in rows]

    def remove_group_member(self, group_id: str, user_id: str) -> None:
        """移除群组成员。"""
        self.execute(
            "DELETE FROM group_members WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        self.commit()


    # ── 语义画像 CRUD ─────────────────────────────────────

    def get_semantic_profile(self, group_id: str, user_id: str) -> dict[str, Any] | None:
        """获取语义画像。"""
        row = self.execute(
            "SELECT * FROM semantic_profiles WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_semantic_profile(row)

    def save_semantic_profile(self, group_id: str, user_id: str, profile: dict[str, Any]) -> None:
        """保存语义画像。"""
        self.execute(
            """
            INSERT INTO semantic_profiles (
                group_id, user_id, name, engagement_rate, interaction_count,
                first_interaction_at, last_interaction_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                name=excluded.name,
                engagement_rate=excluded.engagement_rate,
                interaction_count=excluded.interaction_count,
                first_interaction_at=excluded.first_interaction_at,
                last_interaction_at=excluded.last_interaction_at,
                updated_at=excluded.updated_at
            """,
            (
                group_id,
                user_id,
                profile.get("name", ""),
                float(profile.get("engagement_rate", 0.0)),
                int(profile.get("interaction_count", 0)),
                profile.get("first_interaction_at", ""),
                profile.get("last_interaction_at", ""),
                _now_iso(),
            ),
        )
        self.commit()

    def list_semantic_profiles(self, group_id: str) -> list[dict[str, Any]]:
        """列出群组的所有语义画像。"""
        rows = self.execute(
            "SELECT * FROM semantic_profiles WHERE group_id = ?",
            (group_id,),
        ).fetchall()
        return [self._row_to_semantic_profile(r) for r in rows]

    def _row_to_semantic_profile(self, row: sqlite3.Row) -> dict[str, Any]:
        """将数据库行转换为语义画名字典。"""
        return {
            "group_id": row["group_id"],
            "user_id": row["user_id"],
            "name": row["name"],
            "engagement_rate": row["engagement_rate"],
            "interaction_count": row["interaction_count"],
            "first_interaction_at": row["first_interaction_at"],
            "last_interaction_at": row["last_interaction_at"],
        }

    def get_group_semantic_profile(self, group_id: str) -> dict[str, Any] | None:
        """获取群组语义画像。"""
        row = self.execute(
            "SELECT * FROM group_semantic_profiles WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_group_semantic_profile(row)

    def save_group_semantic_profile(self, group_id: str, profile: dict[str, Any]) -> None:
        """保存群组语义画像。"""
        self.execute(
            """
            INSERT INTO group_semantic_profiles (
                group_id, group_name, updated_at
            ) VALUES (?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                group_name=excluded.group_name,
                updated_at=excluded.updated_at
            """,
            (
                group_id,
                profile.get("group_name", ""),
                _now_iso(),
            ),
        )
        # 保存 pending_ai_responses
        self.execute(
            "DELETE FROM group_pending_ai_responses WHERE group_id = ?",
            (group_id,),
        )
        for record in profile.get("pending_ai_responses", []):
            self.execute(
                """
                INSERT INTO group_pending_ai_responses (
                    group_id, sent_at, target_user_id, topic_hint,
                    response_length, was_engaged, engagement_latency_s
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    record.get("sent_at", ""),
                    record.get("target_user_id", ""),
                    record.get("topic_hint", ""),
                    int(record.get("response_length", 0)),
                    1 if record.get("was_engaged") else 0,
                    float(record.get("engagement_latency_s", 0.0)),
                ),
            )
        self.commit()

    def _row_to_group_semantic_profile(self, row: sqlite3.Row) -> dict[str, Any]:
        """将数据库行转换为群组语义画名字典。"""
        group_id = row["group_id"]

        # 加载 pending_ai_responses
        pending_rows = self.execute(
            "SELECT * FROM group_pending_ai_responses WHERE group_id = ?",
            (group_id,),
        ).fetchall()
        pending_ai_responses = [
            {
                "sent_at": r["sent_at"],
                "target_user_id": r["target_user_id"],
                "topic_hint": r["topic_hint"],
                "response_length": r["response_length"],
                "was_engaged": bool(r["was_engaged"]),
                "engagement_latency_s": r["engagement_latency_s"],
            }
            for r in pending_rows
        ]

        return {
            "group_id": group_id,
            "group_name": row["group_name"],
            "pending_ai_responses": pending_ai_responses,
        }

    # ── 日记元数据 CRUD ─────────────────────────────────

    def get_diary_meta(self, group_id: str) -> list[str]:
        """获取群组的日记尾部重叠源 ID 列表。"""
        row = self.execute(
            "SELECT last_tail_sources FROM diary_meta WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if row is None:
            return []
        return json.loads(row["last_tail_sources"] or "[]")

    def save_diary_meta(self, group_id: str, last_tail_sources: list[str]) -> None:
        """保存群组的日记尾部重叠源 ID 列表。"""
        self.execute(
            """
            INSERT INTO diary_meta (group_id, last_tail_sources, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                last_tail_sources=excluded.last_tail_sources,
                updated_at=excluded.updated_at
            """,
            (group_id, json.dumps(last_tail_sources, ensure_ascii=False), _now_iso()),
        )
        self.commit()
