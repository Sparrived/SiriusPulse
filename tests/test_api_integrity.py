"""Tests for public session persistence APIs."""

from __future__ import annotations

import pytest

from sirius_pulse.config import TokenUsageRecord
from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.models import Message, Transcript
from sirius_pulse.session.store import JsonSessionStore, SessionStoreFactory, SqliteSessionStore
from sirius_pulse.utils.layout import WorkspaceLayout


def _transcript() -> Transcript:
    transcript = Transcript(session_summary="summary")
    transcript.add(Message(role="human", content="hello  ", speaker="Alice", channel="qq"))
    transcript.add(Message(role="assistant", content="hi", speaker="Bot"))
    transcript.reply_runtime.user_last_turn_at["u1"] = "2026-01-01T00:00:00+00:00"
    transcript.reply_runtime.group_recent_turn_timestamps.append("2026-01-01T00:00:01+00:00")
    transcript.reply_runtime.assistant_reply_timestamps.append("2026-01-01T00:00:02+00:00")
    transcript.reply_runtime.last_assistant_reply_at = "2026-01-01T00:00:02+00:00"
    transcript.orchestration_stats = {"skills": {"called": 2}}
    transcript.remember_participant(
        participant=UnifiedUser(
            user_id="u1",
            name="Alice",
            aliases=["ally"],
            identities={"qq": "10001"},
            metadata={"role": "developer"},
        ),
        group_id="g1",
    )
    transcript.add_token_usage_record(
        TokenUsageRecord(
            actor_id="bot",
            task_name="response_generate",
            model="test-model",
            prompt_tokens=10,
            completion_tokens=2,
            total_tokens=12,
            input_chars=40,
            output_chars=8,
            retries_used=1,
        )
    )
    return transcript


def test_json_session_store_when_saved_then_loads_transcript_state(tmp_path):
    store = JsonSessionStore(path=tmp_path / "session.json")
    store.save(_transcript())

    loaded = store.load()

    assert store.exists() is True
    assert [message.content for message in loaded.messages] == ["hello", "hi"]
    assert loaded.session_summary == "summary"
    assert loaded.reply_runtime.last_assistant_reply_at == "2026-01-01T00:00:02+00:00"
    assert loaded.find_user_by_channel_uid(channel="qq", uid="10001", group_id="g1").name == "Alice"
    assert loaded.token_usage_records[0].total_tokens == 12


def test_json_session_store_when_cleared_then_file_is_removed(tmp_path):
    store = JsonSessionStore(path=tmp_path / "session.json")
    store.save(_transcript())

    store.clear()

    assert store.exists() is False


def test_sqlite_session_store_when_created_then_empty_store_has_schema_but_no_session(tmp_path):
    store = SqliteSessionStore(path=tmp_path / "session.db")

    assert store.path.exists() is True
    assert store.exists() is False


def test_sqlite_session_store_when_saved_then_loads_transcript_state(tmp_path):
    store = SqliteSessionStore(path=tmp_path / "session.db")
    store.save(_transcript())

    loaded = store.load()

    assert store.exists() is True
    assert [message.content for message in loaded.messages] == ["hello", "hi"]
    assert loaded.session_summary == "summary"
    assert loaded.orchestration_stats == {"skills": {"called": 2}}
    assert loaded.reply_runtime.user_last_turn_at == {"u1": "2026-01-01T00:00:00+00:00"}
    assert loaded.find_user_by_channel_uid(channel="qq", uid="10001", group_id="g1").name == "Alice"
    assert loaded.token_usage_records[0].retries_used == 1


def test_sqlite_session_store_when_cleared_then_schema_remains_but_session_disappears(tmp_path):
    store = SqliteSessionStore(path=tmp_path / "session.db")
    store.save(_transcript())

    store.clear()

    assert store.path.exists() is True
    assert store.exists() is False
    with pytest.raises(FileNotFoundError):
        store.load()


def test_session_store_factory_when_backend_selected_then_uses_layout_paths(tmp_path):
    layout = WorkspaceLayout(tmp_path)

    json_store = SessionStoreFactory(backend="json").create(layout=layout, session_id="group/a")
    sqlite_store = SessionStoreFactory(backend="sqlite").create(layout=layout, session_id="group/a")

    assert isinstance(json_store, JsonSessionStore)
    assert isinstance(sqlite_store, SqliteSessionStore)
    assert json_store.path.name == "session_state.json"
    assert sqlite_store.path.name == "session_state.db"
    assert "group%2Fa" in str(json_store.path)
