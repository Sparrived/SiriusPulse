"""会话持久化管理模块。

提供对话记录的序列化与反序列化能力，支持 JSON 文件与 SQLite 两种后端。
公开 API 统一收敛于此，外部模块应通过 `sirius_pulse.session` 导入。

使用示例::

    from sirius_pulse.session import JsonSessionStore, SqliteSessionStore, SessionStoreFactory
"""

from __future__ import annotations

from sirius_pulse.session.store import (
    JsonSessionStore,
    SessionStore,
    SessionStoreFactory,
    SqliteSessionStore,
)

__all__ = [
    "JsonSessionStore",
    "SqliteSessionStore",
    "SessionStore",
    "SessionStoreFactory",
]
