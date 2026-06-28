"""SQLite-based persistent storage for token usage records.

Provides :class:`TokenUsageStore` which writes every
:class:`~sirius_pulse.config.TokenUsageRecord` into a local SQLite database
so that cross-session and multi-dimensional analytics become possible.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from sirius_pulse.config import TokenUsageRecord
from sirius_pulse.token.token_utils import (
    _CREATE_INDEXES,
    _CREATE_META,
    _CREATE_TABLE,
    _META_KEY_PREFIX,
    _SCHEMA_VERSION,
)
from sirius_pulse.utils.layout import WorkspaceLayout
from sirius_pulse.utils.sqlite_base import BaseSqliteStore

logger = logging.getLogger(__name__)

__all__ = ["TokenUsageStore"]


class TokenUsageStore(BaseSqliteStore):
    """Append-only SQLite store for :class:`TokenUsageRecord` instances.

    继承自 BaseSqliteStore，复用连接管理和基础操作。

    批量写入策略：每次 add 将参数暂存到内存缓冲区，
    缓冲区满或显式调用 flush() 时才提交事务，
    减少 SQLite commit 频率，提升高频写入场景的吞吐量。

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  Created automatically if absent.
        当传入 ``conn`` 时可省略。
    session_id:
        Logical session identifier written alongside every record so that
        per-session queries are possible.
    conn:
        可选的共享 SQLite 连接。传入时复用该连接，不再自行管理生命周期。
    batch_size:
        缓冲区满时自动 flush 的阈值，默认 5。
    """

    _DEFAULT_BATCH_SIZE = 5

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        session_id: str = "default",
        conn: sqlite3.Connection | None = None,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        read_only: bool = False,
    ) -> None:
        self._session_id = session_id
        self._batch_size = batch_size
        self._buffer: list[tuple] = []
        super().__init__(db_path, conn=conn, read_only=read_only)

    @classmethod
    def for_workspace(
        cls,
        layout: WorkspaceLayout,
        *,
        session_id: str = "default",
    ) -> "TokenUsageStore":
        return cls(layout.token_usage_db_path(), session_id=session_id, conn=None)

    def _create_tables(self) -> None:
        """创建表结构并执行 schema 迁移。"""
        # Create _meta before BaseSqliteStore writes the schema version.
        self.execute(_CREATE_META)
        self.execute(_CREATE_TABLE)
        for idx_sql in _CREATE_INDEXES:
            self.execute(idx_sql)
        # Schema migration: add columns if upgrading from v1
        self._ensure_columns(
            "token_usage",
            {
                "persona_name": "TEXT NOT NULL DEFAULT ''",
                "group_id": "TEXT NOT NULL DEFAULT ''",
                "provider_name": "TEXT NOT NULL DEFAULT ''",
                "breakdown_json": "TEXT NOT NULL DEFAULT ''",
                "duration_ms": "REAL NOT NULL DEFAULT 0",
                "error_type": "TEXT NOT NULL DEFAULT ''",
                "error_message": "TEXT NOT NULL DEFAULT ''",
                "conversation_depth": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        self.set_schema_version(_SCHEMA_VERSION, f"{_META_KEY_PREFIX}schema_version")

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add(self, record: TokenUsageRecord, *, timestamp: float | None = None) -> None:
        """暂存单条记录到缓冲区，满时自动 flush。"""
        ts = timestamp if timestamp is not None else time.time()
        self._buffer.append(
            (
                self._session_id,
                ts,
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
                record.persona_name,
                record.group_id,
                record.provider_name,
                record.breakdown_json,
                record.duration_ms,
                record.error_type,
                record.error_message,
                record.conversation_depth,
            )
        )
        if len(self._buffer) >= self._batch_size:
            self.flush()

    def add_many(self, records: list[TokenUsageRecord], *, timestamp: float | None = None) -> None:
        """暂存多条记录到缓冲区，满时自动 flush。"""
        if not records:
            return
        ts = timestamp if timestamp is not None else time.time()
        for r in records:
            self._buffer.append(
                (
                    self._session_id,
                    ts,
                    r.actor_id,
                    r.task_name,
                    r.model,
                    r.prompt_tokens,
                    r.completion_tokens,
                    r.total_tokens,
                    r.input_chars,
                    r.output_chars,
                    r.estimation_method,
                    r.retries_used,
                    r.persona_name,
                    r.group_id,
                    r.provider_name,
                    r.breakdown_json,
                    r.duration_ms,
                    r.error_type,
                    r.error_message,
                    r.conversation_depth,
                )
            )
        if len(self._buffer) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        """将缓冲区中的所有记录批量写入数据库并提交事务。"""
        if not self._buffer:
            return
        self.executemany(
            """INSERT INTO token_usage
               (session_id, timestamp, actor_id, task_name, model,
                prompt_tokens, completion_tokens, total_tokens,
                input_chars, output_chars, estimation_method, retries_used,
                persona_name, group_id, provider_name, breakdown_json, duration_ms,
                error_type, error_message, conversation_depth)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            self._buffer,
        )
        self._buffer.clear()
        self.commit()

    def cleanup_old_records(self, days: int = 30) -> int:
        """清理超过指定天数的旧记录，返回删除的行数。

        Parameters
        ----------
        days:
            保留最近多少天的数据，默认 30 天。
        """
        self.flush()
        cutoff = time.time() - days * 86400
        cursor = self.execute("DELETE FROM token_usage WHERE timestamp < ?", (cutoff,))
        self.commit()
        removed = cursor.rowcount or 0
        if removed:
            logger.info("清理了 %d 条超过 %d 天的旧 token 使用记录", removed, days)
        return removed

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def count(self, *, session_id: str | None = None) -> int:
        if session_id is not None:
            row = self.execute(
                "SELECT COUNT(*) FROM token_usage WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        else:
            row = self.execute("SELECT COUNT(*) FROM token_usage").fetchone()
        return int(row[0])

    def list_sessions(self) -> list[str]:
        rows = self.execute(
            "SELECT DISTINCT session_id FROM token_usage ORDER BY session_id"
        ).fetchall()
        return [row[0] for row in rows]

    def fetch_records(
        self,
        *,
        session_id: str | None = None,
        actor_id: str | None = None,
        task_name: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, object]]:
        """Return raw rows matching the given filters."""
        clauses: list[str] = []
        params: list[object] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if actor_id is not None:
            clauses.append("actor_id = ?")
            params.append(actor_id)
        if task_name is not None:
            clauses.append("task_name = ?")
            params.append(task_name)
        if model is not None:
            clauses.append("model = ?")
            params.append(model)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.execute(
            f"SELECT * FROM token_usage{where} ORDER BY timestamp",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def fetch_records_filtered(
        self,
        *,
        persona_name: str | None = None,
        group_id: str | None = None,
        start_ts: float | None = None,
        end_ts: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        """Return raw rows with advanced filters and pagination."""
        clauses: list[str] = []
        params: list[object] = []
        if persona_name is not None:
            clauses.append("persona_name = ?")
            params.append(persona_name)
        if group_id is not None:
            clauses.append("group_id = ?")
            params.append(group_id)
        if start_ts is not None:
            clauses.append("timestamp >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("timestamp <= ?")
            params.append(end_ts)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.execute(
            f"""SELECT * FROM token_usage{where}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        return [dict(row) for row in rows]

    def get_section_breakdown(
        self,
        *,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> dict[str, int]:
        """Aggregate per-section token counts from breakdown_json.

        Returns a dict mapping section name to total estimated tokens.
        """
        clauses: list[str] = []
        params: list[object] = []
        if start_ts is not None:
            clauses.append("timestamp >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("timestamp <= ?")
            params.append(end_ts)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.execute(
            f"SELECT breakdown_json FROM token_usage{where}",
            params,
        ).fetchall()

        agg: dict[str, int] = {}
        for row in rows:
            raw = row[0]
            if not raw:
                continue
            try:
                bd = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(bd, dict):
                continue
            for key, val in bd.items():
                if isinstance(val, (int, float)):
                    agg[key] = agg.get(key, 0) + int(val)
        return agg

    def get_section_breakdown_by_task(
        self,
        *,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> dict[str, dict[str, int]]:
        """Aggregate per-section token counts grouped by task_name.

        Returns a dict mapping task_name -> {section: tokens}.
        """
        clauses: list[str] = ["breakdown_json != ''"]
        params: list[object] = []
        if start_ts is not None:
            clauses.append("timestamp >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("timestamp <= ?")
            params.append(end_ts)

        where = " WHERE " + " AND ".join(clauses)
        rows = self.execute(
            f"SELECT task_name, breakdown_json FROM token_usage{where}",
            params,
        ).fetchall()

        agg: dict[str, dict[str, int]] = {}
        for row in rows:
            task_name = row[0] or "unknown"
            raw = row[1]
            if not raw:
                continue
            try:
                bd = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(bd, dict):
                continue
            task_agg = agg.setdefault(task_name, {})
            for key, val in bd.items():
                if isinstance(val, (int, float)):
                    task_agg[key] = task_agg.get(key, 0) + int(val)
        return agg

    def get_recent_records_with_breakdown(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> list[dict[str, object]]:
        """Return recent records with parsed breakdown dict attached."""
        records = self.fetch_records_filtered(
            limit=limit, offset=offset, start_ts=start_ts, end_ts=end_ts
        )
        for rec in records:
            raw = rec.get("breakdown_json", "")
            rec["breakdown"] = {}
            if raw:
                try:
                    rec["breakdown"] = json.loads(str(raw))
                except json.JSONDecodeError:
                    logger.warning("解码 breakdown JSON 失败", exc_info=True)
                    pass
        return records

    # ------------------------------------------------------------------
    # Analytics helpers
    # ------------------------------------------------------------------

    def get_summary(self) -> dict[str, Any]:
        """Return aggregated token usage summary."""
        row = self.execute(
            """SELECT
                COUNT(*) as total_calls,
                COALESCE(SUM(prompt_tokens), 0) as total_prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) as total_completion_tokens,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(input_chars), 0) as total_input_chars,
                COALESCE(SUM(output_chars), 0) as total_output_chars
            FROM token_usage"""
        ).fetchone()
        return dict(row) if row else {}

    def get_breakdown_by(
        self,
        column: str,
        *,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return token usage grouped by a column (e.g. 'task_name', 'model', 'group_id')."""
        if column not in {"task_name", "model", "group_id", "provider_name", "persona_name"}:
            return []
        clauses: list[str] = [f"{column} != ''"]
        params: list[object] = []
        if start_ts is not None:
            clauses.append("timestamp >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("timestamp <= ?")
            params.append(end_ts)
        where = " WHERE " + " AND ".join(clauses)
        rows = self.execute(
            f"""SELECT
                {column} as name,
                COUNT(*) as calls,
                COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                COALESCE(SUM(total_tokens), 0) as total_tokens
            FROM token_usage{where}
            GROUP BY {column}
            ORDER BY total_tokens DESC""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_recent_records(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent token usage records."""
        rows = self.execute(
            """SELECT * FROM token_usage
            ORDER BY timestamp DESC
            LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_hourly_summary(
        self,
        *,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate token usage by hour bucket.

        Returns a list of dicts ordered chronologically, each containing:
        ``hour_ts`` (unix timestamp floored to hour), ``calls``,
        ``prompt_tokens``, ``completion_tokens``, ``total_tokens``.
        """
        clauses: list[str] = []
        params: list[object] = []
        if start_ts is not None:
            clauses.append("timestamp >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("timestamp <= ?")
            params.append(end_ts)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.execute(
            f"""SELECT
                CAST(timestamp / 3600 AS INTEGER) * 3600 as hour_ts,
                COUNT(*) as calls,
                COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                COALESCE(SUM(total_tokens), 0) as total_tokens
            FROM token_usage{where}
            GROUP BY hour_ts
            ORDER BY hour_ts""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_retry_stats(
        self,
        *,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> dict[str, Any]:
        """Return retry rate and total retry count."""
        clauses: list[str] = []
        params: list[object] = []
        if start_ts is not None:
            clauses.append("timestamp >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("timestamp <= ?")
            params.append(end_ts)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        row = self.execute(
            f"""SELECT
                COUNT(*) as total_calls,
                COALESCE(SUM(retries_used), 0) as total_retries,
                CASE WHEN COUNT(*) > 0
                    THEN ROUND(SUM(CASE WHEN retries_used > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2)
                    ELSE 0.0
                END as retry_rate_pct
            FROM token_usage{where}""",
            params,
        ).fetchone()
        return dict(row) if row else {"total_calls": 0, "total_retries": 0, "retry_rate_pct": 0.0}

    def get_duration_stats(
        self,
        *,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> dict[str, Any]:
        """Return average duration per task."""
        clauses: list[str] = ["duration_ms > 0"]
        params: list[object] = []
        if start_ts is not None:
            clauses.append("timestamp >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("timestamp <= ?")
            params.append(end_ts)
        where = " WHERE " + " AND ".join(clauses)
        rows = self.execute(
            f"""SELECT
                task_name,
                COUNT(*) as calls,
                ROUND(AVG(duration_ms), 2) as avg_ms,
                ROUND(MIN(duration_ms), 2) as min_ms,
                ROUND(MAX(duration_ms), 2) as max_ms
            FROM token_usage{where}
            GROUP BY task_name
            ORDER BY avg_ms DESC""",
            params,
        ).fetchall()
        overall = self.execute(
            f"""SELECT
                COUNT(*) as calls,
                ROUND(AVG(duration_ms), 2) as avg_ms,
                ROUND(MIN(duration_ms), 2) as min_ms,
                ROUND(MAX(duration_ms), 2) as max_ms
            FROM token_usage{where}""",
            params,
        ).fetchone()
        return {
            "by_task": [dict(row) for row in rows],
            "overall": dict(overall)
            if overall
            else {"calls": 0, "avg_ms": 0.0, "min_ms": 0.0, "max_ms": 0.0},
        }

    def get_efficiency_stats(
        self,
        *,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> dict[str, Any]:
        """Return chars-per-token efficiency metrics."""
        clauses: list[str] = []
        params: list[object] = []
        if start_ts is not None:
            clauses.append("timestamp >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("timestamp <= ?")
            params.append(end_ts)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        row = self.execute(
            f"""SELECT
                COUNT(*) as calls,
                COALESCE(SUM(input_chars), 0) as total_input_chars,
                COALESCE(SUM(prompt_tokens), 0) as total_prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) as total_completion_tokens,
                CASE WHEN SUM(prompt_tokens) > 0
                    THEN ROUND(SUM(input_chars) * 1.0 / SUM(prompt_tokens), 2)
                    ELSE 0.0
                END as chars_per_token,
                CASE WHEN SUM(completion_tokens) > 0
                    THEN ROUND(SUM(output_chars) * 1.0 / SUM(completion_tokens), 2)
                    ELSE 0.0
                END as output_chars_per_token,
                CASE WHEN SUM(prompt_tokens) > 0
                    THEN ROUND(SUM(completion_tokens) * 1.0 / SUM(prompt_tokens), 2)
                    ELSE 0.0
                END as output_ratio
            FROM token_usage{where}""",
            params,
        ).fetchone()
        return (
            dict(row)
            if row
            else {
                "calls": 0,
                "chars_per_token": 0.0,
                "output_chars_per_token": 0.0,
                "output_ratio": 0.0,
            }
        )

    def get_empty_reply_stats(
        self,
        *,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> dict[str, Any]:
        """Return empty reply rate for response_generate tasks."""
        clauses: list[str] = ["task_name = 'response_generate'"]
        params: list[object] = []
        if start_ts is not None:
            clauses.append("timestamp >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("timestamp <= ?")
            params.append(end_ts)
        where = " WHERE " + " AND ".join(clauses)
        row = self.execute(
            f"""SELECT
                COUNT(*) as total_calls,
                SUM(CASE WHEN completion_tokens = 0 THEN 1 ELSE 0 END) as empty_calls,
                CASE WHEN COUNT(*) > 0
                    THEN ROUND(SUM(CASE WHEN completion_tokens = 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2)
                    ELSE 0.0
                END as empty_rate_pct
            FROM token_usage{where}""",
            params,
        ).fetchone()
        return dict(row) if row else {"total_calls": 0, "empty_calls": 0, "empty_rate_pct": 0.0}

    def get_hourly_distribution(self) -> list[dict[str, Any]]:
        """Return token usage distribution by hour-of-day (0-23)."""
        rows = self.execute(
            """SELECT
                CAST(strftime('%H', datetime(timestamp, 'unixepoch')) AS INTEGER) as hour,
                COUNT(*) as calls,
                COALESCE(SUM(total_tokens), 0) as total_tokens
            FROM token_usage
            GROUP BY hour
            ORDER BY hour"""
        ).fetchall()
        return [dict(row) for row in rows]

    def get_failure_stats(
        self,
        *,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> dict[str, Any]:
        """Return failure rate and breakdown by error_type."""
        clauses: list[str] = []
        params: list[object] = []
        if start_ts is not None:
            clauses.append("timestamp >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("timestamp <= ?")
            params.append(end_ts)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        overall = self.execute(
            f"""SELECT
                COUNT(*) as total_calls,
                SUM(CASE WHEN error_type != '' THEN 1 ELSE 0 END) as failure_calls
            FROM token_usage{where}""",
            params,
        ).fetchone()
        by_type_where = ["error_type != ''"]
        if start_ts is not None:
            by_type_where.append("timestamp >= ?")
        if end_ts is not None:
            by_type_where.append("timestamp <= ?")
        by_type_sql = " WHERE " + " AND ".join(by_type_where)
        by_type = self.execute(
            f"""SELECT error_type as name, COUNT(*) as calls
            FROM token_usage{by_type_sql}
            GROUP BY error_type
            ORDER BY calls DESC""",
            params,
        ).fetchall()
        return {
            "total_calls": overall[0] if overall else 0,
            "failure_calls": overall[1] if overall else 0,
            "failure_rate_pct": round(overall[1] * 100.0 / overall[0], 2)
            if overall and overall[0]
            else 0.0,
            "by_type": [dict(row) for row in by_type],
        }

    def get_conversation_depth_stats(
        self,
        *,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> dict[str, Any]:
        """Return average and max conversation depth for response_generate."""
        clauses: list[str] = ["task_name = 'response_generate'", "conversation_depth > 0"]
        params: list[object] = []
        if start_ts is not None:
            clauses.append("timestamp >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("timestamp <= ?")
            params.append(end_ts)
        where = " WHERE " + " AND ".join(clauses)
        row = self.execute(
            f"""SELECT
                COUNT(*) as calls,
                ROUND(AVG(conversation_depth), 2) as avg_depth,
                MAX(conversation_depth) as max_depth
            FROM token_usage{where}""",
            params,
        ).fetchone()
        return dict(row) if row else {"calls": 0, "avg_depth": 0.0, "max_depth": 0}

    def get_period_comparison(
        self,
        *,
        current_seconds: float = 86400,
        previous_seconds: float = 86400,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> dict[str, Any]:
        """Compare token usage between current and previous period.

        Returns percentage change for key metrics.
        """
        now = time.time()
        # When explicit range is provided, compare that range vs previous equal-length range
        if start_ts is not None and end_ts is not None:
            current_start = start_ts
            current_end = end_ts
            period_len = current_end - current_start
            previous_start = current_start - period_len
            previous_end = current_start
        else:
            current_start = now - current_seconds
            current_end = now
            previous_start = now - current_seconds - previous_seconds
            previous_end = now - current_seconds

        def _agg(start: float, end: float) -> dict[str, Any]:
            row = self.execute(
                """SELECT
                    COUNT(*) as calls,
                    COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                    COALESCE(SUM(total_tokens), 0) as total_tokens
                FROM token_usage
                WHERE timestamp >= ? AND timestamp <= ?""",
                (start, end),
            ).fetchone()
            return (
                dict(row)
                if row
                else {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            )

        current = _agg(current_start, current_end)
        previous = _agg(previous_start, previous_end)

        def _pct(curr: int, prev: int) -> float:
            if prev == 0:
                return 100.0 if curr > 0 else 0.0
            return round((curr - prev) * 100.0 / prev, 1)

        return {
            "current": current,
            "previous": previous,
            "change_calls": _pct(current["calls"], previous["calls"]),
            "change_total_tokens": _pct(current["total_tokens"], previous["total_tokens"]),
            "change_prompt_tokens": _pct(current["prompt_tokens"], previous["prompt_tokens"]),
            "change_completion_tokens": _pct(
                current["completion_tokens"], previous["completion_tokens"]
            ),
        }

    @property
    def session_id(self) -> str:
        return self._session_id
