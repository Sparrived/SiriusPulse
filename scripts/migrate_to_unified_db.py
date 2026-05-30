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


def migrate_persona(persona_path: Path, *, backup: bool = True) -> None:
    """迁移一个人格目录的所有数据库到统一的 persona.db。

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

    # 确保 _meta 表存在
    conn.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()

    # 迁移 memory.db
    memory_db_path = persona_path / "memory.db"
    if memory_db_path.exists():
        _migrate_memory_db(conn, memory_db_path, persona_path, backup)

    # 迁移 token_usage.db
    token_db_path = persona_path / "token" / "token_usage.db"
    if token_db_path.exists():
        _migrate_token_db(conn, token_db_path, persona_path, backup)

    # 迁移 cognition_events.db
    cognition_db_path = persona_path / "cognition_events.db"
    if cognition_db_path.exists():
        _migrate_cognition_db(conn, cognition_db_path, persona_path, backup)

    # 迁移 session_state.db（遍历所有会话目录）
    sessions_dir = persona_path / "sessions"
    if sessions_dir.is_dir():
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            session_db_path = session_dir / "session_state.db"
            if session_db_path.exists():
                session_id = session_dir.name
                _migrate_session_db(conn, session_db_path, session_id, persona_path, backup)

    conn.close()
    logger.info("迁移完成: %s -> %s", persona_path, persona_db_path)


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
            count = _migrate_table_data(src_conn