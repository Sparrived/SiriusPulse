"""Token usage store schema and constants."""
from __future__ import annotations

_SCHEMA_VERSION = 5

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS token_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL,
    timestamp       REAL    NOT NULL,
    actor_id        TEXT    NOT NULL,
    task_name       TEXT    NOT NULL,
    model           TEXT    NOT NULL DEFAULT '',
    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    input_chars     INTEGER NOT NULL DEFAULT 0,
    output_chars    INTEGER NOT NULL DEFAULT 0,
    estimation_method TEXT  NOT NULL DEFAULT 'char_div4',
    retries_used    INTEGER NOT NULL DEFAULT 0,
    persona_name    TEXT    NOT NULL DEFAULT '',
    group_id        TEXT    NOT NULL DEFAULT '',
    provider_name   TEXT    NOT NULL DEFAULT '',
    breakdown_json  TEXT    NOT NULL DEFAULT '',
    duration_ms     REAL    NOT NULL DEFAULT 0,
    error_type      TEXT    NOT NULL DEFAULT '',
    error_message   TEXT    NOT NULL DEFAULT '',
    conversation_depth INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tu_session ON token_usage(session_id);",
    "CREATE INDEX IF NOT EXISTS idx_tu_actor   ON token_usage(actor_id);",
    "CREATE INDEX IF NOT EXISTS idx_tu_task    ON token_usage(task_name);",
    "CREATE INDEX IF NOT EXISTS idx_tu_model   ON token_usage(model);",
    "CREATE INDEX IF NOT EXISTS idx_tu_ts      ON token_usage(timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_tu_persona ON token_usage(persona_name);",
    "CREATE INDEX IF NOT EXISTS idx_tu_group   ON token_usage(group_id);",
    "CREATE INDEX IF NOT EXISTS idx_tu_provider ON token_usage(provider_name);",
]

_CREATE_META = """\
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
