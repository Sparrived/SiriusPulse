"""迁移脚本：将 JSON 数据迁移到 SQLite。

用法：
    python -m sirius_pulse.memory.migrate_to_sqlite <persona_path>

示例：
    python -m sirius_pulse.memory.migrate_to_sqlite data/personas/sirius
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def migrate_persona(persona_path: Path, *, backup: bool = True) -> None:
    """迁移一个人格目录的数据到 SQLite。

    Args:
        persona_path: 人格目录路径
        backup: 是否备份旧文件
    """
    logger.info("开始迁移: %s", persona_path)

    db_path = persona_path / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # 创建表
    _create_tables(conn)

    # 备份目录
    backup_dir = persona_path / "migrated_backup"
    if backup:
        backup_dir.mkdir(exist_ok=True)

    # 迁移 user_manager.json
    user_mgr_path = persona_path / "user_manager.json"
    if user_mgr_path.exists():
        _migrate_user_manager(conn, user_mgr_path)
        if backup:
            shutil.move(str(user_mgr_path), str(backup_dir / user_mgr_path.name))

    # 迁移 alias_index.json
    alias_path = persona_path / "alias_index.json"
    if alias_path.exists():
        _migrate_alias_index(conn, alias_path)
        if backup:
            shutil.move(str(alias_path), str(backup_dir / alias_path.name))

    # 迁移 biography/*.json
    bio_dir = persona_path / "memory" / "biography"
    if bio_dir.is_dir():
        _migrate_biography(conn, bio_dir)
        if backup:
            bio_backup = backup_dir / "biography"
            bio_backup.mkdir(exist_ok=True)
            for f in bio_dir.glob("*.json"):
                shutil.move(str(f), str(bio_backup / f.name))

    # 迁移 semantic profiles
    semantic_dir = persona_path / "memory" / "semantic"
    if semantic_dir.is_dir():
        _migrate_semantic_profiles(conn, semantic_dir)
        if backup:
            semantic_backup = backup_dir / "semantic"
            semantic_backup.mkdir(exist_ok=True)
            for f in semantic_dir.rglob("*.json"):
                relative = f.relative_to(semantic_dir)
                dest = semantic_backup / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(dest))

    conn.close()
    logger.info("迁移完成: %s -> %s", persona_path, db_path)
    if backup:
        logger.info("备份文件已移动到: %s", backup_dir)


def _create_tables(conn: sqlite3.Connection) -> None:
    """创建表结构。"""
    conn.executescript("""
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


def _migrate_user_manager(conn: sqlite3.Connection, path: Path) -> None:
    """迁移 user_manager.json。"""
    logger.info("迁移用户数据: %s", path.name)

    data = json.loads(path.read_text(encoding="utf-8"))
    now = _now_iso()

    # 迁移全局用户
    global_data = data.get("global", {})
    for user_id, user_data in global_data.items():
        if not isinstance(user_data, dict):
            continue

        conn.execute(
            """
            INSERT OR REPLACE INTO users (
                user_id, name, persona, identities, aliases, traits,
                group_memberships, metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                user_data.get("name", ""),
                user_data.get("persona", ""),
                json.dumps(user_data.get("identities", {}), ensure_ascii=False),
                json.dumps(user_data.get("aliases", []), ensure_ascii=False),
                json.dumps(user_data.get("traits", []), ensure_ascii=False),
                json.dumps(user_data.get("group_memberships", {}), ensure_ascii=False),
                json.dumps(user_data.get("metadata", {}), ensure_ascii=False),
                now,
                now,
            ),
        )

        # 保存平台身份
        identities = user_data.get("identities", {})
        for platform, platform_uid in identities.items():
            if platform and platform_uid:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO user_identities (platform, platform_uid, user_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (platform, platform_uid, user_id, now),
                )

    # 迁移群组成员
    entries_data = data.get("entries", {})
    for group_id, group in entries_data.items():
        for user_id, user_data in group.items():
            if not isinstance(user_data, dict):
                continue

            # 确保用户存在
            conn.execute(
                """
                INSERT OR IGNORE INTO users (user_id, name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, user_data.get("name", ""), now, now),
            )

            # 添加群组成员关系
            conn.execute(
                """
                INSERT OR IGNORE INTO group_members (group_id, user_id, joined_at)
                VALUES (?, ?, ?)
                """,
                (group_id, user_id, now),
            )

    conn.commit()
    logger.info("用户数据迁移完成: %d 全局用户, %d 群组",
                len(global_data), len(entries_data))


def _migrate_alias_index(conn: sqlite3.Connection, path: Path) -> None:
    """迁移 alias_index.json。"""
    logger.info("迁移别名索引: %s", path.name)

    data = json.loads(path.read_text(encoding="utf-8"))
    now = _now_iso()

    count = 0
    for alias, entries in data.items():
        if not isinstance(entries, list):
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            conn.execute(
                """
                INSERT OR REPLACE INTO aliases (
                    alias, user_id, user_name, weight, groups, mentioned_count,
                    confidence, first_seen_at, last_seen_at, source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alias,
                    entry.get("user_id", ""),
                    entry.get("user_name", ""),
                    float(entry.get("weight", 1.0)),
                    json.dumps(entry.get("groups", []), ensure_ascii=False),
                    int(entry.get("mentioned_count", 1)),
                    float(entry.get("confidence", 0.5)),
                    entry.get("first_seen_at", ""),
                    entry.get("last_seen_at", ""),
                    entry.get("source", "napcat"),
                    now,
                ),
            )
            count += 1

    conn.commit()
    logger.info("别名索引迁移完成: %d 别名, %d 条目", len(data), count)


def _migrate_biography(conn: sqlite3.Connection, bio_dir: Path) -> None:
    """迁移 biography/*.json。"""
    logger.info("迁移传记数据: %s", bio_dir.name)

    now = _now_iso()
    count = 0

    for card_file in bio_dir.glob("*.json"):
        if card_file.name == "index.json" or card_file.name == "skill_system.json":
            continue

        try:
            data = json.loads(card_file.read_text(encoding="utf-8"))
            user_id = data.get("user_id", "")
            if not user_id:
                # 从文件名推断 user_id
                user_id = card_file.stem

            # 更新用户的传记字段
            conn.execute(
                """
                UPDATE users SET
                    identity_anchors = ?,
                    relationships = ?,
                    short_bio = ?,
                    affinity_score = ?,
                    pending_messages = ?,
                    pending_message_count = ?,
                    distilled_points = ?,
                    last_distill_at = ?,
                    bio_token_estimate = ?,
                    bio_token_budget = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    json.dumps(data.get("identity_anchors", []), ensure_ascii=False),
                    json.dumps(data.get("relationships", []), ensure_ascii=False),
                    data.get("short_bio", ""),
                    float(data.get("affinity_score", 0.0)),
                    json.dumps(data.get("pending_messages", []), ensure_ascii=False),
                    int(data.get("pending_message_count", 0)),
                    json.dumps(data.get("distilled_points", []), ensure_ascii=False),
                    data.get("last_distill_at", ""),
                    int(data.get("bio_token_estimate", 0)),
                    int(data.get("bio_token_budget", 500)),
                    now,
                    user_id,
                ),
            )
            count += 1
        except Exception as exc:
            logger.warning("迁移传记失败 %s: %s", card_file.name, exc)

    conn.commit()
    logger.info("传记数据迁移完成: %d 个传记", count)


def _migrate_semantic_profiles(conn: sqlite3.Connection, semantic_dir: Path) -> None:
    """迁移 semantic profiles。"""
    logger.info("迁移语义画像: %s", semantic_dir.name)

    now = _now_iso()
    count = 0

    # 迁移 global/*.json
    global_dir = semantic_dir / "global"
    if global_dir.is_dir():
        for profile_file in global_dir.glob("*.json"):
            try:
                data = json.loads(profile_file.read_text(encoding="utf-8"))
                user_id = data.get("user_id", profile_file.stem)

                # 作为全局语义画像存储（group_id = "__global__"）
                conn.execute(
                    """
                    INSERT OR REPLACE INTO semantic_profiles (group_id, user_id, profile_data, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("__global__", user_id, json.dumps(data, ensure_ascii=False), now),
                )
                count += 1
            except Exception as exc:
                logger.warning("迁移全局语义画像失败 %s: %s", profile_file.name, exc)

    # 迁移 groups/*.json
    groups_dir = semantic_dir / "groups"
    if groups_dir.is_dir():
        for profile_file in groups_dir.glob("*.json"):
            try:
                data = json.loads(profile_file.read_text(encoding="utf-8"))
                group_id = profile_file.stem

                conn.execute(
                    """
                    INSERT OR REPLACE INTO group_semantic_profiles (group_id, profile_data, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (group_id, json.dumps(data, ensure_ascii=False), now),
                )
            except Exception as exc:
                logger.warning("迁移群组语义画像失败 %s: %s", profile_file.name, exc)

    # 迁移 users/{group_id}/*.json
    users_dir = semantic_dir / "users"
    if users_dir.is_dir():
        for group_dir in users_dir.iterdir():
            if not group_dir.is_dir():
                continue
            group_id = group_dir.name

            for profile_file in group_dir.glob("*.json"):
                try:
                    data = json.loads(profile_file.read_text(encoding="utf-8"))
                    user_id = data.get("user_id", profile_file.stem)

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO semantic_profiles (group_id, user_id, profile_data, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (group_id, user_id, json.dumps(data, ensure_ascii=False), now),
                    )
                    count += 1
                except Exception as exc:
                    logger.warning("迁移用户语义画像失败 %s: %s", profile_file.name, exc)

    conn.commit()
    logger.info("语义画像迁移完成: %d 个画像", count)


def main() -> None:
    """命令行入口。"""
    import sys

    if len(sys.argv) < 2:
        print("用法: python -m sirius_pulse.memory.migrate_to_sqlite <persona_path>")
        print("示例: python -m sirius_pulse.memory.migrate_to_sqlite data/personas/sirius")
        sys.exit(1)

    persona_path = Path(sys.argv[1])
    if not persona_path.exists():
        print(f"路径不存在: {persona_path}")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    migrate_persona(persona_path)


if __name__ == "__main__":
    main()
