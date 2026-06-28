from __future__ import annotations

import json

from sirius_pulse.config import TokenUsageRecord
from sirius_pulse.token.token_store import TokenUsageStore
from sirius_pulse.utils.layout import WorkspaceLayout


def _record(
    *,
    task_name: str = "response_generate",
    model: str = "test-model",
    prompt: int = 10,
    completion: int = 5,
    persona: str = "assistant",
    group: str = "default",
    actor: str = "actor-1",
    breakdown: dict[str, int] | None = None,
    retries_used: int = 0,
    duration_ms: int = 0,
    error_type: str = "",
    conversation_depth: int = 1,
) -> TokenUsageRecord:
    return TokenUsageRecord(
        actor_id=actor,
        task_name=task_name,
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        persona_name=persona,
        group_id=group,
        breakdown_json=json.dumps(breakdown or {}, ensure_ascii=False),
        retries_used=retries_used,
        duration_ms=duration_ms,
        error_type=error_type,
        error_message="failed" if error_type else "",
        conversation_depth=conversation_depth,
    )


def test_token_store_when_created_then_schema_exists_and_count_is_zero(tmp_path):
    store = TokenUsageStore(tmp_path / "token.db")

    assert store.count() == 0
    assert store.get_schema_version("token_schema_version") == 5


def test_token_store_when_batch_size_reached_then_flushes_records(tmp_path):
    store = TokenUsageStore(tmp_path / "token.db", batch_size=2)

    store.add(_record(actor="a1"))

    assert store.count() == 0

    store.add(_record(actor="a2"))

    assert store.count() == 2
    assert store.list_sessions() == ["default"]
    assert [row["actor_id"] for row in store.fetch_records()] == ["a1", "a2"]


def test_token_store_when_filters_are_used_then_returns_matching_rows(tmp_path):
    store = TokenUsageStore(tmp_path / "token.db")
    store.add(_record(persona="alpha", group="g1", actor="u1"), timestamp=10.0)
    store.add(_record(persona="beta", group="g2", actor="u2"), timestamp=11.0)
    store.flush()

    rows = store.fetch_records_filtered(persona_name="alpha", group_id="g1")

    assert len(rows) == 1
    assert rows[0]["actor_id"] == "u1"
    assert [row["name"] for row in store.get_breakdown_by("persona_name")] == ["alpha", "beta"]
    assert [row["name"] for row in store.get_breakdown_by("group_id")] == ["g1", "g2"]


def test_token_store_when_breakdown_is_present_then_aggregates_sections(tmp_path):
    store = TokenUsageStore(tmp_path / "token.db")
    store.add(_record(breakdown={"memory": 4, "history": 6}), timestamp=1.0)
    store.add(
        _record(task_name="cognition_analyze", breakdown={"memory": 3, "skills": 9}), timestamp=2.0
    )
    store.flush()

    assert store.get_section_breakdown() == {"history": 6, "memory": 7, "skills": 9}
    assert store.get_section_breakdown_by_task()["response_generate"] == {"history": 6, "memory": 4}
    assert store.get_recent_records_with_breakdown()[0]["breakdown"] == {"memory": 3, "skills": 9}


def test_token_store_when_records_span_tasks_then_summary_and_breakdowns_are_available(tmp_path):
    store = TokenUsageStore(tmp_path / "token.db")
    store.add(
        _record(
            task_name="response_generate", prompt=12, completion=8, retries_used=1, duration_ms=120
        ),
        timestamp=1.0,
    )
    store.add(
        _record(task_name="cognition_analyze", prompt=7, completion=3, duration_ms=40),
        timestamp=2.0,
    )
    store.flush()

    summary = store.get_summary()
    task_breakdown = store.get_breakdown_by("task_name")
    model_breakdown = store.get_breakdown_by("model")
    retry_stats = store.get_retry_stats()
    duration_stats = store.get_duration_stats()

    assert summary["total_calls"] == 2
    assert summary["total_tokens"] == 30
    assert {row["name"] for row in task_breakdown} == {"response_generate", "cognition_analyze"}
    assert model_breakdown[0]["name"] == "test-model"
    assert retry_stats["total_retries"] == 1
    assert duration_stats["overall"]["avg_ms"] == 80


def test_token_store_when_failures_and_empty_replies_exist_then_stats_are_reported(tmp_path):
    store = TokenUsageStore(tmp_path / "token.db")
    store.add_many(
        [
            _record(
                task_name="response_generate",
                prompt=8,
                completion=0,
                error_type="timeout",
                conversation_depth=3,
            ),
            _record(task_name="cognition_analyze", prompt=8, completion=4),
        ]
    )
    store.flush()

    assert store.get_empty_reply_stats()["empty_calls"] == 1
    assert store.get_failure_stats()["failure_calls"] == 1
    assert store.get_conversation_depth_stats()["max_depth"] == 3


def test_token_store_when_old_records_are_cleaned_then_only_recent_rows_remain(tmp_path):
    store = TokenUsageStore(tmp_path / "token.db")
    store.add(_record(actor="old"), timestamp=1.0)
    store.add(_record(actor="new"), timestamp=10_000_000_000.0)
    store.flush()

    assert store.cleanup_old_records(days=30) == 1
    assert [row["actor_id"] for row in store.get_recent_records()] == ["new"]


def test_token_store_for_workspace_when_layout_is_supplied_then_uses_token_db_path(tmp_path):
    store = TokenUsageStore.for_workspace(WorkspaceLayout(tmp_path), session_id="workspace-session")

    assert store.session_id == "workspace-session"
    assert store.db_path == tmp_path / "token" / "token_usage.db"
