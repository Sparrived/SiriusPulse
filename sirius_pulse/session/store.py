"""
会话持久化存储实现 —— 支持 JSON 文件与 SQLite 两种后端。

公开类:
    - SessionStore: 存储协议（Protocol）
    - JsonSessionStore: JSON 文件后端
    - SqliteSessionStore: SQLite 关系数据库后端
    - SessionStoreFactory: 存储工厂（按后端类型创建对应实例）
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from sirius_pulse.models import Transcript
from sirius_pulse.utils.layout import WorkspaceLayout

__all__ = [
    "SessionStore",
    "JsonSessionStore",
    "SqliteSessionStore",
    "SessionStoreFactory",
]

_SESSION_STORE_SCHEMA_VERSION = 2

_CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS _meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_CREATE_SESSION_META_TABLE = """
CREATE TABLE IF NOT EXISTS session_meta (
    session_id TEXT NOT NULL DEFAULT '',
    session_summary TEXT NOT NULL DEFAULT '',
    orchestration_stats TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id)
)
"""

_CREATE_MESSAGES_TABLE = """
CREATE TABLE IF NOT EXISTS session_messages (
    session_id TEXT NOT NULL DEFAULT '',
    message_index INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    speaker TEXT,
    channel TEXT,
    channel_user_id TEXT,
    multimodal_inputs TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (session_id, message_index)
)
"""

_CREATE_REPLY_RUNTIME_TABLE = """
CREATE TABLE IF NOT EXISTS session_reply_runtime (
    session_id TEXT NOT NULL DEFAULT '',
    id INTEGER NOT NULL DEFAULT 1,
    last_assistant_reply_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, id)
)
"""

_CREATE_REPLY_RUNTIME_USER_TURNS_TABLE = """
CREATE TABLE IF NOT EXISTS session_reply_runtime_user_turns (
    session_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL,
    last_turn_at TEXT NOT NULL,
    PRIMARY KEY (session_id, user_id)
)
"""

_CREATE_REPLY_RUNTIME_GROUP_TURNS_TABLE = """
CREATE TABLE IF NOT EXISTS session_reply_runtime_group_turns (
    session_id TEXT NOT NULL DEFAULT '',
    seq INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
)
"""

_CREATE_REPLY_RUNTIME_ASSISTANT_TURNS_TABLE = """
CREATE TABLE IF NOT EXISTS session_reply_runtime_assistant_turns (
    session_id TEXT NOT NULL DEFAULT '',
    seq INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
)
"""

_CREATE_USER_PROFILES_TABLE = """
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
)
"""

_CREATE_USER_RUNTIME_TABLE = """
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
)
"""

_CREATE_USER_FACTS_TABLE = """
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
)
"""

_CREATE_TOKEN_USAGE_TABLE = """
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
)
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_session_messages_role ON session_messages(role);",
    "CREATE INDEX IF NOT EXISTS idx_session_user_runtime_channel ON session_user_runtime(last_seen_channel);",
    "CREATE INDEX IF NOT EXISTS idx_session_user_memory_facts_user ON session_user_memory_facts(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_session_token_usage_task ON session_token_usage_records(task_name);",
]

_SESSION_DATA_TABLES = [
    "session_user_memory_facts",
    "session_user_runtime",
    "session_user_profiles",
    "session_reply_runtime_user_turns",
    "session_reply_runtime_group_turns",
    "session_reply_runtime_assistant_turns",
    "session_reply_runtime",
    "session_token_usage_records",
    "session_messages",
    "session_meta",
]


class SessionStore(Protocol):
    @property
    def path(self) -> Path:
        ...

    def exists(self) -> bool:
        ...

    def load(self) -> Transcript:
        ...

    def save(self, transcript: Transcript) -> None:
        ...

    def clear(self) -> None:
        ...


class JsonSessionStore:
    def __init__(
        self,
        work_path: str | Path | None = None,
        filename: str = "session_state.json",
        *,
        path: Path | None = None,
    ) -> None:
        self._work_path = Path(work_path) if work_path is not None else None
        if path is not None:
            self._path = Path(path)
        elif work_path is not None:
            self._path = Path(work_path) / filename
        else:
            raise ValueError("JsonSessionStore requires either work_path or path.")

    @classmethod
    def from_layout(cls, layout: WorkspaceLayout, *, session_id: str) -> "JsonSessionStore":
        return cls(path=layout.session_store_path(session_id, backend="json"))

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def load(self) -> Transcript:
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        transcript = Transcript.from_dict(payload)
        # Schema write-back: immediately persist any new default fields so the
        # file stays in sync with the current model definition.
        self.save(transcript)
        return transcript

    def save(self, transcript: Transcript) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


class SqliteSessionStore:
    """SQLite 会话持久化存储。

    注意：此类使用每次操作创建新连接的模式（而非共享连接），
    因此不继承 BaseSqliteStore。这是为了保持其原有的轻量级设计。
    """

    def __init__(
        self,
        work_path: str | Path | None = None,
        filename: str = "session_state.db",
        *,
        path: Path | None = None,
    ) -> None:
        self._work_path = Path(work_path) if work_path is not None else None
        if path is not None:
            self._path = Path(path)
        elif work_path is not None:
            self._path = Path(work_path) / filename
        else:
            raise ValueError("SqliteSessionStore requires either work_path or path.")
        self._conn: sqlite3.Connection | None = None
        self._session_id: str = ""
        self._ensure_schema()

    @classmethod
    def from_layout(cls, layout: WorkspaceLayout, *, session_id: str) -> "SqliteSessionStore":
        return cls(path=layout.session_store_path(session_id, backend="sqlite"))

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _managed_connection(self):
        if self._conn is not None:
            yield self._conn
            self._conn.commit()
        else:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _json_dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _json_loads_dict(
        raw: str | None, default: dict[str, object] | None = None
    ) -> dict[str, object]:
        if not raw:
            return dict(default or {})
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return dict(default or {})
        return dict(data) if isinstance(data, dict) else dict(default or {})

    @staticmethod
    def _json_loads_list(raw: str | None, default: list[object] | None = None) -> list[object]:
        if not raw:
            return list(default or [])
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return list(default or [])
        return list(data) if isinstance(data, list) else list(default or [])

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO _meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    @staticmethod
    def _get_meta(conn: sqlite3.Connection, key: str) -> str:
        row = conn.execute("SELECT value FROM _meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row is not None else ""

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _has_session_data(conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT 1 FROM session_meta WHERE id = 1").fetchone()
        return row is not None

    @staticmethod
    def _delete_session_rows(conn: sqlite3.Connection) -> None:
        for table_name in _SESSION_DATA_TABLES:
            conn.execute(f"DELETE FROM {table_name}")

    def _save_with_connection(self, conn: sqlite3.Connection, transcript: Transcript) -> None:
        self._delete_session_rows(conn)

        conn.execute(
            "INSERT INTO session_meta(session_id, session_summary, orchestration_stats) VALUES(?, ?, ?)",
            (
                self._session_id,
                transcript.session_summary,
                self._json_dumps(transcript.orchestration_stats),
            ),
        )

        conn.executemany(
            """
            INSERT INTO session_messages(
                session_id, message_index, role, content, speaker, channel, channel_user_id,
                multimodal_inputs
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self._session_id,
                    index,
                    message.role,
                    message.content,
                    message.speaker,
                    message.channel,
                    message.channel_user_id,
                    self._json_dumps(message.multimodal_inputs),
                )
                for index, message in enumerate(transcript.messages)
            ],
        )

        conn.execute(
            "INSERT INTO session_reply_runtime(session_id, last_assistant_reply_at) VALUES(?, ?)",
            (self._session_id, transcript.reply_runtime.last_assistant_reply_at),
        )
        conn.executemany(
            "INSERT INTO session_reply_runtime_user_turns(session_id, user_id, last_turn_at) VALUES(?, ?, ?)",
            [
                (self._session_id, uid, ts)
                for uid, ts in transcript.reply_runtime.user_last_turn_at.items()
            ],
        )
        conn.executemany(
            "INSERT INTO session_reply_runtime_group_turns(session_id, seq, timestamp) VALUES(?, ?, ?)",
            [
                (self._session_id, index, timestamp)
                for index, timestamp in enumerate(
                    transcript.reply_runtime.group_recent_turn_timestamps
                )
            ],
        )
        conn.executemany(
            "INSERT INTO session_reply_runtime_assistant_turns(session_id, seq, timestamp) VALUES(?, ?, ?)",
            [
                (self._session_id, index, timestamp)
                for index, timestamp in enumerate(
                    transcript.reply_runtime.assistant_reply_timestamps
                )
            ],
        )

        for group_entries in transcript.user_memory.entries.values():
            for user_id, profile in group_entries.items():
                conn.execute(
                    """
                    INSERT INTO session_user_profiles(
                        session_id, user_id, name, persona, identities, aliases, traits, metadata
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._session_id,
                        user_id,
                        profile.name,
                        profile.persona,
                        self._json_dumps(profile.identities),
                        self._json_dumps([]),
                        self._json_dumps(profile.traits),
                        self._json_dumps(profile.metadata),
                    ),
                )
                # Legacy runtime/facts tables kept for schema compat; insert minimal rows
                conn.execute(
                    """
                    INSERT INTO session_user_runtime(
                        session_id, user_id, inferred_persona, inferred_traits, preference_tags,
                        recent_messages, summary_notes, last_seen_channel, last_seen_uid,
                        observed_keywords, observed_roles, observed_emotions,
                        observed_entities, last_event_processed_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._session_id,
                        user_id,
                        "",
                        "[]",
                        "[]",
                        "[]",
                        "[]",
                        "",
                        "",
                        "[]",
                        "[]",
                        "[]",
                        "[]",
                        None,
                    ),
                )
                conn.execute(
                    "DELETE FROM session_user_memory_facts WHERE session_id = ? AND user_id = ?",
                    (self._session_id, user_id),
                )

        conn.executemany(
            """
            INSERT INTO session_token_usage_records(
                session_id, record_index, actor_id, task_name, model, prompt_tokens,
                completion_tokens, total_tokens, input_chars, output_chars,
                estimation_method, retries_used
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self._session_id,
                    index,
                    record.actor_id,
                    record.task_name,
                    record.model,
                    record.prompt_tokens,
                    record.completion_tokens,
                    record.total_tokens,
                    record.input_chars,
                    record.output_chars,
                    record.estimation_method,
                    record.retries_used,
                )
                for index, record in enumerate(transcript.token_usage_records)
            ],
        )

    def _build_payload_from_connection(self, conn: sqlite3.Connection) -> dict[str, Any]:
        meta_row = conn.execute(
            "SELECT session_summary, orchestration_stats FROM session_meta WHERE session_id = ?",
            (self._session_id,),
        ).fetchone()
        if meta_row is None:
            raise FileNotFoundError(f"session state not found in sqlite store: {self._path}")

        profile_rows = conn.execute(
            "SELECT * FROM session_user_profiles WHERE session_id = ? ORDER BY user_id",
            (self._session_id,),
        ).fetchall()
        fact_rows_by_user: dict[str, list[sqlite3.Row]] = {}
        for row in conn.execute(
            "SELECT * FROM session_user_memory_facts WHERE session_id = ? ORDER BY user_id, fact_index",
            (self._session_id,),
        ).fetchall():
            fact_rows_by_user.setdefault(str(row["user_id"]), []).append(row)

        entries: dict[str, dict[str, Any]] = {}
        for profile_row in profile_rows:
            user_id = str(profile_row["user_id"])
            entries[user_id] = {
                "user_id": user_id,
                "name": str(profile_row["name"]),
                "persona": str(profile_row["persona"]),
                "identities": self._json_loads_dict(str(profile_row["identities"])),
                "aliases": self._json_loads_list(str(profile_row["aliases"])),
                "traits": self._json_loads_list(str(profile_row["traits"])),
                "metadata": self._json_loads_dict(str(profile_row["metadata"])),
            }

        return {
            "messages": [
                {
                    "role": str(row["role"]),
                    "content": str(row["content"]),
                    "speaker": row["speaker"],
                    "channel": row["channel"],
                    "channel_user_id": row["channel_user_id"],
                    "multimodal_inputs": self._json_loads_list(str(row["multimodal_inputs"])),
                }
                for row in conn.execute(
                    "SELECT * FROM session_messages WHERE session_id = ? ORDER BY message_index",
                    (self._session_id,),
                ).fetchall()
            ],
            "user_memory": {"default": entries},
            "reply_runtime": {
                "user_last_turn_at": {
                    str(row["user_id"]): str(row["last_turn_at"])
                    for row in conn.execute(
                        "SELECT * FROM session_reply_runtime_user_turns"
                        " WHERE session_id = ? ORDER BY user_id",
                        (self._session_id,),
                    ).fetchall()
                },
                "group_recent_turn_timestamps": [
                    str(row["timestamp"])
                    for row in conn.execute(
                        "SELECT * FROM session_reply_runtime_group_turns"
                        " WHERE session_id = ? ORDER BY seq",
                        (self._session_id,),
                    ).fetchall()
                ],
                "last_assistant_reply_at": str(
                    conn.execute(
                        "SELECT last_assistant_reply_at FROM session_reply_runtime"
                        " WHERE session_id = ?",
                        (self._session_id,),
                    ).fetchone()[0]
                ),
                "assistant_reply_timestamps": [
                    str(row["timestamp"])
                    for row in conn.execute(
                        "SELECT * FROM session_reply_runtime_assistant_turns"
                        " WHERE session_id = ? ORDER BY seq",
                        (self._session_id,),
                    ).fetchall()
                ],
            },
            "session_summary": str(meta_row["session_summary"]),
            "orchestration_stats": self._json_loads_dict(str(meta_row["orchestration_stats"])),
            "token_usage_records": [
                {
                    "actor_id": str(row["actor_id"]),
                    "task_name": str(row["task_name"]),
                    "model": str(row["model"]),
                    "prompt_tokens": int(row["prompt_tokens"]),
                    "completion_tokens": int(row["completion_tokens"]),
                    "total_tokens": int(row["total_tokens"]),
                    "input_chars": int(row["input_chars"]),
                    "output_chars": int(row["output_chars"]),
                    "estimation_method": str(row["estimation_method"]),
                    "retries_used": int(row["retries_used"]),
                }
                for row in conn.execute(
                    "SELECT * FROM session_token_usage_records"
                    " WHERE session_id = ? ORDER BY record_index",
                    (self._session_id,),
                ).fetchall()
            ],
        }

    def _ensure_schema(self) -> None:
        with self._managed_connection() as conn:
            conn.execute(_CREATE_SESSION_META_TABLE)
            conn.execute(_CREATE_MESSAGES_TABLE)
            conn.execute(_CREATE_REPLY_RUNTIME_TABLE)
            conn.execute(_CREATE_REPLY_RUNTIME_USER_TURNS_TABLE)
            conn.execute(_CREATE_REPLY_RUNTIME_GROUP_TURNS_TABLE)
            conn.execute(_CREATE_REPLY_RUNTIME_ASSISTANT_TURNS_TABLE)
            conn.execute(_CREATE_USER_PROFILES_TABLE)
            conn.execute(_CREATE_USER_RUNTIME_TABLE)
            conn.execute(_CREATE_USER_FACTS_TABLE)
            conn.execute(_CREATE_TOKEN_USAGE_TABLE)
            for index_sql in _CREATE_INDEXES:
                conn.execute(index_sql)
            self._set_meta(conn, "session_store_schema_version", str(_SESSION_STORE_SCHEMA_VERSION))

    def exists(self) -> bool:
        if not self._path.exists():
            return False
        with self._managed_connection() as conn:
            return self._has_session_data(conn)

    def load(self) -> Transcript:
        with self._managed_connection() as conn:
            payload = self._build_payload_from_connection(conn)
        transcript = Transcript.from_dict(payload)
        # Schema write-back: immediately persist any new default fields.
        self.save(transcript)
        return transcript

    def save(self, transcript: Transcript) -> None:
        with self._managed_connection() as conn:
            self._save_with_connection(conn, transcript)

    def clear(self) -> None:
        """Remove the saved session state (deletes rows; keeps schema intact)."""
        if not self._path.exists():
            return
        with self._managed_connection() as conn:
            self._delete_session_rows(conn)


class SessionStoreFactory:
    """Create session stores for a workspace/session namespace."""

    def __init__(
        self,
        *,
        backend: str = "sqlite",
        fixed_store: SessionStore | None = None,
    ) -> None:
        self._backend = backend.strip().lower() or "sqlite"
        self._fixed_store = fixed_store

    @property
    def fixed_store(self) -> SessionStore | None:
        return self._fixed_store

    def create(self, *, layout: WorkspaceLayout, session_id: str) -> SessionStore:
        if self._fixed_store is not None:
            return self._fixed_store
        if self._backend == "json":
            return JsonSessionStore.from_layout(layout, session_id=session_id)
        return SqliteSessionStore.from_layout(layout, session_id=session_id)
