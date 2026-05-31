"""SQLite 存储基类。

提供统一的连接管理、PRAGMA 配置和基础 CRUD 操作。
所有 SQLite 存储类应继承此基类以消除重复代码。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

__all__ = ["BaseSqliteStore"]


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
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        # 共享连接模式：外部传入 conn 时复用，本实例不负责关闭
        if conn is not None:
            self._conn = conn
            self._own_conn = False
            self._db_path = None
        else:
            if db_path is None:
                raise ValueError("db_path 和 conn 不能同时为空")
            self._db_path = Path(db_path)
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                timeout=10,
            )
            self._own_conn = True

        # 统一配置 PRAGMA
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

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
    # Schema 版本管理
    # ------------------------------------------------------------------

    def get_schema_version(self, key: str = "schema_version") -> int:
        """获取当前 schema 版本号。"""
        row = self._conn.execute(
            "SELECT value FROM _meta WHERE key = ?", (key,)
        ).fetchone()
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
            "CREATE TABLE IF NOT EXISTS _meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL"
            ")"
        )
        self._conn.commit()
