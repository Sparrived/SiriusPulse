"""业务视角：有人托她到点提醒一件事——设得上、到点发得出、不该发的不发。

时间一律用 UTC+8 本地时间描述；``now`` 由测试固定，不依赖真实时钟。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.tools.builtin import reminder
from sirius_pulse.tools.data_store import ToolDataStore
from sirius_pulse.tools.models import ToolInvocationContext

LOCAL = timezone(timedelta(hours=8))
# 2026-11-13 是周五。
FRIDAY_10AM = datetime(2026, 11, 13, 10, 0, tzinfo=LOCAL)


class _Chat:
    """记下到点后发到聊天里的消息；send_ok=False 模拟平台没确认送达。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.send_ok = True

    async def dispatch_proactive_message(self, **kwargs: Any) -> bool:
        if self.send_ok:
            self.sent.append(kwargs)
        return self.send_ok


def _store(tmp_path: Path) -> ToolDataStore:
    return ToolDataStore(tmp_path / "tool_data" / "reminder.json")


def _who(user_id: str = "u1", *, group: str = "g1", developer: bool = False):
    return ToolInvocationContext(
        caller=UnifiedUser(
            user_id=user_id, name=f"群友{user_id}", metadata={"is_developer": developer}
        ),
        group_id=group,
        adapter_type="napcat",
    )


async def _set(store: ToolDataStore, now: datetime = FRIDAY_10AM, **params: Any) -> dict:
    params.setdefault("message", "[AT:10001] 该交作业啦，别拖到半夜～")
    return await reminder.run(
        "add",
        data_store=store,
        invocation_context=params.pop("who", _who()),
        now=now,
        **params,
    )


async def _tick(chat: _Chat, store: ToolDataStore, local: datetime) -> int:
    return await reminder.deliver_due(chat, store, now=local.astimezone(timezone.utc))


@pytest.mark.asyncio
async def test_a_one_off_reminder_goes_out_once_at_the_requested_time(tmp_path: Path) -> None:
    store, chat = _store(tmp_path), _Chat()

    added = await _set(store, at="2026-11-13 20:00")
    await _tick(chat, store, FRIDAY_10AM.replace(hour=19, minute=59))
    early = list(chat.sent)
    await _tick(chat, store, FRIDAY_10AM.replace(hour=20, minute=0, second=10))
    await _tick(chat, store, FRIDAY_10AM.replace(hour=21))

    assert added["success"] and "20:00" in added["text_blocks"][0]
    assert early == []
    assert len(chat.sent) == 1
    sent = chat.sent[0]
    assert sent["group_id"] == "g1" and sent["adapter_type"] == "napcat"
    assert sent["text"] == "[AT:10001] 该交作业啦，别拖到半夜～"
    # 发出去的话要进聊天历史，她之后才知道自己提醒过。
    assert sent["record_delivery"] is True
    listed = await reminder.run("list", data_store=store, invocation_context=_who())
    assert listed["data"]["reminders"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "expected_local"),
    [
        ({"at": "20:00"}, datetime(2026, 11, 13, 20, 0, tzinfo=LOCAL)),  # 今天还没到
        ({"at": "09:30"}, datetime(2026, 11, 14, 9, 30, tzinfo=LOCAL)),  # 今天已过，明天
        ({"at": "11-20 08:00"}, datetime(2026, 11, 20, 8, 0, tzinfo=LOCAL)),
        ({"in_minutes": 45}, datetime(2026, 11, 13, 10, 45, tzinfo=LOCAL)),
    ],
)
async def test_she_can_say_when_in_the_ways_people_ask(
    tmp_path: Path, params: dict, expected_local: datetime
) -> None:
    store = _store(tmp_path)

    added = await _set(store, **params)

    due = datetime.fromisoformat(added["data"]["reminder"]["due_at"])
    assert due == expected_local.astimezone(timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "error"),
    [
        ({"at": "2026-11-13 08:00"}, "已经过去"),
        ({"at": "下周找个时间"}, "看不懂"),
        ({}, "at 或 in_minutes"),
        ({"at": "20:00", "message": ""}, "message 不能为空"),
        ({"at": "2028-01-01 08:00"}, "一年以内"),
    ],
)
async def test_unusable_requests_are_refused_with_a_reason(
    tmp_path: Path, params: dict, error: str
) -> None:
    store = _store(tmp_path)

    refused = await _set(store, **params)

    assert refused["success"] is False and error in refused["error"]
    assert reminder._load(store) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repeat", "first", "second"),
    [
        ("daily", datetime(2026, 11, 13, 22, 0), datetime(2026, 11, 14, 22, 0)),
        ("weekdays", datetime(2026, 11, 13, 22, 0), datetime(2026, 11, 16, 22, 0)),  # 周五 → 周一
        ("weekly", datetime(2026, 11, 13, 22, 0), datetime(2026, 11, 20, 22, 0)),
    ],
)
async def test_repeating_reminders_roll_forward_after_each_one(
    tmp_path: Path, repeat: str, first: datetime, second: datetime
) -> None:
    store, chat = _store(tmp_path), _Chat()
    await _set(store, at="22:00", repeat=repeat)

    await _tick(chat, store, first.replace(tzinfo=LOCAL) + timedelta(seconds=5))

    assert len(chat.sent) == 1
    pending = reminder._load(store)[0]
    assert pending["status"] == "pending"
    assert datetime.fromisoformat(pending["due_at"]) == second.replace(tzinfo=LOCAL)


@pytest.mark.asyncio
async def test_a_weekday_reminder_set_for_the_weekend_starts_on_monday(tmp_path: Path) -> None:
    store = _store(tmp_path)

    added = await _set(store, at="2026-11-14 09:00", repeat="weekdays")  # 周六

    due = datetime.fromisoformat(added["data"]["reminder"]["due_at"])
    assert due == datetime(2026, 11, 16, 9, 0, tzinfo=LOCAL)


@pytest.mark.asyncio
async def test_an_unconfirmed_send_is_retried_and_a_long_outage_is_not_made_up_for(
    tmp_path: Path,
) -> None:
    store, chat = _store(tmp_path), _Chat()
    await _set(store, at="20:00")
    await _set(store, at="2026-11-13 12:00", message="中午记得吃饭")

    chat.send_ok = False
    await _tick(chat, store, FRIDAY_10AM.replace(hour=20, minute=1))
    chat.send_ok = True
    await _tick(chat, store, FRIDAY_10AM.replace(hour=20, minute=2))

    # 20:00 那条重试后发出；12:00 那条到 20:02 已经晚了 8 小时，记为错过、不补发。
    assert [s["text"] for s in chat.sent] == ["[AT:10001] 该交作业啦，别拖到半夜～"]
    statuses = {r["message"]: r["status"] for r in reminder._load(store)}
    assert statuses["中午记得吃饭"] == "missed"


@pytest.mark.asyncio
async def test_only_the_person_who_asked_or_a_developer_can_call_it_off(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = (await _set(store, at="20:00"))["data"]["reminder"]["id"]
    second = (await _set(store, at="21:00"))["data"]["reminder"]["id"]

    by_stranger = await reminder.run(
        "cancel", reminder_id=first, data_store=store, invocation_context=_who("u2")
    )
    by_owner = await reminder.run(
        "cancel", reminder_id=first, data_store=store, invocation_context=_who("u1")
    )
    by_developer = await reminder.run(
        "cancel",
        reminder_id=second,
        data_store=store,
        invocation_context=_who("u9", developer=True),
    )

    assert by_stranger["success"] is False and "设这个提醒的人" in by_stranger["error"]
    assert by_owner["success"] and by_developer["success"]
    assert {r["status"] for r in reminder._load(store)} == {"cancelled"}


@pytest.mark.asyncio
async def test_each_chat_only_sees_and_fills_its_own_reminders(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set("max_pending_per_chat", 2)
    store.save()
    await _set(store, at="20:00")
    await _set(store, at="21:00")

    third = await _set(store, at="22:00")
    elsewhere = await _set(store, at="22:00", who=_who("u3", group="g2"))
    listed_here = await reminder.run("list", data_store=store, invocation_context=_who())
    listed_there = await reminder.run(
        "list", data_store=store, invocation_context=_who("u3", group="g2")
    )

    assert third["success"] is False and "2 个" in third["error"]
    assert elsewhere["success"]
    assert len(listed_here["data"]["reminders"]) == 2
    assert len(listed_there["data"]["reminders"]) == 1
