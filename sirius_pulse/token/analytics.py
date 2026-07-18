"""Multi-dimensional analytics on top of :class:`TokenUsageStore`.

Provides functions that query the SQLite database directly so that
analysis can span multiple sessions without loading all records into
memory.
"""

from __future__ import annotations

from typing import TypedDict

from sirius_pulse.token.token_store import TokenUsageStore

# ------------------------------------------------------------------
# Result types
# ------------------------------------------------------------------


class BucketDict(TypedDict):
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    input_chars: int
    output_chars: int
    retries: int
    cache_info_calls: int
    cached_prompt_tokens: int
    uncached_prompt_tokens: int
    cache_creation_prompt_tokens: int


class BaselineDict(TypedDict):
    total_calls: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    avg_tokens_per_call: float
    avg_prompt_tokens_per_call: float
    avg_completion_tokens_per_call: float
    completion_to_prompt_ratio: float
    retry_rate: float
    cache_info_calls: int
    cached_prompt_tokens: int
    uncached_prompt_tokens: int
    cache_creation_prompt_tokens: int
    cache_info_coverage: float
    cache_hit_rate: float


class TimeSliceDict(TypedDict):
    time_bucket: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_prompt_tokens: int
    uncached_prompt_tokens: int


class AnalyticsReport(TypedDict):
    baseline: BaselineDict
    by_session: dict[str, BucketDict]
    by_actor: dict[str, BucketDict]
    by_task: dict[str, BucketDict]
    by_model: dict[str, BucketDict]


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

_BASE_AGG_COLS = (
    "COUNT(*) AS calls, "
    "SUM(prompt_tokens) AS prompt_tokens, "
    "SUM(completion_tokens) AS completion_tokens, "
    "SUM(total_tokens) AS total_tokens, "
    "SUM(input_chars) AS input_chars, "
    "SUM(output_chars) AS output_chars, "
    "SUM(CASE WHEN retries_used > 0 THEN 1 ELSE 0 END) AS retries"
)

_CACHE_AGG_COLS = (
    "SUM(CASE WHEN cache_info_available != 0 THEN 1 ELSE 0 END) AS cache_info_calls, "
    "SUM(CASE WHEN cache_info_available != 0 THEN cached_prompt_tokens ELSE 0 END) "
    "AS cached_prompt_tokens, "
    "SUM(CASE WHEN cache_info_available != 0 THEN uncached_prompt_tokens ELSE 0 END) "
    "AS uncached_prompt_tokens, "
    "SUM(CASE WHEN cache_info_available != 0 THEN cache_creation_prompt_tokens ELSE 0 END) "
    "AS cache_creation_prompt_tokens"
)


def _agg_cols(store: TokenUsageStore) -> str:
    if store.cache_columns_available:
        return f"{_BASE_AGG_COLS}, {_CACHE_AGG_COLS}"
    return (
        f"{_BASE_AGG_COLS}, 0 AS cache_info_calls, 0 AS cached_prompt_tokens, "
        "0 AS uncached_prompt_tokens, 0 AS cache_creation_prompt_tokens"
    )


def _bucket_from_row(row: dict[str, object]) -> BucketDict:
    return BucketDict(
        calls=int(row["calls"]),  # type: ignore[call-overload]
        prompt_tokens=int(row["prompt_tokens"]),  # type: ignore[call-overload]
        completion_tokens=int(row["completion_tokens"]),  # type: ignore[call-overload]
        total_tokens=int(row["total_tokens"]),  # type: ignore[call-overload]
        input_chars=int(row["input_chars"]),  # type: ignore[call-overload]
        output_chars=int(row["output_chars"]),  # type: ignore[call-overload]
        retries=int(row["retries"]),  # type: ignore[call-overload]
        cache_info_calls=int(row["cache_info_calls"]),  # type: ignore[call-overload]
        cached_prompt_tokens=int(row["cached_prompt_tokens"]),  # type: ignore[call-overload]
        uncached_prompt_tokens=int(row["uncached_prompt_tokens"]),  # type: ignore[call-overload]
        cache_creation_prompt_tokens=int(row["cache_creation_prompt_tokens"]),  # type: ignore[call-overload]
    )


def _build_where(
    session_id: str | None,
    actor_id: str | None,
    task_name: str | None,
    model: str | None,
    start_ts: float | None = None,
    end_ts: float | None = None,
) -> tuple[str, list[object]]:
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
    if start_ts is not None:
        clauses.append("timestamp >= ?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("timestamp <= ?")
        params.append(end_ts)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


# ------------------------------------------------------------------
# Public query API
# ------------------------------------------------------------------


def compute_baseline(
    store: TokenUsageStore,
    *,
    session_id: str | None = None,
    actor_id: str | None = None,
    task_name: str | None = None,
    model: str | None = None,
    start_ts: float | None = None,
    end_ts: float | None = None,
) -> BaselineDict:
    """Compute aggregate baseline statistics with optional filters."""
    where, params = _build_where(session_id, actor_id, task_name, model, start_ts, end_ts)
    conn = store.conn
    row = conn.execute(
        f"SELECT {_agg_cols(store)} FROM token_usage{where}",
        params,
    ).fetchone()
    calls = int(row["calls"]) if row["calls"] else 0
    prompt = int(row["prompt_tokens"]) if row["prompt_tokens"] else 0
    comp = int(row["completion_tokens"]) if row["completion_tokens"] else 0
    total = int(row["total_tokens"]) if row["total_tokens"] else 0
    retries = int(row["retries"]) if row["retries"] else 0
    cache_info_calls = int(row["cache_info_calls"]) if row["cache_info_calls"] else 0
    cached = int(row["cached_prompt_tokens"]) if row["cached_prompt_tokens"] else 0
    uncached = int(row["uncached_prompt_tokens"]) if row["uncached_prompt_tokens"] else 0
    cache_creation = (
        int(row["cache_creation_prompt_tokens"])
        if row["cache_creation_prompt_tokens"]
        else 0
    )
    observed_prompt = cached + uncached
    return BaselineDict(
        total_calls=calls,
        total_prompt_tokens=prompt,
        total_completion_tokens=comp,
        total_tokens=total,
        avg_tokens_per_call=total / calls if calls else 0.0,
        avg_prompt_tokens_per_call=prompt / calls if calls else 0.0,
        avg_completion_tokens_per_call=comp / calls if calls else 0.0,
        completion_to_prompt_ratio=comp / prompt if prompt else 0.0,
        retry_rate=retries / calls if calls else 0.0,
        cache_info_calls=cache_info_calls,
        cached_prompt_tokens=cached,
        uncached_prompt_tokens=uncached,
        cache_creation_prompt_tokens=cache_creation,
        cache_info_coverage=cache_info_calls / calls if calls else 0.0,
        cache_hit_rate=cached / observed_prompt if observed_prompt else 0.0,
    )


def group_by_session(
    store: TokenUsageStore,
    *,
    actor_id: str | None = None,
    task_name: str | None = None,
    model: str | None = None,
) -> dict[str, BucketDict]:
    """Aggregate token usage grouped by session."""
    where, params = _build_where(None, actor_id, task_name, model)
    conn = store.conn
    rows = conn.execute(
        f"SELECT session_id, {_agg_cols(store)} FROM token_usage{where} GROUP BY session_id ORDER BY session_id",
        params,
    ).fetchall()
    return {str(r["session_id"]): _bucket_from_row(dict(r)) for r in rows}


def group_by_actor(
    store: TokenUsageStore,
    *,
    session_id: str | None = None,
    task_name: str | None = None,
    model: str | None = None,
) -> dict[str, BucketDict]:
    """Aggregate token usage grouped by actor."""
    where, params = _build_where(session_id, None, task_name, model)
    conn = store.conn
    rows = conn.execute(
        f"SELECT actor_id, {_agg_cols(store)} FROM token_usage{where} GROUP BY actor_id ORDER BY actor_id",
        params,
    ).fetchall()
    return {str(r["actor_id"]): _bucket_from_row(dict(r)) for r in rows}


def group_by_task(
    store: TokenUsageStore,
    *,
    session_id: str | None = None,
    actor_id: str | None = None,
    model: str | None = None,
) -> dict[str, BucketDict]:
    """Aggregate token usage grouped by task."""
    where, params = _build_where(session_id, actor_id, None, model)
    conn = store.conn
    rows = conn.execute(
        f"SELECT task_name, {_agg_cols(store)} FROM token_usage{where} GROUP BY task_name ORDER BY task_name",
        params,
    ).fetchall()
    return {str(r["task_name"]): _bucket_from_row(dict(r)) for r in rows}


def group_by_model(
    store: TokenUsageStore,
    *,
    session_id: str | None = None,
    actor_id: str | None = None,
    task_name: str | None = None,
    start_ts: float | None = None,
    end_ts: float | None = None,
) -> dict[str, BucketDict]:
    """Aggregate token usage grouped by model."""
    where, params = _build_where(session_id, actor_id, task_name, None, start_ts, end_ts)
    conn = store.conn
    rows = conn.execute(
        f"SELECT model, {_agg_cols(store)} FROM token_usage{where} GROUP BY model ORDER BY model",
        params,
    ).fetchall()
    return {str(r["model"]): _bucket_from_row(dict(r)) for r in rows}


def time_series(
    store: TokenUsageStore,
    *,
    bucket_seconds: int = 3600,
    session_id: str | None = None,
    actor_id: str | None = None,
    task_name: str | None = None,
    model: str | None = None,
    start_ts: float | None = None,
    end_ts: float | None = None,
) -> list[TimeSliceDict]:
    """Aggregate token usage into fixed-width time buckets.

    Parameters
    ----------
    bucket_seconds:
        Width in seconds of each time bucket (default 3600 = 1 hour).
    """
    where, params = _build_where(session_id, actor_id, task_name, model, start_ts, end_ts)
    conn = store.conn
    cache_series_cols = (
        "SUM(CASE WHEN cache_info_available != 0 THEN cached_prompt_tokens ELSE 0 END)"
        " AS cached_prompt_tokens, "
        "SUM(CASE WHEN cache_info_available != 0 THEN uncached_prompt_tokens ELSE 0 END)"
        " AS uncached_prompt_tokens"
        if store.cache_columns_available
        else "0 AS cached_prompt_tokens, 0 AS uncached_prompt_tokens"
    )
    rows = conn.execute(
        f"""SELECT
                CAST(timestamp / ? AS INTEGER) * ? AS ts_bucket,
                COUNT(*) AS calls,
                SUM(prompt_tokens)     AS prompt_tokens,
                SUM(completion_tokens) AS completion_tokens,
                SUM(total_tokens)      AS total_tokens,
                {cache_series_cols}
            FROM token_usage{where}
            GROUP BY ts_bucket
            ORDER BY ts_bucket""",
        [bucket_seconds, bucket_seconds, *params],
    ).fetchall()
    from datetime import datetime, timezone

    result: list[TimeSliceDict] = []
    for r in rows:
        dt = datetime.fromtimestamp(float(r["ts_bucket"]), tz=timezone.utc)
        result.append(
            TimeSliceDict(
                time_bucket=dt.isoformat(),
                calls=int(r["calls"]),
                prompt_tokens=int(r["prompt_tokens"]),
                completion_tokens=int(r["completion_tokens"]),
                total_tokens=int(r["total_tokens"]),
                cached_prompt_tokens=int(r["cached_prompt_tokens"] or 0),
                uncached_prompt_tokens=int(r["uncached_prompt_tokens"] or 0),
            )
        )
    return result


def full_report(
    store: TokenUsageStore,
    *,
    session_id: str | None = None,
) -> AnalyticsReport:
    """Produce a comprehensive analytics report.

    When *session_id* is given the report is scoped to that session;
    otherwise it covers all sessions in the database.
    """
    return AnalyticsReport(
        baseline=compute_baseline(store, session_id=session_id),
        by_session=group_by_session(store),
        by_actor=group_by_actor(store, session_id=session_id),
        by_task=group_by_task(store, session_id=session_id),
        by_model=group_by_model(store, session_id=session_id),
    )
