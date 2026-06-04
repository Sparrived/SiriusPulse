"""统一的人格级 SQLite 数据库连接管理。

所有人格共用一个 persona.db 文件，由 PersonaDatabase 统一管理连接。
各存储层（MemoryStorage、TokenUsageStore 等）通过传入共享连接使用同一数据库。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from sirius_pulse.utils.sqlite_base import open_sqlite_connection

logger = logging.getLogger(__name__)

__all__ = ["PersonaDatabase"]


class PersonaDatabase:
    """统一的人格级 SQLite 数据库连接管理。

    每个人格创建一个 PersonaDatabase 实例，所有存储层共享同一连接。
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._conn = open_sqlite_connection(
            self._db_path,
            check_same_thread=False,
            timeout=10,
        )
        self._ensure_meta_table()
        logger.info("PersonaDatabase 已打开: %s", self._db_path)

    def _ensure_meta_table(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS _meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL"
            ")"
        )
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def db_path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        self._conn.close()
        logger.info("PersonaDatabase 已关闭: %s", self._db_path)

    def __enter__(self) -> PersonaDatabase:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
