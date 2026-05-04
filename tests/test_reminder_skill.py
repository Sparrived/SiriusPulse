"""Tests for the reminder builtin skill."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from sirius_chat.skills.builtin.reminder import run, _is_valid_hhmm, _weekday_name


class MockDataStore:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def save(self) -> None:
        pass


class MockCaller:
    def __init__(self, user_id: str = "", name: str = "") -> None:
        self.user_id = user_id
        self.name = name


class MockContext:
    def __init__(self, caller: MockCaller | None = None) -> None:
        self.caller = caller


@pytest.fixture
def store():
    return MockDataStore()


def test_create_once_minutes_after(store: MockDataStore):
    ctx = MockContext(MockCaller("u1", "临雀"))
    result = run(
        action="create",
        content="test reminder",
        mode="once",
        minutes_after=5,
        data_store=store,
        invocation_context=ctx,
    )
    assert result["success"] is True
    assert "临雀" in result["text_blocks"][0]
    reminders = store.get("reminders")
    assert len(reminders) == 1
    assert reminders[0]["content"] == "test reminder"
    assert reminders[0]["user_id"] == "u1"
    assert reminders[0]["user_name"] == "临雀"


def test_create_once_trigger_at(store: MockDataStore):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    result = run(
        action="create",
        content="future task",
        mode="once",
        trigger_at=future,
        data_store=store,
    )
    assert result["success"] is True
    reminders = store.get("reminders")
    assert reminders[0]["fire_at"] == future


def test_create_daily(store: MockDataStore):
    result = run(
        action="create",
        content="daily standup",
        mode="daily",
        time="09:00",
        data_store=store,
    )
    assert result["success"] is True
    reminders = store.get("reminders")
    assert reminders[0]["mode"] == "daily"
    assert reminders[0]["time"] == "09:00"


def test_create_weekly(store: MockDataStore):
    result = run(
        action="create",
        content="weekly report",
        mode="weekly",
        time="18:00",
        weekdays=[4],
        data_store=store,
    )
    assert result["success"] is True
    reminders = store.get("reminders")
    assert reminders[0]["mode"] == "weekly"
    assert reminders[0]["weekdays"] == [4]


def test_create_missing_content(store: MockDataStore):
    result = run(action="create", content="", mode="once", minutes_after=5, data_store=store)
    assert result["success"] is False
    assert "提醒内容不能为空" in result["error"]


def test_create_once_no_time(store: MockDataStore):
    result = run(action="create", content="oops", mode="once", data_store=store)
    assert result["success"] is False
    assert "minutes_after" in result["error"]


def test_create_daily_bad_time(store: MockDataStore):
    result = run(action="create", content="bad", mode="daily", time="25:00", data_store=store)
    assert result["success"] is False


def test_create_weekly_bad_weekday(store: MockDataStore):
    result = run(
        action="create", content="bad", mode="weekly", time="09:00", weekday=7, data_store=store
    )
    assert result["success"] is False


def test_list_reminders(store: MockDataStore):
    ctx = MockContext(MockCaller("u1", "临雀"))
    run(action="create", content="r1", mode="once", minutes_after=5, data_store=store, invocation_context=ctx)
    run(action="create", content="r2", mode="daily", time="08:00", data_store=store)
    result = run(action="list", data_store=store)
    assert result["success"] is True
    assert "r1" in result["text_blocks"][0]
    assert "r2" in result["text_blocks"][0]
    assert "临雀" in result["text_blocks"][0]


def test_list_empty(store: MockDataStore):
    result = run(action="list", data_store=store)
    assert result["success"] is True
    assert "没有" in result["text_blocks"][0]


def test_cancel_own_reminder(store: MockDataStore):
    ctx = MockContext(MockCaller("u1", "临雀"))
    create_result = run(
        action="create", content="to cancel", mode="once", minutes_after=5, data_store=store, invocation_context=ctx
    )
    rid = create_result["internal_metadata"]["reminder_id"]
    cancel_result = run(action="cancel", reminder_id=rid, data_store=store, invocation_context=ctx)
    assert cancel_result["success"] is True
    assert store.get("reminders") == []


def test_cancel_others_reminder(store: MockDataStore):
    owner_ctx = MockContext(MockCaller("u1", "临雀"))
    other_ctx = MockContext(MockCaller("u2", "路人"))
    create_result = run(
        action="create", content="mine", mode="once", minutes_after=5, data_store=store, invocation_context=owner_ctx
    )
    rid = create_result["internal_metadata"]["reminder_id"]
    cancel_result = run(action="cancel", reminder_id=rid, data_store=store, invocation_context=other_ctx)
    assert cancel_result["success"] is False
    assert "只有创建者本人可以取消" in cancel_result["error"]


def test_cancel_not_found(store: MockDataStore):
    result = run(action="cancel", reminder_id="nonexistent", data_store=store)
    assert result["success"] is False


def test_invalid_action(store: MockDataStore):
    result = run(action="foo", data_store=store)
    assert result["success"] is False


def test_valid_hhmm():
    assert _is_valid_hhmm("00:00") is True
    assert _is_valid_hhmm("23:59") is True
    assert _is_valid_hhmm("24:00") is False
    assert _is_valid_hhmm("12:60") is False
    assert _is_valid_hhmm("abc") is False


def test_create_with_adapter_type(store: MockDataStore):
    result = run(
        action="create",
        content="adapter reminder",
        mode="once",
        minutes_after=5,
        adapter_type="napcat",
        data_store=store,
    )
    assert result["success"] is True
    reminders = store.get("reminders")
    assert reminders[0]["adapter_type"] == "napcat"


def test_create_default_adapter_type_empty(store: MockDataStore):
    result = run(
        action="create",
        content="default adapter",
        mode="once",
        minutes_after=5,
        data_store=store,
    )
    assert result["success"] is True
    reminders = store.get("reminders")
    assert "adapter_type" not in reminders[0]


def test_create_interval(store: MockDataStore):
    result = run(
        action="create",
        content="喝水提醒",
        mode="interval",
        minutes_after=30,
        data_store=store,
    )
    assert result["success"] is True
    reminders = store.get("reminders")
    assert reminders[0]["mode"] == "interval"
    assert reminders[0]["minutes_after"] == 30
    assert "fire_at" in reminders[0]
    assert "每隔 30 分钟提醒一次" in result["text_blocks"][0]


def test_create_interval_missing_minutes(store: MockDataStore):
    result = run(
        action="create",
        content="喝水提醒",
        mode="interval",
        minutes_after=0,
        data_store=store,
    )
    assert result["success"] is False
    assert "minutes_after" in result["error"]


def test_create_target_self(store: MockDataStore):
    result = run(
        action="create",
        content="去看屏幕右边",
        mode="once",
        minutes_after=5,
        target="self",
        data_store=store,
    )
    assert result["success"] is True
    reminders = store.get("reminders")
    assert reminders[0]["target"] == "self"
    assert "提醒自己" in result["text_blocks"][0]


def test_create_target_default_is_user(store: MockDataStore):
    result = run(
        action="create",
        content="起床",
        mode="once",
        minutes_after=5,
        data_store=store,
    )
    assert result["success"] is True
    reminders = store.get("reminders")
    assert reminders[0]["target"] == "user"
    assert "提醒用户" in result["text_blocks"][0]


def test_create_with_skill_chain(store: MockDataStore):
    result = run(
        action="create",
        content="汇报天气",
        mode="once",
        minutes_after=5,
        skill_chain=[
            {"skill": "weather", "params": {"city": "北京"}}
        ],
        data_store=store,
    )
    assert result["success"] is True
    reminders = store.get("reminders")
    assert reminders[0]["skill_chain"] == [{"skill": "weather", "params": {"city": "北京"}}]


def test_weekday_name():
    assert _weekday_name(0) == "周一"
    assert _weekday_name(6) == "周日"
