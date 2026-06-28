"""基础记忆在真实群聊中的业务行为测试。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sirius_pulse.memory.basic import (
    BasicMemoryEntry,
    BasicMemoryFileStore,
    BasicMemoryManager,
)


def _old_timestamp(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_group_context_when_messages_exceed_window_then_keeps_recent_dialogue():
    mgr = BasicMemoryManager(context_window=3)

    for index in range(5):
        mgr.add_entry(
            "group_a", "alice", "user", f"message-{index}", speaker_name="Alice"
        )

    context = mgr.get_context("group_a")

    assert [entry.content for entry in context] == [
        "message-2",
        "message-3",
        "message-4",
    ]
    assert all(entry.group_id == "group_a" for entry in context)


def test_group_memory_when_messages_exceed_hard_limit_then_discards_old_entries():
    mgr = BasicMemoryManager(hard_limit=4, context_window=3)

    for index in range(7):
        mgr.add_entry("group_a", "alice", "user", f"message-{index}", speaker_name="Alice")

    assert [entry.content for entry in mgr.get_all("group_a")] == [
        "message-3",
        "message-4",
        "message-5",
        "message-6",
    ]
    assert [entry.content for entry in mgr.get_context("group_a")] == [
        "message-4",
        "message-5",
        "message-6",
    ]


def test_diary_candidates_when_dialogue_outgrows_context_then_returns_older_turns_only():
    mgr = BasicMemoryManager(context_window=2)

    for index in range(5):
        mgr.add_entry("group_a", "alice", "user", f"turn-{index}", speaker_name="Alice")

    candidates = mgr.get_archive_candidates("group_a")

    assert [entry.content for entry in candidates] == ["turn-0", "turn-1", "turn-2"]


def test_consolidation_candidates_when_group_is_idle_then_include_active_context():
    mgr = BasicMemoryManager(context_window=2)

    for index in range(5):
        mgr.add_entry("group_a", "alice", "user", f"turn-{index}", speaker_name="Alice")

    hot_candidates = mgr.get_consolidation_candidates("group_a", include_context=False)
    idle_candidates = mgr.get_consolidation_candidates("group_a", include_context=True)

    assert [entry.content for entry in hot_candidates] == ["turn-0", "turn-1", "turn-2"]
    assert [entry.content for entry in idle_candidates] == [
        "turn-0",
        "turn-1",
        "turn-2",
        "turn-3",
        "turn-4",
    ]


def test_group_memory_when_two_groups_are_active_then_dialogues_stay_isolated():
    mgr = BasicMemoryManager()

    mgr.add_entry("group_dev", "alice", "user", "部署好了", speaker_name="Alice")
    mgr.add_entry("group_game", "bob", "user", "晚上开黑", speaker_name="Bob")

    assert [entry.content for entry in mgr.get_context("group_dev")] == ["部署好了"]
    assert [entry.content for entry in mgr.get_context("group_game")] == ["晚上开黑"]
    assert set(mgr.list_groups()) == {"group_dev", "group_game"}


def test_cross_group_lookup_when_same_user_talks_elsewhere_then_returns_recent_external_context():
    mgr = BasicMemoryManager()
    mgr.add_entry("group_current", "u1", "user", "当前群发言", speaker_name="Alice")
    mgr.add_entry("group_other", "u1", "user", "另一个群的近况", speaker_name="Alice")
    mgr.add_entry("group_other", "u2", "user", "旁人的消息", speaker_name="Bob")

    entries = mgr.get_entries_by_user("u1", exclude_group_id="group_current", n=10)

    assert [entry.content for entry in entries] == ["另一个群的近况"]


def test_memory_snapshot_when_engine_restarts_then_restores_dialogue_and_heat_state():
    mgr = BasicMemoryManager(context_window=5)
    mgr.add_entry("group_a", "alice", "user", "第一句", speaker_name="Alice")
    mgr.add_entry("group_a", "bob", "assistant", "第二句", speaker_name="Bob")

    restored = BasicMemoryManager.from_dict(mgr.to_dict())

    context = restored.get_context("group_a")
    assert [entry.content for entry in context] == ["第一句", "第二句"]
    assert restored.get_heat_state("group_a") is not None


def test_memory_snapshot_when_legacy_state_is_large_then_restores_all_by_default():
    payload: dict[str, list[dict[str, str]]] = {"group_a": []}
    for index in range(35):
        payload["group_a"].append(
            {
                "entry_id": f"entry_{index}",
                "group_id": "group_a",
                "user_id": "alice",
                "role": "human",
                "content": f"legacy-{index}",
                "timestamp": f"2026-01-01T00:00:{index:02d}+00:00",
            }
        )

    restored = BasicMemoryManager.from_dict(payload)

    assert [entry.content for entry in restored.get_all("group_a")] == [
        f"legacy-{index}" for index in range(35)
    ]


def test_remove_entries_by_ids_when_units_cover_sources_then_prunes_active_window():
    mgr = BasicMemoryManager()
    first = mgr.add_entry("group_a", "alice", "human", "covered", speaker_name="Alice")
    second = mgr.add_entry("group_a", "bob", "human", "kept", speaker_name="Bob")

    removed = mgr.remove_entries_by_ids("group_a", {first.entry_id})

    assert removed == 1
    assert [entry.content for entry in mgr.get_all("group_a")] == [second.content]


def test_basic_memory_entry_when_intent_scores_are_missing_then_defaults_to_empty_dict():
    entry = BasicMemoryEntry.from_dict(
        {
            "entry_id": "entry_1",
            "group_id": "group_a",
            "user_id": "alice",
            "role": "human",
            "content": "hello",
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
    )

    assert entry.intent_scores == {}


def test_basic_memory_store_when_entry_is_updated_then_archive_keeps_intent_scores(
    tmp_path,
):
    store = BasicMemoryFileStore(tmp_path)
    entry = BasicMemoryEntry(
        entry_id="entry_1",
        group_id="group_a",
        user_id="alice",
        role="human",
        content="hello",
        timestamp="2026-01-01T00:00:00+00:00",
    )
    store.append(entry)

    entry.intent_scores = {"social_intent": "social", "directed_score": 0.75}

    assert store.update_entry(entry) is True

    archive_path = tmp_path / "archive" / "group_a.jsonl"
    payload = json.loads(archive_path.read_text(encoding="utf-8").strip())
    assert payload["intent_scores"] == {
        "social_intent": "social",
        "directed_score": 0.75,
    }


def test_basic_memory_store_when_appends_are_concurrent_then_all_entries_are_archived(
    tmp_path,
):
    store = BasicMemoryFileStore(tmp_path)
    entries = [
        BasicMemoryEntry(
            entry_id=f"entry_{index}",
            group_id="group_a",
            user_id=f"user_{index % 8}",
            role="human",
            content=f"message-{index}",
            timestamp=f"2026-01-01T00:00:{index % 60:02d}+00:00",
        )
        for index in range(200)
    ]

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(store.append, entries))

    archived = store.read_all("group_a")
    assert sorted(entry.entry_id for entry in archived) == sorted(
        entry.entry_id for entry in entries
    )
    assert not list((tmp_path / "archive").glob("*.tmp"))


def test_group_heat_when_recent_people_are_chatting_then_group_is_not_cold():
    mgr = BasicMemoryManager()

    for index in range(5):
        mgr.add_entry("group_a", f"user-{index}", "user", f"recent-{index}")

    heat = mgr.compute_heat("group_a")

    assert 0.0 < heat <= 1.0
    assert mgr.is_cold("group_a") is False


def test_group_cold_signal_when_last_message_is_old_then_diary_can_be_promoted():
    mgr = BasicMemoryManager()
    mgr.add_entry(
        "group_a",
        "alice",
        "user",
        "很久前的聊天",
        timestamp=_old_timestamp(minutes_ago=70),
    )

    heat, seconds_since_last = mgr.get_cold_params("group_a")

    assert heat < 0.35
    assert seconds_since_last >= 60 * 60


def test_clear_group_when_admin_resets_session_then_only_target_group_is_removed():
    mgr = BasicMemoryManager()
    mgr.add_entry("group_a", "alice", "user", "需要清空")
    mgr.add_entry("group_b", "bob", "user", "需要保留")

    mgr.clear_group("group_a")

    assert mgr.get_context("group_a") == []
    assert [entry.content for entry in mgr.get_context("group_b")] == ["需要保留"]
