"""SQLite-based persistent storage for cognition analysis events.

Tracks emotional and intent state over time for group atmosphere monitoring.
Also persists decision events (strategy, threshold, reason) for WebUI analysis.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from sirius_pulse.utils.sqlite_base import BaseSqliteStore

logger = logging.getLogger(__name__)

__all__ = ["CognitionEventStore"]

_SCHEMA_VERSION = 3

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS cognition_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         REAL    NOT NULL,
    group_id          TEXT    NOT NULL DEFAULT '',
    user_id           TEXT    NOT NULL DEFAULT '',
    valence           REAL    NOT NULL DEFAULT 0,
    arousal           REAL    NOT NULL DEFAULT 0.3,
    basic_emotion     TEXT    NOT NULL DEFAULT '',
    intensity         REAL    NOT NULL DEFAULT 0.5,
    social_intent     TEXT    NOT NULL DEFAULT '',
    urgency_score     REAL    NOT NULL DEFAULT 0,
    relevance_score   REAL    NOT NULL DEFAULT 0.5,
    confidence        REAL    NOT NULL DEFAULT 0.8,
    directed_score    REAL    NOT NULL DEFAULT 0,
    sarcasm_score     REAL    NOT NULL DEFAULT 0,
    entitlement_score REAL    NOT NULL DEFAULT 0,
    turn_gap_readiness REAL   NOT NULL DEFAULT 0.5,
    directed_signals  TEXT    NOT NULL DEFAULT '{}'
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ce_ts ON cognition_events(timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_ce_group ON cognition_events(group_id);",
    "CREATE INDEX IF NOT EXISTS idx_ce_user ON cognition_events(user_id);",
]

# ── Schema v3: decision_events ────────────────────────────────────

_CREATE_DECISION_TABLE = """\
CREATE TABLE IF NOT EXISTS decision_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL    NOT NULL,
    group_id        TEXT    NOT NULL DEFAULT '',
    user_id         TEXT    NOT NULL DEFAULT '',
    strategy        TEXT    NOT NULL DEFAULT 'silent',
    score           REAL    NOT NULL DEFAULT 0,
    threshold       REAL    NOT NULL DEFAULT 0.5,
    reason          TEXT    NOT NULL DEFAULT '',
    directed_score  REAL    NOT NULL DEFAULT 0,
    urgency         REAL    NOT NULL DEFAULT 0,
    entitlement     REAL    NOT NULL DEFAULT 0,
    sarcasm         REAL    NOT NULL DEFAULT 0,
    heat_level      TEXT    NOT NULL DEFAULT 'warm',
    msg_rate        REAL    NOT NULL DEFAULT 0,
    cooldown        REAL    NOT NULL DEFAULT 0,
    since_reply     REAL    NOT NULL DEFAULT 0,
    expressiveness  REAL    NOT NULL DEFAULT 0.5,
    sensitivity     REAL    NOT NULL DEFAULT 0.5,
    affinity        REAL    NOT NULL DEFAULT 0
);
"""

_CREATE_DECISION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_de_ts ON decision_events(timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_de_group ON decision_events(group_id);",
    "CREATE INDEX IF NOT EXISTS idx_de_strategy ON decision_events(strategy);",
]

# Columns added in schema v2
_V2_COLUMNS = {
    "directed_score": "REAL NOT NULL DEFAULT 0",
    "sarcasm_score": "REAL NOT NULL DEFAULT 0",
    "entitlement_score": "REAL NOT NULL DEFAULT 0",
    "turn_gap_readiness": "REAL NOT NULL DEFAULT 0.5",
    "directed_signals": "TEXT NOT NULL DEFAULT '{}'",
}


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check whether a column already exists in a table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


class CognitionEventStore(BaseSqliteStore):
    """Append-only SQLite store for cognition analysis events.

    继承自 BaseSqliteStore，复用连接管理和基础操作。
    """

    def _create_tables(self) -> None:
        """创建表结构并执行 schema 迁移。"""
        self.execute(_CREATE_TABLE)
        for idx_sql in _CREATE_INDEXES:
            self.execute(idx_sql)

        # Migrate v1 -> v2
        for col, dtype in _V2_COLUMNS.items():
            if not _column_exists(self._conn, "cognition_events", col):
                self.execute(f"ALTER TABLE cognition_events ADD COLUMN {col} {dtype}")

        # Migrate v2 -> v3: decision_events table
        self.execute(_CREATE_DECISION_TABLE)
        for idx_sql in _CREATE_DECISION_INDEXES:
            self.execute(idx_sql)

        self.set_schema_version(_SCHEMA_VERSION, "cognition_schema_version")

    def add(
        self,
        *,
        group_id: str = "",
        user_id: str = "",
        valence: float = 0.0,
        arousal: float = 0.3,
        basic_emotion: str = "",
        intensity: float = 0.5,
        social_intent: str = "",
        urgency_score: float = 0.0,
        relevance_score: float = 0.5,
        confidence: float = 0.8,
        directed_score: float = 0.0,
        sarcasm_score: float = 0.0,
        entitlement_score: float = 0.0,
        turn_gap_readiness: float = 0.5,
        directed_signals: dict[str, Any] | None = None,
        timestamp: float | None = None,
    ) -> None:
        """Persist a single cognition event."""
        ts = timestamp if timestamp is not None else time.time()
        signals_json = json.dumps(directed_signals or {}, ensure_ascii=False)
        self.execute(
            """INSERT INTO cognition_events
               (timestamp, group_id, user_id, valence, arousal, basic_emotion,
                intensity, social_intent, urgency_score, relevance_score, confidence,
                directed_score, sarcasm_score, entitlement_score, turn_gap_readiness,
                directed_signals)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ts, group_id, user_id, valence, arousal, basic_emotion,
                intensity, social_intent, urgency_score, relevance_score, confidence,
                directed_score, sarcasm_score, entitlement_score, turn_gap_readiness,
                signals_json,
            ),
        )
        self.commit()

    def get_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent cognition events ordered by timestamp desc."""
        rows = self.execute(
            """SELECT * FROM cognition_events
            ORDER BY timestamp DESC
            LIMIT ?""",
            (limit,),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def get_group_timeline(self, group_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Return cognition events for a specific group."""
        rows = self.execute(
            """SELECT * FROM cognition_events
            WHERE group_id = ?
            ORDER BY timestamp DESC
            LIMIT ?""",
            (group_id, limit),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def get_emotion_distribution(self, group_id: str | None = None) -> dict[str, int]:
        """Return count of each basic_emotion label."""
        where = "WHERE group_id = ?" if group_id else ""
        params = (group_id,) if group_id else ()
        rows = self.execute(
            f"""SELECT basic_emotion, COUNT(*) as cnt
            FROM cognition_events
            {where}
            GROUP BY basic_emotion
            ORDER BY cnt DESC""",
            params,
        ).fetchall()
        return {row[0] or "unknown": row[1] for row in rows}

    # ── Decision events ────────────────────────────────────────────

    def add_decision(
        self,
        *,
        group_id: str = "",
        user_id: str = "",
        strategy: str = "silent",
        score: float = 0.0,
        threshold: float = 0.5,
        reason: str = "",
        directed_score: float = 0.0,
        urgency: float = 0.0,
        entitlement: float = 0.0,
        sarcasm: float = 0.0,
        heat_level: str = "warm",
        msg_rate: float = 0.0,
        cooldown: float = 0.0,
        since_reply: float = 0.0,
        expressiveness: float = 0.5,
        sensitivity: float = 0.5,
        affinity: float = 0.0,
        timestamp: float | None = None,
    ) -> None:
        """Persist a single decision event."""
        ts = timestamp if timestamp is not None else time.time()
        self.execute(
            """INSERT INTO decision_events
               (timestamp, group_id, user_id, strategy, score, threshold, reason,
                directed_score, urgency, entitlement, sarcasm,
                heat_level, msg_rate, cooldown, since_reply,
                expressiveness, sensitivity, affinity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ts, group_id, user_id, strategy, score, threshold, reason,
                directed_score, urgency, entitlement, sarcasm,
                heat_level, msg_rate, cooldown, since_reply,
                expressiveness, sensitivity, affinity,
            ),
        )
        self.commit()

    def get_decision_events(
        self, group_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return recent decision events, optionally filtered by group."""
        if group_id:
            rows = self.execute(
                """SELECT * FROM decision_events
                WHERE group_id = ?
                ORDER BY timestamp DESC LIMIT ?""",
                (group_id, limit),
            ).fetchall()
        else:
            rows = self.execute(
                """SELECT * FROM decision_events
                ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ── Aggregation queries for WebUI analysis ─────────────────────

    def get_intent_distribution(self, group_id: str | None = None) -> dict[str, int]:
        """Return count of each social_intent label."""
        where = "WHERE group_id = ?" if group_id else ""
        params = (group_id,) if group_id else ()
        rows = self.execute(
            f"""SELECT social_intent, COUNT(*) as cnt
            FROM cognition_events
            {where}
            GROUP BY social_intent
            ORDER BY cnt DESC""",
            params,
        ).fetchall()
        return {row[0] or "unknown": row[1] for row in rows}

    def get_user_stats(self, group_id: str | None = None) -> list[dict[str, Any]]:
        """Return per-user aggregated cognition stats."""
        where = "WHERE group_id = ? AND user_id != ''" if group_id else "WHERE user_id != ''"
        params = (group_id,) if group_id else ()
        rows = self.execute(
            f"""SELECT
                user_id,
                COUNT(*) as event_count,
                ROUND(AVG(valence), 4) as avg_valence,
                ROUND(AVG(arousal), 4) as avg_arousal,
                ROUND(AVG(intensity), 4) as avg_intensity,
                ROUND(AVG(directed_score), 4) as avg_directed,
                ROUND(AVG(sarcasm_score), 4) as avg_sarcasm,
                ROUND(AVG(urgency_score), 4) as avg_urgency,
                ROUND(AVG(relevance_score), 4) as avg_relevance,
                MAX(timestamp) as last_active
            FROM cognition_events
            {where}
            GROUP BY user_id
            ORDER BY event_count DESC""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_group_summary(self) -> list[dict[str, Any]]:
        """Return per-group aggregated cognition summary."""
        rows = self.execute(
            """SELECT
                group_id,
                COUNT(*) as event_count,
                COUNT(DISTINCT user_id) as unique_users,
                ROUND(AVG(valence), 4) as avg_valence,
                ROUND(AVG(arousal), 4) as avg_arousal,
                MAX(timestamp) as last_event
            FROM cognition_events
            WHERE group_id != ''
            GROUP BY group_id
            ORDER BY event_count DESC""",
        ).fetchall()
        return [dict(row) for row in rows]

    def get_hourly_distribution(self, group_id: str | None = None) -> dict[int, int]:
        """Return event count by hour of day (0-23, server timezone)."""
        where = "WHERE group_id = ?" if group_id else ""
        params = (group_id,) if group_id else ()
        rows = self.execute(
            f"""SELECT CAST(strftime('%H', timestamp, 'unixepoch', 'localtime') AS INTEGER) as hour,
                COUNT(*) as cnt
            FROM cognition_events
            {where}
            GROUP BY hour
            ORDER BY hour""",
            params,
        ).fetchall()
        # 保证 0-23 全覆盖
        result: dict[int, int] = {h: 0 for h in range(24)}
        for row in rows:
            result[int(row[0])] = int(row[1])
        return result

    def get_score_distributions(self, group_id: str | None = None) -> dict[str, list[float]]:
        """Return raw score arrays for directed/sarcasm/entitlement distributions."""
        where = "WHERE group_id = ?" if group_id else ""
        params = (group_id,) if group_id else ()
        rows = self.execute(
            f"""SELECT directed_score, sarcasm_score, entitlement_score
            FROM cognition_events
            {where}""",
            params,
        ).fetchall()
        return {
            "directed": [float(row[0]) for row in rows],
            "sarcasm": [float(row[1]) for row in rows],
            "entitlement": [float(row[2]) for row in rows],
        }

    def get_strategy_distribution(self, group_id: str | None = None) -> dict[str, int]:
        """Return count of each decision strategy."""
        where = "WHERE group_id = ?" if group_id else ""
        params = (group_id,) if group_id else ()
        rows = self.execute(
            f"""SELECT strategy, COUNT(*) as cnt
            FROM decision_events
            {where}
            GROUP BY strategy
            ORDER BY cnt DESC""",
            params,
        ).fetchall()
        return {row[0] or "unknown": row[1] for row in rows}

    def get_decision_summary(self, group_id: str | None = None) -> dict[str, Any]:
        """Return aggregated decision stats (avg score/threshold, reason distribution)."""
        where = "WHERE group_id = ?" if group_id else ""
        params = (group_id,) if group_id else ()

        # 基本聚合
        row = self.execute(
            f"""SELECT
                COUNT(*) as total,
                ROUND(AVG(score), 4) as avg_score,
                ROUND(AVG(threshold), 4) as avg_threshold,
                ROUND(AVG(msg_rate), 4) as avg_msg_rate
            FROM decision_events
            {where}""",
            params,
        ).fetchone()
        summary: dict[str, Any] = dict(row) if row else {}

        # reason 分布
        reason_rows = self.execute(
            f"""SELECT reason, COUNT(*) as cnt
            FROM decision_events
            {where}
            GROUP BY reason
            ORDER BY cnt DESC
            LIMIT 15""",
            params,
        ).fetchall()
        summary["reason_distribution"] = {r[0] or "unknown": r[1] for r in reason_rows}

        # heat_level 分布
        heat_rows = self.execute(
            f"""SELECT heat_level, COUNT(*) as cnt
            FROM decision_events
            {where}
            GROUP BY heat_level
            ORDER BY cnt DESC""",
            params,
        ).fetchall()
        summary["heat_distribution"] = {r[0] or "unknown": r[1] for r in heat_rows}

        return summary

    def get_decision_timeline(
        self, group_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return recent decisions with strategy, score, threshold for timeline chart."""
        if group_id:
            rows = self.execute(
                """SELECT timestamp, strategy, score, threshold, reason,
                    heat_level, msg_rate, expressiveness, sensitivity
                FROM decision_events
                WHERE group_id = ?
                ORDER BY timestamp DESC LIMIT ?""",
                (group_id, limit),
            ).fetchall()
        else:
            rows = self.execute(
                """SELECT timestamp, strategy, score, threshold, reason,
                    heat_level, msg_rate, expressiveness, sensitivity
                FROM decision_events
                ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d: dict[str, Any] = dict(row)
    raw = d.get("directed_signals", "{}")
    try:
        d["directed_signals"] = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        d["directed_signals"] = {}
    return d
