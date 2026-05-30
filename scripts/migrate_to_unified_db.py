"""迁移脚本：将旧的独立数据库合并到统一的 persona.db。

将以下数据库的数据迁移到 persona.db：
- memory.db (用户数据、别名、语义画像)
- token/token_usage.db (Token 用量记录)
- cognition_events.db (认知事件、决策事件)
- sessions/*/session_state.db (会话状态)

用法：
    python scripts/migrate_to_unified_db.py <persona_path>

示例：
    python scripts/migrate_to_unified_db.py data/personas/sirius
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# 需要迁移的 memory.db 表（按依赖顺序）
_MEMORY_TABLES = [
    "users",
    "user_identities",
    "group_members",
    "aliases",
    "semantic_profiles",
    "response_records",
    "group_semantic_profiles",
    "atmosphere_history",
    "group_pending_ai_responses",
]

# 需要迁移的 token_usage.db 表
_TOKEN_TABLES = [
    "token_usage",
]

# 需要迁移的 cognition_events.db 表
_COGNITION_TABLES = [
    "cognition_events",
    "decision_events",
]

# 需要迁移的 session_state.db 表（需要添加 session_id 列）
_SESSION_TABLES = [
    "session_meta",
    "session_messages",
    "session_reply_runtime",
    "session_reply_runtime_user_turns",
    "session_reply_runtime_group_turns",
    "session_reply_runtime_assistant_turns",
    "session_user_profiles",
    "session_user_runtime",
    "session_user_memory_facts",
    "session_token_usage_records",
]


def _find_db(original: Path, backup_dir: Path) -> Path | None:
    """查找数据库文件，优先从原始位置，其次从备份目录。

    在备份目录中查找时，同时检查：
    - backup_dir/filename（扁平结构）
    - backup_dir/原相对路径（目录结构保持）
    """
    if original.exists():
        return original
    # 尝试扁平结构：backup_dir/filename
    flat_backup = backup_dir / original.name
    if flat_backup.exists():
        return flat_backup
    # 尝试保持目录结构：backup_dir/subdir/filename
    structured_backup = backup_dir / original.parent.name / original.name
    if structured_backup.exists():
        return structured_backup
    return None


def migrate_persona(persona_path: Path, *, backup: bool = True) -> None:
    """迁移一个人格目录的所有数据库到统一的 persona.db。

    优先从原始位置读取数据库，如果不存在则从 migrated_backup/ 目录读取。

    Args:
        persona_path: 人格目录路径
        backup: 是否备份旧数据库文件
    """
    logger.info("开始迁移: %s", persona_path)

    persona_db_path = persona_path / "persona.db"
    conn = sqlite3.connect(str(persona_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # 创建所有表结构
    _create_all_tables(conn)

    backup_dir = persona_path / "migrated_backup"

    # 迁移 memory.db（优先原始位置，其次备份目录）
    memory_db_path = _find_db(persona_path / "memory.db", backup_dir)
    if memory_db_path:
        _migrate_memory_db(conn, memory_db_path, persona_path, backup)

    # 迁移 token_usage.db（优先原始位置，其次备份目录）
    token_db_path = _find_db(persona_path / "token" / "token_usage.db", backup_dir)
    if token_db_path:
        _migrate_token_db(conn, token_db_path, persona_path, backup)

    # 迁移 cognition_events.db（优先原始位置，其次备份目录）
    cognition_db_path = _find_db(persona_path / "cognition_events.db", backup_dir)
    if cognition_db_path:
        _migrate_cognition_db(conn, cognition_db_path, persona_path, backup)

    # 迁移 session_state.db（遍历所有会话目录，包括备份）
    for sessions_root in [persona_path / "sessions", backup_dir / "sessions"]:
        if not sessions_root.is_dir():
            continue
        for session_dir in sessions_root.iterdir():
            if not session_dir.is_dir():
                continue
            session_db_path = session_dir / "session_state.db"
            if session_db_path.exists():
                session_id = session_dir.name
                _migrate_session_db(conn, session_db_path, session_id, persona_path, backup)

    conn.close()
    logger.info("迁移完成: %s -> %s", persona_path, persona_db_path)


def _create_all_tables(conn: sqlite3.Connection) -> None:
    """创建 persona.db 中的所有表结构。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS _meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

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

        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            actor_id TEXT NOT NULL,
            task_name TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            input_chars INTEGER NOT NULL DEFAULT 0,
            output_chars INTEGER NOT NULL DEFAULT 0,
            estimation_method TEXT NOT NULL DEFAULT 'char_div4',
            retries_used INTEGER NOT NULL DEFAULT 0,
            persona_name TEXT NOT NULL DEFAULT '',
            group_id TEXT NOT NULL DEFAULT '',
            provider_name TEXT NOT NULL DEFAULT '',
            breakdown_json TEXT NOT NULL DEFAULT '',
            duration_ms REAL NOT NULL DEFAULT 0,
            error_type TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            conversation_depth INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_tu_session ON token_usage(session_id);
        CREATE INDEX IF NOT EXISTS idx_tu_actor ON token_usage(actor_id);
        CREATE INDEX IF NOT EXISTS idx_tu_task ON token_usage(task_name);
        CREATE INDEX IF NOT EXISTS idx_tu_model ON token_usage(model);
        CREATE INDEX IF NOT EXISTS idx_tu_ts ON token_usage(timestamp);
        CREATE INDEX IF NOT EXISTS idx_tu_persona ON token_usage(persona_name);
        CREATE INDEX IF NOT EXISTS idx_tu_group ON token_usage(group_id);
        CREATE INDEX IF NOT EXISTS idx_tu_provider ON token_usage(provider_name);

        CREATE TABLE IF NOT EXISTS cognition_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            group_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL DEFAULT '',
            valence REAL NOT NULL DEFAULT 0,
            arousal REAL NOT NULL DEFAULT 0.3,
            basic_emotion TEXT NOT NULL DEFAULT '',
            intensity REAL NOT NULL DEFAULT 0.5,
            social_intent TEXT NOT NULL DEFAULT '',
            urgency_score REAL NOT NULL DEFAULT 0,
            relevance_score REAL NOT NULL DEFAULT 0.5,
            confidence REAL NOT NULL DEFAULT 0.8,
            directed_score REAL NOT NULL DEFAULT 0,
            sarcasm_score REAL NOT NULL DEFAULT 0,
            entitlement_score REAL NOT NULL DEFAULT 0,
            turn_gap_readiness REAL NOT NULL DEFAULT 0.5,
            directed_signals TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_ce_ts ON cognition_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_ce_group ON cognition_events(group_id);
        CREATE INDEX IF NOT EXISTS idx_ce_user ON cognition_events(user_id);

        CREATE TABLE IF NOT EXISTS decision_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            group_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL DEFAULT '',
            strategy TEXT NOT NULL DEFAULT 'silent',
            score REAL NOT NULL DEFAULT 0,
            threshold REAL NOT NULL DEFAULT 0.5,
            reason TEXT NOT NULL DEFAULT '',
            directed_score REAL NOT NULL DEFAULT 0,
            urgency REAL NOT NULL DEFAULT 0,
            entitlement REAL NOT NULL DEFAULT 0,
            sarcasm REAL NOT NULL DEFAULT 0,
            heat_level TEXT NOT NULL DEFAULT 'warm',
            msg_rate REAL NOT NULL DEFAULT 0,
            cooldown REAL NOT NULL DEFAULT 0,
            since_reply REAL NOT NULL DEFAULT 0,
            expressiveness REAL NOT NULL DEFAULT 0.5,
            sensitivity REAL NOT NULL DEFAULT 0.5,
            affinity REAL NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_de_ts ON decision_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_de_group ON decision_events(group_id);
        CREATE INDEX IF NOT EXISTS idx_de_strategy ON decision_events(strategy);

        CREATE TABLE IF NOT EXISTS session_meta (
            session_id TEXT NOT NULL DEFAULT '',
            session_summary TEXT NOT NULL DEFAULT '',
            orchestration_stats TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (session_id)
        );

        CREATE TABLE IF NOT EXISTS session_messages (
            session_id TEXT NOT NULL DEFAULT '',
            message_index INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            speaker TEXT,
            channel TEXT,
            channel_user_id TEXT,
            multimodal_inputs TEXT NOT NULL DEFAULT '[]',
            reply_mode TEXT NOT NULL DEFAULT 'always',
            PRIMARY KEY (session_id, message_index)
        );

        CREATE INDEX IF NOT EXISTS idx_session_messages_role ON session_messages(session_id, role);

        CREATE TABLE IF NOT EXISTS session_reply_runtime (
            session_id TEXT NOT NULL DEFAULT '',
            last_assistant_reply_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (session_id)
        );

        CREATE TABLE IF NOT EXISTS session_reply_runtime_user_turns (
            session_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL,
            last_turn_at TEXT NOT NULL,
            PRIMARY KEY (session_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS session_reply_runtime_group_turns (
            session_id TEXT NOT NULL DEFAULT '',
            seq INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            PRIMARY KEY (session_id, seq)
        );

        CREATE TABLE IF NOT EXISTS session_reply_runtime_assistant_turns (
            session_id TEXT NOT NULL DEFAULT '',
            seq INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            PRIMARY KEY (session_id, seq)
        );

        CREATE TABLE IF NOT EXISTS session_user_profiles (
            session_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            persona TEXT NOT NULL DEFAULT '',
            identities TEXT NOT NULL DEFAULT '{}',
            aliases TEXT NOT NULL DEFAULT '[]',
            traits TEXT NOT NULL DEFAULT '[]',
            metadata TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (session_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS session_user_runtime (
            session_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL,
            inferred_persona TEXT NOT NULL DEFAULT '',
            inferred_traits TEXT NOT NULL DEFAULT '[]',
            preference_tags TEXT NOT NULL DEFAULT '[]',
            recent_messages TEXT NOT NULL DEFAULT '[]',
            summary_notes TEXT NOT NULL DEFAULT '[]',
            last_seen_channel TEXT NOT NULL DEFAULT '',
            last_seen_uid TEXT NOT NULL DEFAULT '',
            observed_keywords TEXT NOT NULL DEFAULT '[]',
            observed_roles TEXT NOT NULL DEFAULT '[]',
            observed_emotions TEXT NOT NULL DEFAULT '[]',
            observed_entities TEXT NOT NULL DEFAULT '[]',
            last_event_processed_at TEXT,
            PRIMARY KEY (session_id, user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_session_user_runtime_channel ON session_user_runtime(session_id, last_seen_channel);

        CREATE TABLE IF NOT EXISTS session_user_memory_facts (
            session_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL,
            fact_index INTEGER NOT NULL,
            fact_type TEXT NOT NULL,
            value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'unknown',
            confidence REAL NOT NULL DEFAULT 0.5,
            observed_at TEXT NOT NULL DEFAULT '',
            observed_time_desc TEXT NOT NULL DEFAULT '',
            memory_category TEXT NOT NULL DEFAULT 'custom',
            validated INTEGER NOT NULL DEFAULT 0,
            conflict_with TEXT NOT NULL DEFAULT '[]',
            context_channel TEXT NOT NULL DEFAULT '',
            context_topic TEXT NOT NULL DEFAULT '',
            context_metadata TEXT NOT NULL DEFAULT '{}',
            mention_count INTEGER NOT NULL DEFAULT 0,
            source_event_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (session_id, user_id, fact_index)
        );

        CREATE INDEX IF NOT EXISTS idx_session_user_memory_facts_user ON session_user_memory_facts(session_id, user_id);

        CREATE TABLE IF NOT EXISTS session_token_usage_records (
            session_id TEXT NOT NULL DEFAULT '',
            record_index INTEGER NOT NULL,
            actor_id TEXT NOT NULL,
            task_name TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            input_chars INTEGER NOT NULL DEFAULT 0,
            output_chars INTEGER NOT NULL DEFAULT 0,
            estimation_method TEXT NOT NULL DEFAULT 'char_div4',
            retries_used INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (session_id, record_index)
        );

        CREATE INDEX IF NOT EXISTS idx_session_token_usage_task ON session_token_usage_records(session_id, task_name);
    """)
    conn.commit()
    logger.info("已创建所有表结构")


def _migrate_table_data(
    src_conn: sqlite3.Connection,
    dst_conn: sqlite3.Connection,
    table_name: str,
    extra_columns: dict[str, str] | None = None,
    session_id: str | None = None,
) -> int:
    """将源表数据复制到目标表。

    Args:
        src_conn: 源数据库连接
        dst_conn: 目标数据库连接
        table_name: 表名
        extra_columns: 额外添加的列 {列名: 默认值}
        session_id: 会话 ID（用于 session 表）

    Returns:
        迁移的行数
    """
    try:
        # 检查源表是否存在
        src_table_exists = src_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        ).fetchone()
        if not src_table_exists:
            return 0

        # 检查目标表是否存在
        dst_table_exists = dst_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        ).fetchone()
        if not dst_table_exists:
            logger.warning("目标表 %s 不存在，跳过", table_name)
            return 0

        # 获取源表的列名
        src_columns = [row[1] for row in src_conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
        # 获取目标表的列名
        dst_columns = [row[1] for row in dst_conn.execute(f"PRAGMA table_info({table_name})").fetchall()]

        # 确定要复制的列（源表和目标表的交集）
        common_columns = [col for col in src_columns if col in dst_columns]
        if not common_columns:
            logger.warning("表 %s 没有共同的列，跳过", table_name)
            return 0

        # 构建 SELECT 和 INSERT 语句
        select_cols = ", ".join(common_columns)
        insert_cols = ", ".join(common_columns)
        placeholders = ", ".join(["?"] * len(common_columns))

        # 读取源数据
        rows = src_conn.execute(f"SELECT {select_cols} FROM {table_name}").fetchall()

        # 写入目标表
        count = 0
        for row in rows:
            values = list(row)
            dst_conn.execute(
                f"INSERT OR REPLACE INTO {table_name} ({insert_cols}) VALUES ({placeholders})",
                values
            )
            count += 1

        dst_conn.commit()
        return count

    except Exception as exc:
        logger.warning("迁移表 %s 失败: %s", table_name, exc)
        return 0


def _migrate_memory_db(
    conn: sqlite3.Connection,
    memory_db_path: Path,
    persona_path: Path,
    backup: bool,
) -> None:
    """迁移 memory.db 到 persona.db。"""
    logger.info("迁移 memory.db: %s", memory_db_path)

    try:
        src_conn = sqlite3.connect(str(memory_db_path))
        src_conn.row_factory = sqlite3.Row

        total = 0
        for table_name in _MEMORY_TABLES:
            count = _migrate_table_data(src_conn, conn, table_name)
            if count > 0:
                logger.info("  迁移表 %s: %d 行", table_name, count)
                total += count

        src_conn.close()
        logger.info("memory.db 迁移完成: %d 行", total)

        if backup:
            backup_path = persona_path / "migrated_backup" / "memory.db"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            memory_db_path.rename(backup_path)
            logger.info("已备份到: %s", backup_path)

    except Exception as exc:
        logger.error("迁移 memory.db 失败: %s", exc)


def _migrate_token_db(
    conn: sqlite3.Connection,
    token_db_path: Path,
    persona_path: Path,
    backup: bool,
) -> None:
    """迁移 token_usage.db 到 persona.db。"""
    logger.info("迁移 token_usage.db: %s", token_db_path)

    try:
        src_conn = sqlite3.connect(str(token_db_path))
        src_conn.row_factory = sqlite3.Row

        total = 0
        for table_name in _TOKEN_TABLES:
            count = _migrate_table_data(src_conn, conn, table_name)
            if count > 0:
                logger.info("  迁移表 %s: %d 行", table_name, count)
                total += count

        # 迁移 _meta 表中的 token_schema_version
        try:
            row = src_conn.execute(
                "SELECT value FROM _meta WHERE key = 'schema_version'"
            ).fetchone()
            if row:
                conn.execute(
                    "INSERT OR REPLACE INTO _meta(key, value) VALUES(?, ?)",
                    ("token_schema_version", row[0])
                )
                conn.commit()
        except Exception:
            pass

        src_conn.close()
        logger.info("token_usage.db 迁移完成: %d 行", total)

        if backup:
            backup_path = persona_path / "migrated_backup" / "token_usage.db"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            token_db_path.rename(backup_path)
            logger.info("已备份到: %s", backup_path)

    except Exception as exc:
        logger.error("迁移 token_usage.db 失败: %s", exc)


def _migrate_cognition_db(
    conn: sqlite3.Connection,
    cognition_db_path: Path,
    persona_path: Path,
    backup: bool,
) -> None:
    """迁移 cognition_events.db 到 persona.db。"""
    logger.info("迁移 cognition_events.db: %s", cognition_db_path)

    try:
        src_conn = sqlite3.connect(str(cognition_db_path))
        src_conn.row_factory = sqlite3.Row

        total = 0
        for table_name in _COGNITION_TABLES:
            count = _migrate_table_data(src_conn, conn, table_name)
            if count > 0:
                logger.info("  迁移表 %s: %d 行", table_name, count)
                total += count

        # 迁移 _meta 表中的 cognition_schema_version
        try:
            row = src_conn.execute(
                "SELECT value FROM _meta WHERE key = 'schema_version'"
            ).fetchone()
            if row:
                conn.execute(
                    "INSERT OR REPLACE INTO _meta(key, value) VALUES(?, ?)",
                    ("cognition_schema_version", row[0])
                )
                conn.commit()
        except Exception:
            pass

        src_conn.close()
        logger.info("cognition_events.db 迁移完成: %d 行", total)

        if backup:
            backup_path = persona_path / "migrated_backup" / "cognition_events.db"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            cognition_db_path.rename(backup_path)
            logger.info("已备份到: %s", backup_path)

    except Exception as exc:
        logger.error("迁移 cognition_events.db 失败: %s", exc)


def _migrate_session_db(
    conn: sqlite3.Connection,
    session_db_path: Path,
    session_id: str,
    persona_path: Path,
    backup: bool,
) -> None:
    """迁移 session_state.db 到 persona.db。"""
    logger.info("迁移 session_state.db: %s (session_id=%s)", session_db_path, session_id)

    try:
        src_conn = sqlite3.connect(str(session_db_path))
        src_conn.row_factory = sqlite3.Row

        total = 0
        for table_name in _SESSION_TABLES:
            count = _migrate_session_table(src_conn, conn, table_name, session_id)
            if count > 0:
                logger.info("  迁移表 %s: %d 行", table_name, count)
                total += count

        # 迁移 _meta 表中的 session_store_schema_version
        try:
            row = src_conn.execute(
                "SELECT value FROM _meta WHERE key = 'session_store_schema_version'"
            ).fetchone()
            if row:
                conn.execute(
                    "INSERT OR REPLACE INTO _meta(key, value) VALUES(?, ?)",
                    ("session_store_schema_version", row[0])
                )
                conn.commit()
        except Exception:
            pass

        src_conn.close()
        logger.info("session_state.db 迁移完成: %d 行", total)

        if backup:
            backup_path = persona_path / "migrated_backup" / "sessions" / session_id / "session_state.db"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            session_db_path.rename(backup_path)
            logger.info("已备份到: %s", backup_path)

    except Exception as exc:
        logger.error("迁移 session_state.db 失败: %s", exc)


def _migrate_session_table(
    src_conn: sqlite3.Connection,
    dst_conn: sqlite3.Connection,
    table_name: str,
    session_id: str,
) -> int:
    """迁移 session 表，添加 session_id 列。

    Args:
        src_conn: 源数据库连接
        dst_conn: 目标数据库连接
        table_name: 表名
        session_id: 会话 ID

    Returns:
        迁移的行数
    """
    try:
        # 检查源表是否存在
        src_table_exists = src_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        ).fetchone()
        if not src_table_exists:
            return 0

        # 检查目标表是否存在
        dst_table_exists = dst_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        ).fetchone()
        if not dst_table_exists:
            logger.warning("目标表 %s 不存在，跳过", table_name)
            return 0

        # 获取源表的列名
        src_columns = [row[1] for row in src_conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
        # 获取目标表的列名
        dst_columns = [row[1] for row in dst_conn.execute(f"PRAGMA table_info({table_name})").fetchall()]

        # 检查目标表是否有 session_id 列
        has_session_id = "session_id" in dst_columns
        if not has_session_id:
            logger.warning("目标表 %s 没有 session_id 列，跳过", table_name)
            return 0

        # 确定要复制的列（源表和目标表的交集，排除 session_id）
        common_columns = [col for col in src_columns if col in dst_columns and col != "session_id"]
        if not common_columns:
            logger.warning("表 %s 没有共同的列，跳过", table_name)
            return 0

        # 构建 SELECT 和 INSERT 语句
        select_cols = ", ".join(common_columns)
        insert_cols = "session_id, " + ", ".join(common_columns)
        placeholders = ", ".join(["?"] * (len(common_columns) + 1))

        # 读取源数据
        rows = src_conn.execute(f"SELECT {select_cols} FROM {table_name}").fetchall()

        # 写入目标表
        count = 0
        for row in rows:
            values = [session_id] + list(row)
            dst_conn.execute(
                f"INSERT OR REPLACE INTO {table_name} ({insert_cols}) VALUES ({placeholders})",
                values
            )
            count += 1

        dst_conn.commit()
        return count

    except Exception as exc:
        logger.warning("迁移表 %s 失败: %s", table_name, exc)
        return 0


def main() -> None:
    """命令行入口。"""
    if len(sys.argv) < 2:
        print("用法: python scripts/migrate_to_unified_db.py <persona_path>")
        print("示例: python scripts/migrate_to_unified_db.py data/personas/sirius")
        sys.exit(1)

    persona_path = Path(sys.argv[1])
    if not persona_path.exists():
        print(f"路径不存在: {persona_path}")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    migrate_persona(persona_path)


if __name__ == "__main__":
    main()
