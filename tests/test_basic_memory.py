"""基础记忆在真实群聊中的业务行为测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sirius_pulse.memory.basic import BasicMemoryManager


def _old_timestamp(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_group_context_when_messages_exceed_window_then_keeps_recent_dialogue():
    mgr = BasicMemoryManager(context_window=3)

    for index in range(5):
        mgr.add_entry("group_a", "alice", "user", f"message-{index}", speaker_name="Alice")

    context = mgr.get_context("group_a")

    assert [entry.content for entry in context] == ["message-2", "message-3", "message-4"]
    assert all(entry.group_id == "group_a" for entry in context)


def test_diary_candidates_when_dialogue_outgrows_context_then_returns_older_turns_only():
    mgr = BasicMemoryManager(context_window=2)

    for index in range(5):
        mgr.add_entry("group_a", "alice", "user", f"turn-{index}", speaker_name="Alice")

    candidates = mgr.get_archive_candidates("group_a")

    assert [entry.content for entry in candidates] == ["turn-0", "turn-1", "turn-2"]


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
        timestamp=_old_timestamp(minutes_ago=40),
    )

    heat, seconds_since_last = mgr.get_cold_params("group_a")

    assert heat < 0.35
    assert seconds_since_last >= 30 * 60


def test_clear_group_when_admin_resets_session_then_only_target_group_is_removed():
    mgr = BasicMemoryManager()
    mgr.add_entry("group_a", "alice", "user", "需要清空")
    mgr.add_entry("group_b", "bob", "user", "需要保留")

    mgr.clear_group("group_a")

    assert mgr.get_context("group_a") == []
    assert [entry.content for entry in mgr.get_context("group_b")] == ["需要保留"]
