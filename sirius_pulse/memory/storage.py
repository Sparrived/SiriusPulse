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
        self.executescript("""
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
                interest_topics TEXT DEFAULT '[]',
                group_norms TEXT DEFAULT '{}',
                taboo_topics TEXT DEFAULT '[]',
                dominant_topic TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS atmosphere_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                timestamp TEXT DEFAULT '',
                group_valence REAL DEFAULT 0.0,
                group_arousal REAL DEFAULT 0.0,
                active_participants INTEGER DEFAULT 0,
                FOREIGN KEY (group_id) REFERENCES group_semantic_profiles(group_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_atmosphere_history_group
                ON atmosphere_history(group_id);

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
        """)

    # ── 用户 CRUD ─────────────────────────────────────────

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        """获取用户。"""
        row = self.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    def save_user(self, user: dict[str, Any]) -> None:
        """保存用户（插入或更新）。"""
        now = _now_iso()
        self.execute(
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

    # ── 别名 CRUD ─────────────────────────────────────────

    def get_alias_entries(self, alias: str) -> list[dict[str, Any]]:
        """获取别名的所有条目。"""
        rows = self.execute(
            "SELECT * FROM aliases WHERE alias = ?", (alias,)
        ).fetchall()
        return [self._row_to_alias(row) for row in rows]

    def save_alias_entry(self, entry: dict[str, Any]) -> None:
        """保存别名条目。"""
        self.execute(
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
        self.commit()

    def delete_alias_entry(self, alias: str, user_id: str) -> None:
        """删除别名条目。"""
        self.execute(
            "DELETE FROM aliases WHERE alias = ? AND user_id = ?",
            (alias, user_id),
        )
        self.commit()

    def get_aliases_for_group(self, group_id: str) -> dict[str, str]:
        """获取群组相关的别名速查表。"""
        rows = self.execute(
            """
            SELECT DISTINCT alias, user_name FROM aliases
            WHERE groups LIKE ?
            """,
            (f'%"{group_id}"%',),
        ).fetchall()
        return {r["alias"]: r["user_name"] for r in rows}

    def get_all_aliases(self) -> dict[str, list[dict[str, Any]]]:
        """获取所有别名索引。"""
        rows = self.execute("SELECT * FROM aliases").fetchall()
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
        # 保存 response_records
        self.execute(
            "DELETE FROM response_records WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        for record in profile.get("pending_responses", []):
            self.execute(
                """
                INSERT INTO response_records (
                    group_id, user_id, sent_at, target_user_id, topic_hint,
                    response_length, was_engaged, engagement_latency_s
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    user_id,
                    record.get("sent_at", ""),
                    record.get("target_user_id", ""),
                    record.get("topic_hint", ""),
                    int(record.get("response_length", 0)),
                    1 if record.get("was_engaged") else 0,
                    float(record.get("engagement_latency_s", 0.0)),
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
        group_id = row["group_id"]
        user_id = row["user_id"]

        # 加载 response_records
        records = self.execute(
            "SELECT * FROM response_records WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchall()
        pending_responses = [
            {
                "sent_at": r["sent_at"],
                "target_user_id": r["target_user_id"],
                "topic_hint": r["topic_hint"],
                "response_length": r["response_length"],
                "was_engaged": bool(r["was_engaged"]),
                "engagement_latency_s": r["engagement_latency_s"],
            }
            for r in records
        ]

        return {
            "group_id": group_id,
            "user_id": user_id,
            "name": row["name"],
            "engagement_rate": row["engagement_rate"],
            "interaction_count": row["interaction_count"],
            "first_interaction_at": row["first_interaction_at"],
            "last_interaction_at": row["last_interaction_at"],
            "pending_responses": pending_responses,
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
                group_id, group_name, interest_topics, group_norms,
                taboo_topics, dominant_topic, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                group_name=excluded.group_name,
                interest_topics=excluded.interest_topics,
                group_norms=excluded.group_norms,
                taboo_topics=excluded.taboo_topics,
                dominant_topic=excluded.dominant_topic,
                updated_at=excluded.updated_at
            """,
            (
                group_id,
                profile.get("group_name", ""),
                json.dumps(profile.get("interest_topics", []), ensure_ascii=False),
                json.dumps(profile.get("group_norms", {}), ensure_ascii=False),
                json.dumps(profile.get("taboo_topics", []), ensure_ascii=False),
                profile.get("dominant_topic", ""),
                _now_iso(),
            ),
        )
        # 保存 atmosphere_history
        self.execute(
            "DELETE FROM atmosphere_history WHERE group_id = ?",
            (group_id,),
        )
        for snapshot in profile.get("atmosphere_history", []):
            self.execute(
                """
                INSERT INTO atmosphere_history (
                    group_id, timestamp, group_valence, group_arousal, active_participants
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    snapshot.get("timestamp", ""),
                    float(snapshot.get("group_valence", 0.0)),
                    float(snapshot.get("group_arousal", 0.0)),
                    int(snapshot.get("active_participants", 0)),
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

        # 加载 atmosphere_history
        history_rows = self.execute(
            "SELECT * FROM atmosphere_history WHERE group_id = ?",
            (group_id,),
        ).fetchall()
        atmosphere_history = [
            {
                "timestamp": r["timestamp"],
                "group_valence": r["group_valence"],
                "group_arousal": r["group_arousal"],
                "active_participants": r["active_participants"],
            }
            for r in history_rows
        ]

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
            "interest_topics": json.loads(row["interest_topics"]),
            "atmosphere_history": atmosphere_history,
            "group_norms": json.loads(row["group_norms"]),
            "taboo_topics": json.loads(row["taboo_topics"]),
            "dominant_topic": row["dominant_topic"],
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
