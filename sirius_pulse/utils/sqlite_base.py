"""SQLite 存储基类。

提供统一的连接管理、PRAGMA 配置和基础 CRUD 操作。
所有 SQLite 存储类应继承此基类以消除重复代码。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["BaseSqliteStore", "configure_sqlite_connection", "open_sqlite_connection"]


def configure_sqlite_connection(
    conn: sqlite3.Connection,
    *,
    row_factory: bool = True,
    foreign_keys: bool = True,
    journal_mode: str | None = "WAL",
    query_only: bool = False,
) -> sqlite3.Connection:
    """Apply the project's standard SQLite connection pragmas."""
    if row_factory:
        conn.row_factory = sqlite3.Row
    if journal_mode:
        mode = journal_mode.strip().upper()
        if not mode.replace("_", "").isalnum():
            raise ValueError(f"invalid SQLite journal_mode: {journal_mode!r}")
        conn.execute(f"PRAGMA journal_mode={mode}")
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON")
    if query_only:
        conn.execute("PRAGMA query_only=ON")
    return conn


def open_sqlite_connection(
    db_path: Path | str,
    *,
    timeout: float = 10,
    check_same_thread: bool = True,
    row_factory: bool = True,
    foreign_keys: bool = True,
    journal_mode: str | None = "WAL",
    query_only: bool = False,
    create_parent: bool = True,
) -> sqlite3.Connection:
    """Open a SQLite connection with shared project defaults."""
    path = Path(db_path)
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        check_same_thread=check_same_thread,
        timeout=timeout,
    )
    return configure_sqlite_connection(
        conn,
        row_factory=row_factory,
        foreign_keys=foreign_keys,
        journal_mode=journal_mode,
        query_only=query_only,
    )


class BaseSqliteStore:
    """SQLite 存储基类。

    封装通用的连接管理、PRAGMA 配置和基础操作。
    子类只需实现 _create_tables() 方法定义表结构。

    Parameters
    ----------
    db_path:
        SQLite 数据库文件路径。传入 conn 时可省略。
    conn:
        可选的共享 SQLite 连接。传入时复用该连接，不再自行管理生命周期。
    read_only:
        只读模式。为 True 时跳过 _create_tables() 并设置 query_only PRAGMA，
        避免与写入方产生锁冲突（适用于 WebUI 只读 API）。
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        conn: sqlite3.Connection | None = None,
        read_only: bool = False,
    ) -> None:
        self._read_only = read_only

        # 共享连接模式：外部传入 conn 时复用，本实例不负责关闭
        if conn is not None:
            self._conn = conn
            self._own_conn = False
            self._db_path = None
        else:
            if db_path is None:
                raise ValueError("db_path 和 conn 不能同时为空")
            self._db_path = Path(db_path)
            self._conn = open_sqlite_connection(
                self._db_path,
                check_same_thread=False,
                query_only=read_only,
            )
            self._own_conn = True

        if conn is not None:
            configure_sqlite_connection(conn, query_only=read_only)

        if read_only:
            # 只读模式：禁止一切写操作，避免与写入方锁冲突
            pass
        else:
            # 子类实现表结构创建
            self._create_tables()

    def _create_tables(self) -> None:
        """创建表结构，子类必须实现。"""
        raise NotImplementedError("子类必须实现 _create_tables() 方法")

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        """获取数据库连接。"""
        return self._conn

    @property
    def db_path(self) -> Path | None:
        """获取数据库文件路径（共享连接时为 None）。"""
        return self._db_path

    def close(self) -> None:
        """关闭数据库连接（仅关闭自有连接）。"""
        if self._own_conn and self._conn:
            self._conn.close()

    def __enter__(self) -> BaseSqliteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 基础 CRUD 操作
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        """执行单条 SQL 语句。"""
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, params_list: list[tuple] | list[list]) -> sqlite3.Cursor:
        """批量执行 SQL 语句。"""
        return self._conn.executemany(sql, params_list)

    def executescript(self, sql: str) -> None:
        """执行 SQL 脚本（多条语句）。"""
        self._conn.executescript(sql)

    def commit(self) -> None:
        """提交当前事务。"""
        self._conn.commit()

    def fetchone(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        """执行查询并返回单行结果（字典形式）。"""
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        """执行查询并返回所有结果（字典列表形式）。"""
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Schema 自动迁移
    # ------------------------------------------------------------------

    def _ensure_columns(self, table_name: str, columns: dict[str, str]) -> None:
        """确保表包含所有预期列，自动补齐缺失列。

        CREATE TABLE IF NOT EXISTS 不会修改已存在的表结构，
        此方法通过 PRAGMA table_info 检测差异并 ALTER TABLE 补列。

        以后需要给表加字段时，只需：
        1. 在 CREATE TABLE 中添加列定义（新库生效）
        2. 在 _create_tables 中调用此方法补齐旧库

        Parameters
        ----------
        table_name:
            表名
        columns:
            {列名: "TYPE DEFAULT value"} 格式的期望列定义
        """
        existing = {
            row["name"] for row in self.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        for col_name, col_def in columns.items():
            if col_name not in existing:
                try:
                    self.execute(f"ALTER TABLE {table_name}" f" ADD COLUMN {col_name} {col_def}")
                    self.commit()
                    logger.info("自动迁移：为 %s 表补齐列 %s", table_name, col_name)
                except sqlite3.OperationalError:
                    pass

    # ------------------------------------------------------------------
    # Schema 版本管理
    # ------------------------------------------------------------------

    def get_schema_version(self, key: str = "schema_version") -> int:
        """获取当前 schema 版本号。"""
        row = self._conn.execute("SELECT value FROM _meta WHERE key = ?", (key,)).fetchone()
        return int(row[0]) if row else 0

    def set_schema_version(self, version: int, key: str = "schema_version") -> None:
        """设置 schema 版本号。"""
        self._conn.execute(
            "INSERT OR REPLACE INTO _meta(key, value) VALUES(?, ?)",
            (key, str(version)),
        )
        self._conn.commit()

    def ensure_meta_table(self) -> None:
        """确保 _meta 表存在。"""
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS _meta (" "key TEXT PRIMARY KEY, value TEXT NOT NULL" ")"
        )
        self._conn.commit()
