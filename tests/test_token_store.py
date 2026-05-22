"""Tests for TokenUsageStore (SQLite) and analytics module."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from sirius_pulse.config import TokenUsageRecord
from sirius_pulse.token.store import TokenUsageStore
from sirius_pulse.token.analytics import (
    compute_baseline,
    full_report,
    group_by_actor,
    group_by_model,
    group_by_session,
    group_by_task,
    time_series,
)


def _make_record(
    *,
    actor_id: str = "actor_a",
    task_name: str = "chat_main",
    model: str = "gpt-4",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    retries_used: int = 0,
) -> TokenUsageRecord:
    return TokenUsageRecord(
        actor_id=actor_id,
        task_name=task_name,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        input_chars=prompt_tokens * 4,
        output_chars=completion_tokens * 4,
        retries_used=retries_used,
    )


@pytest.fixture()
def store(tmp_path: Path) -> TokenUsageStore:
    return TokenUsageStore(tmp_path / "test.db", session_id="sess_1")


# ── Store basic CRUD ─────────────────────────────────────────────

class TestTokenUsageStore:

    def test_add_and_count(self, store: TokenUsageStore) -> None:
        assert store.count() == 0
        store.add(_make_record())
        assert store.count() == 1

    def test_add_many(self, store: TokenUsageStore) -> None:
        records = [_make_record(actor_id=f"u{i}") for i in range(5)]
        store.add_many(records)
        assert store.count() == 5

    def test_add_many_empty(self, store: TokenUsageStore) -> None:
        store.add_many([])
        assert store.count() == 0

    def test_list_sessions(self, tmp_path: Path) -> None:
        s1 = TokenUsageStore(tmp_path / "multi.db", session_id="alpha")
        s1.add(_make_record())
        s1.close()
        s2 = TokenUsageStore(tmp_path / "multi.db", session_id="beta")
        s2.add(_make_record())
        sessions = s2.list_sessions()
        assert sessions == ["alpha", "beta"]
        s2.close()

    def test_count_by_session(self, tmp_path: Path) -> None:
        s = TokenUsageStore(tmp_path / "cnt.db", session_id="s1")
        s.add(_make_record())
        s.close()
        s = TokenUsageStore(tmp_path / "cnt.db", session_id="s2")
        s.add(_make_record())
        s.add(_make_record())
        assert s.count(session_id="s1") == 1
        assert s.count(session_id="s2") == 2
        assert s.count() == 3
        s.close()

    def test_fetch_records_filters(self, store: TokenUsageStore) -> None:
        store.add(_make_record(actor_id="alice", task_name="chat_main"))
        store.add(_make_record(actor_id="bob", task_name="memory_extract"))
        rows = store.fetch_records(actor_id="alice")
        assert len(rows) == 1
        assert rows[0]["actor_id"] == "alice"
        rows = store.fetch_records(task_name="memory_extract")
        assert len(rows) == 1
        assert rows[0]["actor_id"] == "bob"

    def test_close_and_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "reopen.db"
        s = TokenUsageStore(db, session_id="x")
        s.add(_make_record())
        s.close()
        s2 = TokenUsageStore(db, session_id="x")
        assert s2.count() == 1
        s2.close()

    def test_properties(self, store: TokenUsageStore) -> None:
        assert store.session_id == "sess_1"
        assert store.db_path.name == "test.db"


# ── Analytics ─────────────────────────────────────────────────────

def _seed_store(tmp_path: Path) -> TokenUsageStore:
    """Create a store with diverse records for analytics tests."""
    s = TokenUsageStore(tmp_path / "analytics.db", session_id="s1")
    s.add(_make_record(actor_id="alice", task_name="chat_main", model="gpt-4", prompt_tokens=100, completion_tokens=50))
    s.add(_make_record(actor_id="alice", task_name="memory_extract", model="gpt-3.5", prompt_tokens=80, completion_tokens=20, retries_used=1))
    s.add(_make_record(actor_id="bob", task_name="chat_main", model="gpt-4", prompt_tokens=200, completion_tokens=100))
    s.close()
    s = TokenUsageStore(tmp_path / "analytics.db", session_id="s2")
    s.add(_make_record(actor_id="alice", task_name="chat_main", model="gpt-4", prompt_tokens=150, completion_tokens=75))
    return s


class TestAnalytics:

    def test_compute_baseline_global(self, tmp_path: Path) -> None:
        store = _seed_store(tmp_path)
        bl = compute_baseline(store)
        assert bl["total_calls"] == 4
        assert bl["total_prompt_tokens"] == 100 + 80 + 200 + 150
        assert bl["total_completion_tokens"] == 50 + 20 + 100 + 75
        assert bl["retry_rate"] == pytest.approx(1 / 4)
        store.close()

    def test_compute_baseline_filtered(self, tmp_path: Path) -> None:
        store = _seed_store(tmp_path)
        bl = compute_baseline(store, actor_id="bob")
        assert bl["total_calls"] == 1
        assert bl["total_prompt_tokens"] == 200
        store.close()

    def test_compute_baseline_empty(self, store: TokenUsageStore) -> None:
        bl = compute_baseline(store)
        assert bl["total_calls"] == 0
        assert bl["avg_tokens_per_call"] == 0.0

    def test_group_by_session(self, tmp_path: Path) -> None:
        store = _seed_store(tmp_path)
        result = group_by_session(store)
        assert "s1" in result
        assert "s2" in result
        assert result["s1"]["calls"] == 3
        assert result["s2"]["calls"] == 1
        store.close()

    def test_group_by_actor(self, tmp_path: Path) -> None:
        store = _seed_store(tmp_path)
        result = group_by_actor(store)
        assert result["alice"]["calls"] == 3
        assert result["bob"]["calls"] == 1
        store.close()

    def test_group_by_actor_filtered_by_session(self, tmp_path: Path) -> None:
        store = _seed_store(tmp_path)
        result = group_by_actor(store, session_id="s1")
        assert result["alice"]["calls"] == 2
        assert result["bob"]["calls"] == 1
        store.close()

    def test_group_by_task(self, tmp_path: Path) -> None:
        store = _seed_store(tmp_path)
        result = group_by_task(store)
        assert result["chat_main"]["calls"] == 3
        assert result["memory_extract"]["calls"] == 1
        store.close()

    def test_group_by_model(self, tmp_path: Path) -> None:
        store = _seed_store(tmp_path)
        result = group_by_model(store)
        assert result["gpt-4"]["calls"] == 3
        assert result["gpt-3.5"]["calls"] == 1
        store.close()

    def test_time_series(self, tmp_path: Path) -> None:
        s = TokenUsageStore(tmp_path / "ts.db", session_id="t")
        now = time.time()
        s.add(_make_record(), timestamp=now)
        s.add(_make_record(), timestamp=now + 1)
        s.add(_make_record(), timestamp=now + 7200)
        result = time_series(s, bucket_seconds=3600)
        assert len(result) == 2
        assert result[0]["calls"] == 2
        assert result[1]["calls"] == 1
        s.close()

    def test_full_report(self, tmp_path: Path) -> None:
        store = _seed_store(tmp_path)
        report = full_report(store)
        assert report["baseline"]["total_calls"] == 4
        assert "s1" in report["by_session"]
        assert "alice" in report["by_actor"]
        assert "chat_main" in report["by_task"]
        assert "gpt-4" in report["by_model"]
        store.close()

    def test_full_report_scoped_session(self, tmp_path: Path) -> None:
        store = _seed_store(tmp_path)
        report = full_report(store, session_id="s1")
        assert report["baseline"]["total_calls"] == 3
        store.close()

