"""业务视角：她在等一件要好一会儿才有结果的事，给自己定个时间回来看。

以前只能 bash ``sleep`` 几秒再看，等久了就放下不管。外部可观察的是：到点有没有在原来的
会话里开一轮回合、带没带上她的备注、该等的时候有没有等、取消了还会不会叫。
时间一律由测试固定，不依赖真实时钟。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from sirius_pulse.life.clock import DaySchedule
from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.tools.builtin import background_task, wake_me
from sirius_pulse.tools.data_store import ToolDataStore
from sirius_pulse.tools.models import ToolInvocationContext

LOCAL = timezone(timedelta(hours=8))
AFTERNOON = datetime(2026, 11, 14, 15, 0, tzinfo=LOCAL)
NOTE = "看 rimu 的部署日志：出现 listening on :8000 就算好了，接着跑冒烟测试；还在拉镜像就再等。"


class _Her:
    """引擎这一侧：记下每次被叫醒的回合；busy 时像真实引擎一样回 deferred。"""

    def __init__(self, tmp_path: Path, *, schedule: DaySchedule | None = None) -> None:
        self.stores = {
            "wake_me": ToolDataStore(tmp_path / "tool_data" / "wake_me.json"),
            "image_generate": ToolDataStore(tmp_path / "tool_data" / "image_generate.json"),
            "bash": ToolDataStore(tmp_path / "tool_data" / "bash.json"),
        }
        self.turns: list[dict[str, Any]] = []
        self.busy = False
        self.crash: str = ""
        self.schedule = schedule or DaySchedule.from_strings(wake="07:30", sleep="23:30")

    def get_data_store(self, name: str) -> ToolDataStore:
        return self.stores[name]

    def get_day_schedule(self) -> DaySchedule:
        return self.schedule

    async def handle_background_tool_result(self, **kwargs: Any) -> dict[str, Any]:
        if self.busy:
            return {"status": "deferred"}
        if self.crash == "after_tool":
            kwargs["before_tool"]()
            raise RuntimeError("模型调用失败")
        if self.crash == "before_tool":
            raise RuntimeError("模型调用失败")
        self.turns.append(kwargs)
        return {"status": "completed"}

    @property
    def store(self) -> ToolDataStore:
        return self.stores["wake_me"]


def _her_own_turn(group: str = "g1") -> ToolInvocationContext:
    return ToolInvocationContext(group_id=group, adapter_type="napcat", self_initiated=True)


def _asked_in_chat(group: str = "g1") -> ToolInvocationContext:
    return ToolInvocationContext(
        caller=UnifiedUser(user_id="u1", name="临雀", metadata={"is_developer": False}),
        group_id=group,
        adapter_type="napcat",
    )


def _set(her: _Her, *, who=None, now: datetime = AFTERNOON, **params: Any) -> dict:
    params.setdefault("note", NOTE)
    return wake_me.run(
        data_store=her.store, invocation_context=who or _her_own_turn(), now=now, **params
    )


async def _tick(her: _Her, local: datetime) -> str | None:
    return await wake_me.deliver_due(her, her.store, now=local.astimezone(timezone.utc))


@pytest.mark.asyncio
async def test_she_is_woken_once_in_the_same_chat_with_her_note(tmp_path: Path) -> None:
    her = _Her(tmp_path)

    set_up = _set(her, in_minutes=30)
    early = await _tick(her, AFTERNOON + timedelta(minutes=29))
    on_time = await _tick(her, AFTERNOON + timedelta(minutes=30, seconds=5))
    again = await _tick(her, AFTERNOON + timedelta(hours=2))

    assert set_up["success"] and "15:30" in set_up["text_blocks"][0]
    assert early is None and again is None
    assert on_time == wake_me.DONE
    (turn,) = her.turns
    assert turn["group_id"] == "g1" and turn["adapter_type"] == "napcat"
    assert NOTE in turn["output"] and "15:30" in turn["output"]
    # 她自己做事时定的：叫醒后也不能对外说话。
    assert turn["self_initiated"] is True
    assert "【到点了】" in turn["lead"] and "wake_me" in turn["lead"]


@pytest.mark.asyncio
async def test_a_busy_chat_only_postpones_the_wake_up(tmp_path: Path) -> None:
    her = _Her(tmp_path)
    _set(her, in_minutes=5)
    her.busy = True

    while_busy = await _tick(her, AFTERNOON + timedelta(minutes=6))
    her.busy = False
    after = await _tick(her, AFTERNOON + timedelta(minutes=6, seconds=15))

    assert while_busy == wake_me.PENDING and after == wake_me.DONE
    assert len(her.turns) == 1


@pytest.mark.asyncio
async def test_her_own_wake_up_waits_for_morning_but_one_asked_for_in_chat_does_not(
    tmp_path: Path,
) -> None:
    her = _Her(tmp_path)
    late = AFTERNOON.replace(hour=23, minute=0)
    _set(her, now=late, in_minutes=60, note="她自己的：看夜里跑的批处理")
    _set(her, now=late, in_minutes=60, note="临雀要的：零点看下抢购开没开", who=_asked_in_chat())

    await _tick(her, late + timedelta(hours=1, seconds=5))  # 00:00，她睡着
    await _tick(her, late + timedelta(hours=1, seconds=20))
    at_night = [turn["output"] for turn in her.turns]
    await _tick(her, AFTERNOON.replace(day=15, hour=7, minute=31))  # 起床后

    assert len(at_night) == 1 and "临雀要的" in at_night[0]
    assert "她自己的" in her.turns[-1]["output"]
    assert her.turns[0]["self_initiated"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "error"),
    [
        ({"in_minutes": 0, "at": ""}, "in_minutes"),
        ({"in_minutes": 5, "at": "20:00"}, "只填一个"),
        ({"at": "2026-11-14 15:00:30"}, "至少要等 1 分钟"),
        ({"in_minutes": 60 * 24 * 8}, "7 天以内"),
        ({"in_minutes": 10, "note": ""}, "note 不能为空"),
    ],
)
async def test_unusable_wake_ups_are_refused_with_a_reason(
    tmp_path: Path, params: dict, error: str
) -> None:
    her = _Her(tmp_path)

    refused = _set(her, **params)

    assert refused["success"] is False and error in refused["error"]
    assert wake_me.list_wakes(her.store) == []


@pytest.mark.asyncio
async def test_she_cannot_keep_waking_herself_up_without_end(tmp_path: Path) -> None:
    her = _Her(tmp_path)
    her.store.set("max_per_day", 3)
    her.store.save()

    results = [_set(her, in_minutes=10 + i) for i in range(4)]
    next_day = _set(her, now=AFTERNOON + timedelta(days=1, minutes=1), in_minutes=10)

    assert [r["success"] for r in results] == [True, True, True, False]
    assert "24 小时内" in results[-1]["error"]
    assert next_day["success"] is True


@pytest.mark.asyncio
async def test_a_cancelled_wake_up_never_comes(tmp_path: Path) -> None:
    her = _Her(tmp_path)
    wake_id = _set(her, in_minutes=20)["data"]["wake"]["task_id"]

    listed = await background_task.run(
        "list", engine_context=her, invocation_context=_her_own_turn()
    )
    elsewhere = await background_task.run(
        "list", engine_context=her, invocation_context=_her_own_turn("g2")
    )
    stopped = await background_task.run(
        "stop", task_id=wake_id, engine_context=her, invocation_context=_her_own_turn()
    )
    await _tick(her, AFTERNOON + timedelta(minutes=21))

    assert wake_id in listed["text_blocks"][0] and "唤醒·等待中" in listed["text_blocks"][0]
    assert wake_id not in elsewhere["text_blocks"][0]
    assert stopped["data"]["stopped"] is True
    assert her.turns == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("crash", "expected", "retried"),
    [("before_tool", wake_me.PENDING, True), ("after_tool", wake_me.INTERRUPTED, False)],
    ids=["nothing-done-yet", "already-acted"],
)
async def test_a_failed_turn_is_retried_only_if_she_had_not_done_anything_yet(
    tmp_path: Path, crash: str, expected: str, retried: bool
) -> None:
    her = _Her(tmp_path)
    _set(her, in_minutes=5)
    her.crash = crash

    first = await _tick(her, AFTERNOON + timedelta(minutes=6))
    her.crash = ""
    await _tick(her, AFTERNOON + timedelta(minutes=6, seconds=15))

    assert first == expected
    assert (len(her.turns) == 1) is retried


@pytest.mark.asyncio
async def test_a_wake_up_cut_off_by_a_restart_is_not_run_twice(tmp_path: Path) -> None:
    her = _Her(tmp_path)
    _set(her, in_minutes=5)
    started = asyncio.Event()

    async def hang(**kwargs: Any) -> dict[str, Any]:
        kwargs["before_tool"]()  # 她已经动过一个工具
        started.set()
        await asyncio.sleep(3600)
        return {"status": "completed"}

    her.handle_background_tool_result = hang  # type: ignore[method-assign]
    running = asyncio.create_task(_tick(her, AFTERNOON + timedelta(minutes=6)))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    restarted = _Her(tmp_path)
    wake_me.recover_interrupted(restarted.store)
    await _tick(restarted, AFTERNOON + timedelta(minutes=7))

    assert restarted.turns == []
    assert wake_me.list_wakes(restarted.store)[0]["status"] == wake_me.INTERRUPTED
