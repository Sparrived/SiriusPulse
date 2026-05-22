"""Tests for reminder due-check logic in the engine."""

from __future__ import annotations

from datetime import datetime, timezone

from sirius_pulse.core.emotional_engine import _is_reminder_due


def _utc_at_local(hour: int, minute: int, year: int = 2026, month: int = 4, day: int = 27) -> datetime:
    """Return a UTC datetime that corresponds to the given local time."""
    local = datetime(year, month, day, hour, minute, 0).astimezone()
    return local.astimezone(timezone.utc)


def test_once_due():
    now = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    reminder = {"mode": "once", "fire_at": "2026-04-27T11:59:00+00:00"}
    assert _is_reminder_due(reminder, now) is True


def test_once_not_due():
    now = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    reminder = {"mode": "once", "fire_at": "2026-04-27T12:01:00+00:00"}
    assert _is_reminder_due(reminder, now) is False


def test_once_no_fire_at():
    now = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    assert _is_reminder_due({"mode": "once"}, now) is False


def test_daily_due():
    now = _utc_at_local(12, 0)
    reminder = {"mode": "daily", "time": "12:00"}
    assert _is_reminder_due(reminder, now) is True


def test_daily_not_due_different_hour():
    now = _utc_at_local(12, 0)
    reminder = {"mode": "daily", "time": "13:00"}
    assert _is_reminder_due(reminder, now) is False


def test_daily_already_fired_this_minute():
    now = _utc_at_local(12, 0)
    reminder = {"mode": "daily", "time": "12:00", "last_fired_at": now.isoformat()}
    assert _is_reminder_due(reminder, now) is False


def test_daily_already_fired_different_day():
    now = _utc_at_local(12, 0)
    yesterday = _utc_at_local(12, 0, day=26)
    reminder = {"mode": "daily", "time": "12:00", "last_fired_at": yesterday.isoformat()}
    assert _is_reminder_due(reminder, now) is True


def test_weekly_due():
    now = _utc_at_local(12, 0, day=27)  # Monday
    reminder = {"mode": "weekly", "time": "12:00", "weekday": 0}
    assert _is_reminder_due(reminder, now) is True


def test_weekly_wrong_day():
    now = _utc_at_local(12, 0, day=27)  # Monday
    reminder = {"mode": "weekly", "time": "12:00", "weekday": 1}
    assert _is_reminder_due(reminder, now) is False


def test_interval_due():
    now = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    reminder = {"mode": "interval", "minutes_after": 10, "fire_at": "2026-04-27T11:59:00+00:00"}
    assert _is_reminder_due(reminder, now) is True


def test_interval_not_yet():
    now = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    reminder = {"mode": "interval", "minutes_after": 10, "fire_at": "2026-04-27T12:01:00+00:00"}
    assert _is_reminder_due(reminder, now) is False


def test_interval_already_fired_this_minute():
    now = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    reminder = {
        "mode": "interval",
        "minutes_after": 10,
        "fire_at": "2026-04-27T11:59:00+00:00",
        "last_fired_at": "2026-04-27T11:59:30+00:00",
    }
    assert _is_reminder_due(reminder, now) is False


def test_interval_no_fire_at():
    now = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    assert _is_reminder_due({"mode": "interval", "minutes_after": 10}, now) is False


def test_invalid_mode():
    now = _utc_at_local(12, 0)
    assert _is_reminder_due({"mode": "unknown"}, now) is False
