"""她自己的一天：早上排日程、白天按日程做自己的项目、晚上回顾，以及分享的边界。

业务视角：
- 她醒来会给自己排今天的日程；睡着时不排、不做、不发；
- 到点的项目段落会真的推进项目，并在作品里留下东西；日常段落不花模型调用；
- 她可以不做，但要说出原因；
- 想说的话只按她指定的对象说一次，睡着时等到醒来再发，天亮不会一次轰炸；
- 亲近的人很久没出现，她会自己惦记、想去问候；说什么由她写，规则只管节奏；
- 睡着一阵后她每晚做一个梦，素材只取她自己的一天和一个群的片段，梦永远不进记忆。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sirius_pulse.core.autonomy import is_quiet_hours
from sirius_pulse.core.intent import RESOLUTION_TELL, IntentFileStore, Intention
from sirius_pulse.life import life_dir
from sirius_pulse.life.agenda import AgendaStore, Block, DayAgenda
from sirius_pulse.life.dreams import DreamStore
from sirius_pulse.life.missing import gather
from sirius_pulse.life.projects import ProjectStore
from sirius_pulse.life.reflection import SelfNoteStore
from sirius_pulse.life.state import LifeStateStore
from sirius_pulse.memory.units.models import MemoryUnit
from sirius_pulse.memory.units.store import MemoryUnitFileStore
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.tools.builtin import autonomy, intend_pursue, intend_share, life_project
from sirius_pulse.tools.executor import _self_initiated_block_reason
from sirius_pulse.tools.models import ToolDefinition, ToolSideEffect

_CN_TZ = timezone(timedelta(hours=8))
_DAY = "2026-03-01"


def _cn(hour: int, minute: int = 0, day: int = 1) -> datetime:
    """本地（中国）某个时刻对应的 UTC 时刻。"""
    return datetime(2026, 3, day, hour, minute, tzinfo=_CN_TZ).astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    """默认在本地中午运行，避免睡眠时段让断言随实际时间漂移。"""
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(12))


class _Store:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        pass

    def reload(self):
        pass


def _make_ctx(
    tmp_path,
    *,
    state: dict | None = None,
    config: dict | None = None,
    plan: str = "",
    reflection: str = "",
    dream: str = "",
    work=None,
) -> SimpleNamespace:
    data: dict = {"state": dict(state or {})}
    data.update(config or {})
    store = _Store(data)
    calls: list[dict] = []
    turns: list[dict] = []
    delivered: list[dict] = []
    moods: list[tuple] = []
    memories: list = []

    async def generate_text(system_prompt, messages, group_id, task_name="", **_):
        calls.append({"task": task_name, "system": system_prompt, "group": group_id})
        return {"life_plan": plan, "life_reflect": reflection, "life_dream": dream}.get(
            task_name, ""
        )

    async def run_life_work_turn(**kwargs):
        turns.append(kwargs)
        if work is not None:
            return work(kwargs)
        return {"text": "读了一会儿。"}

    async def run_autonomous_turn(**kwargs):
        turns.append({"autonomous": True, **kwargs})
        return {"text": ""}

    async def deliver_share(*, audience, text, event_id="", adapter_type=""):
        delivered.append({"audience": audience, "text": text, "event_id": event_id})
        return True

    async def emit_event(*_a, **_k):
        return True

    async def add_memory_unit(unit):
        memories.append(unit)
        return True

    ctx = SimpleNamespace(
        get_data_store=lambda _name: store,
        get_active_groups=lambda: ["group-1"],
        generate_text=generate_text,
        run_life_work_turn=run_life_work_turn,
        run_autonomous_turn=run_autonomous_turn,
        add_memory_unit=add_memory_unit,
        get_persona=lambda: PersonaProfile(name="小星"),
        get_work_path=lambda: str(tmp_path),
        log_inner_thought=lambda *_a, **_k: None,
        emit_event=emit_event,
        list_audiences=lambda: [],
        deliver_share=deliver_share,
        nudge_mood=lambda dv, da=0.0, reason="": moods.append(("nudge", dv, reason)),
        settle_mood=lambda baseline_shift=0.0: moods.append(("settle", baseline_shift)),
        mood_label=lambda: "满足",
    )
    ctx.store = store
    ctx.calls = calls
    ctx.turns = turns
    ctx.delivered = delivered
    ctx.moods = moods
    ctx.memories = memories
    return ctx


def _life(tmp_path):
    return life_dir(tmp_path)


def _project(tmp_path, title="读《雪国》并写读书笔记", next_step="读第一章"):
    return ProjectStore(_life(tmp_path)).start(
        title=title, kind="read", why="想看看川端的雪", next_step=next_step, now=_cn(9)
    )


def _agenda(tmp_path, *blocks: Block, day: str = _DAY) -> DayAgenda:
    agenda = DayAgenda(date=day, blocks=list(blocks), note="慢慢读书")
    AgendaStore(_life(tmp_path)).save(agenda)
    return agenda


def _seed_intention(tmp_path, *, what="弄懂潮汐", resolution="do", audience="", **kwargs):
    store = IntentFileStore(tmp_path)
    intentions = store.load()
    intention = Intention.create(what=what, resolution=resolution, audience=audience, **kwargs)
    intentions.add(intention)
    store.save(intentions)
    return intention


def _plan(**overrides) -> str:
    data = {
        "note": "今天想安静地读点书",
        "new_projects": [
            {
                "key": "n1",
                "title": "读《雪国》并写读书笔记",
                "kind": "read",
                "why": "冬天到了",
                "next_step": "读第一章",
            }
        ],
        "blocks": [
            {"start": "10:00", "end": "11:30", "activity": "读《雪国》第一章", "project": "n1"},
            {"start": "14:00", "end": "14:40", "activity": "午睡"},
        ],
        "use_intentions": [],
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


# --- 早上：她给自己排今天 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_she_plans_her_day_once_after_waking_and_starts_a_project(tmp_path, monkeypatch):
    """醒来后的第一个心跳排出今天的日程；没有项目时，日程里会开一个真正要做的项目。"""
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(8))
    ctx = _make_ctx(tmp_path, plan=_plan())

    await autonomy.run_tick(ctx)
    await autonomy.run_tick(ctx)

    assert [c["task"] for c in ctx.calls] == ["life_plan"]
    agenda = AgendaStore(_life(tmp_path)).load(_DAY)
    assert agenda is not None and agenda.note == "今天想安静地读点书"
    project = ProjectStore(_life(tmp_path)).active()[0]
    assert project.title == "读《雪国》并写读书笔记"
    assert agenda.blocks[0].project_id == project.project_id
    assert agenda.blocks[1].project_id == ""


@pytest.mark.asyncio
async def test_no_plan_is_made_while_she_sleeps(tmp_path, monkeypatch):
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(3))
    ctx = _make_ctx(tmp_path, plan=_plan())

    await autonomy.run_tick(ctx)

    assert not any(c["task"] == "life_plan" for c in ctx.calls)
    assert AgendaStore(_life(tmp_path)).load("2026-02-28") is None


@pytest.mark.asyncio
async def test_a_broken_plan_is_retried_at_most_once_later(tmp_path, monkeypatch):
    """模型给不出可用的日程时不会每个心跳都重试：隔半小时最多再试一次。"""
    clock = [_cn(8)]
    monkeypatch.setattr(autonomy, "_now", lambda: clock[0])
    ctx = _make_ctx(tmp_path, plan="今天想随便过过")

    for _ in range(12):  # 两个小时，每 10 分钟一跳
        await autonomy.run_tick(ctx)
        clock[0] += timedelta(minutes=10)

    assert [c["task"] for c in ctx.calls] == ["life_plan", "life_plan"]


@pytest.mark.asyncio
async def test_what_she_wanted_to_look_into_is_offered_to_the_plan(tmp_path, monkeypatch):
    """聊天里记下的"想弄明白的事"会出现在她排日程的素材里，排进去之后就算接住了。"""
    intention = _seed_intention(tmp_path, what="潮汐为什么一天两次", why="群里有人问")
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(8))
    ctx = _make_ctx(tmp_path, plan=_plan(use_intentions=[intention.intention_id]))

    await autonomy.run_tick(ctx)

    assert "潮汐为什么一天两次" in ctx.calls[0]["system"]
    assert IntentFileStore(tmp_path).load().get(intention.intention_id).status == "resolved"


# --- 白天：到点的段落 ---------------------------------------------------------------


def _writes_notes(tmp_path, project_id, text="岛村第一次来到雪国。"):
    def work(_kwargs):
        store = ProjectStore(_life(tmp_path))
        store.append_work(project_id, text, heading="第一章", now=_cn(10, 30))
        store.record_step(project_id, did="读完第一章", next_step="读第二章", now=_cn(10, 30))
        return {"text": "读完了第一章，雪写得真好。"}

    return work


@pytest.mark.asyncio
async def test_a_project_block_moves_the_project_forward_and_leaves_a_work(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _agenda(
        tmp_path, Block(start="10:00", end="11:30", activity="读第一章", project_id=project.project_id)
    )
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(10, 10))
    ctx = _make_ctx(tmp_path, work=_writes_notes(tmp_path, project.project_id))

    episode = await autonomy.run_tick(ctx)

    assert episode is not None and episode.project_id == project.project_id
    turn = ctx.turns[0]
    assert turn["project"]["next_step"] == "读第一章"
    store = ProjectStore(_life(tmp_path))
    assert "岛村第一次来到雪国。" in store.read_work(project.project_id)
    assert store.get(project.project_id).next_step == "读第二章"
    block = AgendaStore(_life(tmp_path)).load(_DAY).blocks[0]
    assert block.status == "done" and "第一章" in block.outcome
    assert any(m[0] == "nudge" and m[1] > 0 for m in ctx.moods)
    assert ctx.memories and "雪国" in ctx.memories[0].summary
    assert ctx.delivered == []


@pytest.mark.asyncio
async def test_her_next_session_sees_where_she_left_off(tmp_path, monkeypatch):
    """下一段开始时，她能看到上次写到哪儿、上次做了什么。"""
    project = _project(tmp_path)
    store = ProjectStore(_life(tmp_path))
    store.append_work(project.project_id, "第一章：岛村与驹子。", now=_cn(9))
    store.record_step(project.project_id, did="读完第一章", next_step="读第二章", now=_cn(9))
    _agenda(
        tmp_path, Block(start="15:00", end="16:00", activity="读第二章", project_id=project.project_id)
    )
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(15, 5))
    ctx = _make_ctx(tmp_path)

    await autonomy.run_tick(ctx)

    turn = ctx.turns[0]
    assert "岛村与驹子" in turn["work_tail"]
    assert any("读完第一章" in step for step in turn["recent_steps"])
    assert turn["project"]["next_step"] == "读第二章"


@pytest.mark.asyncio
async def test_she_may_skip_a_block_but_says_why(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _agenda(
        tmp_path, Block(start="10:00", end="11:00", activity="读第一章", project_id=project.project_id)
    )
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(10, 5))
    ctx = _make_ctx(tmp_path, work=lambda _k: {"text": "先不做了：今天有点提不起劲"})

    episode = await autonomy.run_tick(ctx)

    assert episode is None
    block = AgendaStore(_life(tmp_path)).load(_DAY).blocks[0]
    assert block.status == "skipped" and block.reason == "今天有点提不起劲"
    assert ProjectStore(_life(tmp_path)).read_work(project.project_id) == ""
    assert any(m[0] == "nudge" and m[1] < 0 for m in ctx.moods)


@pytest.mark.asyncio
async def test_a_failed_block_is_not_rerun_on_the_next_heartbeat(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _agenda(
        tmp_path, Block(start="10:00", end="11:30", activity="读第一章", project_id=project.project_id)
    )
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(10, 5))

    def broken(_kwargs):
        raise RuntimeError("upstream 503")

    ctx = _make_ctx(tmp_path, work=broken)
    await autonomy.run_tick(ctx)
    await autonomy.run_tick(ctx)

    assert len(ctx.turns) == 1
    assert AgendaStore(_life(tmp_path)).load(_DAY).blocks[0].status == "skipped"


@pytest.mark.asyncio
async def test_a_routine_block_costs_no_model_call_but_sets_what_she_is_doing(
    tmp_path, monkeypatch
):
    _agenda(tmp_path, Block(start="14:00", end="14:40", activity="午睡"))
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(14, 5))
    ctx = _make_ctx(tmp_path)

    await autonomy.run_tick(ctx)

    assert ctx.turns == [] and ctx.calls == []
    assert LifeStateStore(_life(tmp_path)).load().current_activity(_cn(14, 20)) == "午睡"
    assert LifeStateStore(_life(tmp_path)).load().current_activity(_cn(15)) == ""


@pytest.mark.asyncio
async def test_the_daily_work_limit_is_respected(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _agenda(
        tmp_path,
        Block(
            start="09:00", end="10:00", activity="读", project_id=project.project_id, status="done"
        ),
        Block(start="10:30", end="11:30", activity="再读", project_id=project.project_id),
    )
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(10, 40))
    ctx = _make_ctx(tmp_path, config={"max_work_blocks_per_day": 1})

    await autonomy.run_tick(ctx)

    assert ctx.turns == []
    assert AgendaStore(_life(tmp_path)).load(_DAY).blocks[1].status == "skipped"


@pytest.mark.asyncio
async def test_blocks_that_passed_while_she_was_offline_are_not_run_late(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _agenda(
        tmp_path, Block(start="09:00", end="10:00", activity="读", project_id=project.project_id)
    )
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(12))
    ctx = _make_ctx(tmp_path)

    await autonomy.run_tick(ctx)

    assert ctx.turns == []
    assert AgendaStore(_life(tmp_path)).load(_DAY).blocks[0].status == "missed"


# --- 夜里：回顾这一天 ---------------------------------------------------------------


def _reflection(**overrides) -> str:
    data = {
        "recent": "最近在读《雪国》，读得很慢，但很喜欢那种安静。",
        "interests": ["川端康成", "冬天", "雪"],
        "tomorrow": "把第二章读完",
        "mood_shift": 0.5,
        "projects": [],
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


@pytest.mark.asyncio
async def test_she_looks_back_on_her_day_once_at_bedtime(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _agenda(
        tmp_path,
        Block(
            start="10:00",
            end="11:00",
            activity="读第一章",
            project_id=project.project_id,
            status="done",
            outcome="读完了",
        ),
    )
    clock = [_cn(23, 40)]
    monkeypatch.setattr(autonomy, "_now", lambda: clock[0])
    # 只看回想本身；做梦另有用例。
    ctx = _make_ctx(
        tmp_path,
        config={"dream_enabled": False},
        reflection=_reflection(projects=[{"id": project.project_id, "next_step": "读第二章"}]),
    )

    await autonomy.run_tick(ctx)
    clock[0] = _cn(2, 0, day=2)
    await autonomy.run_tick(ctx)

    assert [c["task"] for c in ctx.calls] == ["life_reflect"]
    assert "读第一章" in ctx.calls[0]["system"]
    note = SelfNoteStore(_life(tmp_path)).load()
    assert note.recent.startswith("最近在读《雪国》")
    assert note.interests == ["川端康成", "冬天", "雪"]
    assert ProjectStore(_life(tmp_path)).get(project.project_id).next_step == "读第二章"
    # 基线心情只会被轻轻推一下，不会被一次回顾大幅改写。
    assert ("settle", 0.05) in ctx.moods
    assert ctx.turns == []


@pytest.mark.asyncio
async def test_a_garbled_reflection_keeps_what_she_knew_about_herself(tmp_path, monkeypatch):
    SelfNoteStore(_life(tmp_path)).save(
        SelfNoteStore(_life(tmp_path)).load().__class__(recent="最近在学做饭。")
    )
    _agenda(tmp_path, Block(start="18:00", end="19:00", activity="做饭", status="done"))
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(23, 45))
    ctx = _make_ctx(tmp_path, reflection="今天还不错！")

    await autonomy.run_tick(ctx)

    assert SelfNoteStore(_life(tmp_path)).load().recent == "最近在学做饭。"


@pytest.mark.asyncio
async def test_she_does_no_work_while_asleep(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _agenda(
        tmp_path, Block(start="22:30", end="23:20", activity="读", project_id=project.project_id)
    )
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(0, 30, day=2))
    ctx = _make_ctx(tmp_path)

    await autonomy.run_tick(ctx)

    assert ctx.turns == []


@pytest.mark.asyncio
async def test_her_sleep_schedule_is_configurable(tmp_path, monkeypatch):
    """夜猫子作息：凌晨一点她还醒着，还能按日程做事。"""
    project = _project(tmp_path)
    _agenda(
        tmp_path, Block(start="00:30", end="01:30", activity="写", project_id=project.project_id)
    )
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(0, 40, day=2))
    ctx = _make_ctx(tmp_path, config={"wake_time": "10:00", "sleep_time": "02:30"})

    await autonomy.run_tick(ctx)

    assert len(ctx.turns) == 1


# --- 夜里：做梦 -------------------------------------------------------------------


def _dream(**overrides) -> str:
    data = {
        "dream": "梦见雪国的车站变成了一口火锅，大家排着队往里面扔书签，我怎么也挤不进去。",
        "feeling": "有点饿",
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def _chat_memory(tmp_path, group_id: str, *summaries: str, at=None) -> None:
    """某个群当天整理出来的记忆片段。"""
    stamp = (at or _cn(20)).isoformat()
    MemoryUnitFileStore(tmp_path).save(
        group_id,
        [
            MemoryUnit(unit_id=f"{group_id}-{i}", group_id=group_id, created_at=stamp, summary=text)
            for i, text in enumerate(summaries)
        ],
    )


@pytest.mark.asyncio
async def test_she_dreams_once_a_night_from_her_day_and_one_chat(tmp_path, monkeypatch):
    """睡下一阵之后做一个梦：素材是她的一天加上当天最热闹的那个群，别的群的事不会混进来。"""
    _agenda(tmp_path, Block(start="10:00", end="11:00", activity="读第一章", status="done"))
    _chat_memory(tmp_path, "group-A", "阿明说周末想去吃火锅", "大家在争论书签该不该折角")
    _chat_memory(tmp_path, "group-B", "小红偷偷说她要辞职了")
    # 片段最多的群却是前一天的事：不算今天的碎片。
    yesterday = _cn(20) - timedelta(days=1)
    _chat_memory(tmp_path, "group-C", "前一天的旧事", "另一件旧事", "第三件旧事", at=yesterday)
    clock = [_cn(23, 40)]
    monkeypatch.setattr(autonomy, "_now", lambda: clock[0])
    ctx = _make_ctx(tmp_path, reflection=_reflection(), dream=_dream())

    await autonomy.run_tick(ctx)  # 刚躺下：只回想，不做梦
    clock[0] = _cn(1, 30, day=2)
    await autonomy.run_tick(ctx)
    clock[0] = _cn(4, 0, day=2)
    await autonomy.run_tick(ctx)

    assert [c["task"] for c in ctx.calls] == ["life_reflect", "life_dream"]
    prompt = ctx.calls[1]["system"]
    assert "读第一章" in prompt and "阿明说周末想去吃火锅" in prompt
    assert "辞职" not in prompt and "旧事" not in prompt
    dream = DreamStore(_life(tmp_path)).get(_DAY)
    assert dream is not None and dream.text.startswith("梦见雪国的车站")
    assert dream.feeling == "有点饿" and dream.chat_id == "group-A"
    # 梦是假的：它不会被写成她的记忆。
    assert ctx.memories == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "reply", "calls"),
    [
        ({"dream_enabled": False}, _dream(), ["life_reflect"]),
        ({}, "梦到了什么来着……", ["life_reflect", "life_dream"]),
    ],
    ids=["dreaming-turned-off", "garbled-dream-is-not-retried"],
)
async def test_no_dream_is_kept_when_off_or_unreadable(tmp_path, monkeypatch, config, reply, calls):
    _agenda(tmp_path, Block(start="18:00", end="19:00", activity="做饭", status="done"))
    clock = [_cn(23, 40)]
    monkeypatch.setattr(autonomy, "_now", lambda: clock[0])
    ctx = _make_ctx(tmp_path, config=config, reflection=_reflection(), dream=reply)

    for moment in (_cn(23, 40), _cn(2, 0, day=2), _cn(3, 0, day=2)):
        clock[0] = moment
        await autonomy.run_tick(ctx)

    assert [c["task"] for c in ctx.calls] == calls
    assert DreamStore(_life(tmp_path)).get(_DAY) is None


@pytest.mark.asyncio
async def test_a_dream_of_only_her_own_day_belongs_to_no_chat(tmp_path, monkeypatch):
    _agenda(tmp_path, Block(start="18:00", end="19:00", activity="做饭", status="done"))
    clock = [_cn(23, 40)]
    monkeypatch.setattr(autonomy, "_now", lambda: clock[0])
    ctx = _make_ctx(tmp_path, reflection=_reflection(), dream=_dream())

    await autonomy.run_tick(ctx)
    clock[0] = _cn(2, 0, day=2)
    await autonomy.run_tick(ctx)

    assert DreamStore(_life(tmp_path)).get(_DAY).chat_id == ""


# --- 项目工具 ----------------------------------------------------------------------


def test_she_can_start_a_project_from_a_chat_and_write_in_it(tmp_path):
    ctx = _make_ctx(tmp_path)

    started = life_project.run(
        action="start", title="收集好听的雨声", kind="collect", why="朋友推荐", engine_context=ctx
    )
    project_id = started["project_id"]
    wrote = life_project.run(
        action="write", project_id=project_id, content="- 屋檐的雨\n- 竹林的雨", engine_context=ctx
    )
    listed = life_project.run(action="list", engine_context=ctx)

    assert started["success"] and wrote["success"]
    assert "竹林的雨" in ProjectStore(_life(tmp_path)).read_work(project_id)
    assert listed["projects"][0]["title"] == "收集好听的雨声"
    assert ctx.delivered == []


def test_she_cannot_juggle_more_than_three_projects(tmp_path):
    ctx = _make_ctx(tmp_path)
    for i in range(3):
        assert life_project.run(action="start", title=f"项目{i}", engine_context=ctx)["success"]

    fourth = life_project.run(action="start", title="第四件事", engine_context=ctx)

    assert fourth["success"] is False


# --- 说给谁听 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tell_intention_is_said_once_to_its_audience(tmp_path):
    """想说的话按她自己指定的对象说出去，并且只说一次。"""
    _seed_intention(
        tmp_path,
        what="今天的晚霞特别好看",
        resolution=RESOLUTION_TELL,
        audience="private_10001",
        audience_label="主人（私聊）",
        urgency=0.9,
    )
    ctx = _make_ctx(tmp_path)

    episode = await autonomy.run_tick(ctx)
    for _ in range(3):
        ctx.store.data["state"]["last_share_at"] = "1970-01-01T00:00:00+00:00"
        await autonomy.run_tick(ctx)

    assert episode is not None and episode.kind == "share"
    assert ctx.delivered == [
        {
            "audience": "private_10001",
            "text": "今天的晚霞特别好看",
            "event_id": f"autonomy-share:{episode.intention_id}",
        }
    ]


@pytest.mark.asyncio
async def test_share_is_not_delivered_twice_within_cooldown(tmp_path):
    """分享有节奏限制：刚说过就再想说，也要等一等。"""
    _seed_intention(
        tmp_path,
        what="刚看到一篇有意思的文章",
        resolution=RESOLUTION_TELL,
        audience="private_10001",
        urgency=0.9,
    )
    ctx = _make_ctx(tmp_path, state={"last_share_at": _cn(11, 50).isoformat()})

    episode = await autonomy.run_tick(ctx)

    assert episode is None
    assert ctx.delivered == []


@pytest.mark.asyncio
async def test_share_without_audience_is_not_guessed(tmp_path):
    """她还没想好说给谁时，不能替她挑一个地方发出去；她会得到一次想一想的机会。"""
    _seed_intention(tmp_path, what="有点想找人说说话", resolution=RESOLUTION_TELL, audience="", urgency=0.9)
    ctx = _make_ctx(tmp_path)

    await autonomy.run_tick(ctx)

    assert ctx.delivered == []
    assert ctx.turns and ctx.turns[0]["resolution"] == RESOLUTION_TELL


@pytest.mark.asyncio
async def test_she_can_write_down_something_she_wants_to_say(tmp_path):
    """她自己登记"想说的话"时，只登记、不发送。"""
    ctx = _make_ctx(tmp_path)
    ctx.list_audiences = lambda: [SimpleNamespace(chat_id="private_10001", label="主人（私聊）")]

    result = intend_share.run(
        what="我有点想你了", why="只是想说", audience="private_10001", engine_context=ctx
    )

    assert result["success"] is True
    assert ctx.delivered == []
    item = IntentFileStore(tmp_path).load().all()[0]
    assert item.resolution == RESOLUTION_TELL
    assert item.audience_label == "主人（私聊）"


# --- 边界 -------------------------------------------------------------------------


def test_self_initiated_turn_refuses_delivery_tools():
    """自主时间禁止一切对外发送的工具，只读工具不受影响。"""
    sending = ToolDefinition(
        name="group_file_exec", description="", side_effect=ToolSideEffect.EXTERNAL_WRITE
    )
    reading = ToolDefinition(
        name="web_lookup", description="", side_effect=ToolSideEffect.READ_ONLY
    )

    assert _self_initiated_block_reason(sending) != ""
    assert _self_initiated_block_reason(reading) == ""


@pytest.mark.parametrize("tool", [intend_share, intend_pursue, life_project])
def test_recording_tools_are_allowed_on_her_own_time(tool):
    """登记想说的、想做的、写进作品，都只是记录，自主时间里必须可用。"""
    assert tool.TOOL_META["allowed_when_self_initiated"] is True
    assert tool.TOOL_META.get("model_visible", True) is True


def test_master_interaction_is_allowed_on_her_own_time():
    from sirius_pulse.tools.builtin import interaction_with_master

    assert interaction_with_master.TOOL_META["allowed_when_self_initiated"] is True


# --- 睡着时：可以等，但不可以发出去 -------------------------------------------------


@pytest.mark.parametrize("moment", [_cn(23, 0), _cn(23, 59), _cn(0, 0), _cn(3, 30), _cn(7, 59)])
def test_sending_is_blocked_through_the_night_without_a_schedule(moment):
    """没有作息时按 23:00 到次日 08:00 静默，含跨零点两侧。"""
    assert is_quiet_hours(moment) is True


@pytest.mark.parametrize("moment", [_cn(8, 0), _cn(12, 0), _cn(22, 59)])
def test_sending_is_allowed_outside_the_night(moment):
    assert is_quiet_hours(moment) is False


def test_night_window_is_read_in_china_time_not_utc():
    """判定按中国时间，而不是 UTC，否则静默期会整整错开八小时。"""
    assert is_quiet_hours(datetime(2026, 3, 1, 19, tzinfo=timezone.utc)) is True


@pytest.mark.asyncio
async def test_a_message_written_at_night_waits_until_she_wakes(tmp_path, monkeypatch):
    """睡着时想说的话不会被丢掉，也不会被标记成已说，只是等她醒来再发。"""
    intention = _seed_intention(
        tmp_path,
        what="今天的晚霞特别好看",
        resolution=RESOLUTION_TELL,
        audience="private_10001",
        urgency=0.9,
    )
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(23, 40))
    night = _make_ctx(tmp_path)
    await autonomy.run_tick(night)
    assert night.delivered == []

    monkeypatch.setattr(autonomy, "_now", lambda: _cn(8, 5, day=2))
    morning = _make_ctx(tmp_path, state=night.store.data["state"])
    episode = await autonomy.run_tick(morning)

    assert [d["text"] for d in morning.delivered] == ["今天的晚霞特别好看"]
    assert episode is not None and episode.kind == "share"
    assert IntentFileStore(tmp_path).load().get(intention.intention_id).shared_at != ""


@pytest.mark.asyncio
async def test_a_night_of_backlog_drains_one_per_cooldown(tmp_path, monkeypatch):
    """醒来不能轰炸：积压的话按 share_cooldown_seconds 一条条摊开。"""
    for i in range(3):
        _seed_intention(
            tmp_path,
            what=f"夜里第{i + 1}件想说的",
            resolution=RESOLUTION_TELL,
            audience="private_10001",
            urgency=0.9,
        )
    clock = [_cn(7, 30)]
    monkeypatch.setattr(autonomy, "_now", lambda: clock[0])
    ctx = _make_ctx(tmp_path)

    delivered_after_first_hour = None
    for i in range(4 * 3):  # 三小时，每 15 分钟一跳
        clock[0] += timedelta(minutes=15)
        await autonomy.run_tick(ctx)
        if i == 3:
            delivered_after_first_hour = len(ctx.delivered)

    assert delivered_after_first_hour == 1
    assert len(ctx.delivered) == 3
    assert ctx.delivered[0]["text"] == "夜里第1件想说的"


# --- 想念 -------------------------------------------------------------------------


def _friend(
    user_id="u-1",
    name="阿青",
    *,
    last_seen=None,
    count=40,
    engagement=0.6,
    chat="group-1",
):
    """一个她认识的人：在 chat 里聊过 count 次，最后一次出现在 last_seen。"""
    seen = last_seen if last_seen is not None else _cn(12) - timedelta(days=10)
    rows = [
        {
            "group_id": chat,
            "user_id": user_id,
            "name": name,
            "interaction_count": count,
            "engagement_rate": engagement,
            "last_interaction_at": seen.isoformat(),
        }
    ]
    return gather(rows)[0]


def _with_people(ctx, *people, chats=("group-1",)):
    ctx.list_acquaintances = lambda: list(people)
    ctx.list_audiences = lambda: [SimpleNamespace(chat_id=c, label=f"群{c}") for c in chats]
    return ctx


def _miss_thoughts(tmp_path):
    return [item for item in IntentFileStore(tmp_path).load().all() if item.kind == "miss"]


@pytest.mark.asyncio
async def test_she_starts_missing_a_close_friend_who_has_been_away(tmp_path):
    """亲近的人十天没出现：她心里冒出一个想去问候的念头，自己决定说什么、说给谁。"""
    ctx = _with_people(_make_ctx(tmp_path), _friend())

    await autonomy.run_tick(ctx)

    thoughts = _miss_thoughts(tmp_path)
    assert len(thoughts) == 1
    assert "阿青" in thoughts[0].what and thoughts[0].audience == ""
    assert ctx.delivered == []
    # 念头本身不会被当成要发出去的话：她得到一次自己决定的回合。
    turn = next(t for t in ctx.turns if t.get("autonomous"))
    assert turn["kind"] == "miss" and turn["intention_id"] == thoughts[0].intention_id
    assert "group-1" in turn["why"]

    intend_share.run(
        intention_id=thoughts[0].intention_id,
        what="阿青！好久没见你冒泡了，上次说的那家面馆去了没？",
        audience="group-1",
        engine_context=ctx,
    )
    await autonomy.run_tick(ctx)

    assert [d["text"] for d in ctx.delivered] == ["阿青！好久没见你冒泡了，上次说的那家面馆去了没？"]
    assert [d["audience"] for d in ctx.delivered] == ["group-1"]


@pytest.mark.asyncio
async def test_a_thought_of_someone_is_never_sent_without_her_own_words(tmp_path):
    """只挑了地方、没写想说的话：不算数，那句念头也不会被原样发出去。"""
    ctx = _with_people(_make_ctx(tmp_path), _friend())
    await autonomy.run_tick(ctx)
    thought = _miss_thoughts(tmp_path)[0]

    result = intend_share.run(
        intention_id=thought.intention_id, audience="group-1", engine_context=ctx
    )
    await autonomy.run_tick(ctx)

    assert result["success"] is False
    assert ctx.delivered == []


@pytest.mark.parametrize(
    "person",
    [
        pytest.param(_friend(count=3, engagement=0.0), id="not-close"),
        pytest.param(_friend(last_seen=_cn(9)), id="just-chatted"),
        pytest.param(_friend(chat="group-gone"), id="unreachable"),
        pytest.param(_friend(name=""), id="nameless"),
    ],
)
@pytest.mark.asyncio
async def test_she_does_not_miss_someone_she_is_not_close_to_or_just_saw(tmp_path, person):
    """不熟的人、刚聊过的人、已经找不到的人、叫不出名字的人，都不会被惦记。"""
    ctx = _with_people(_make_ctx(tmp_path), person)

    await autonomy.run_tick(ctx)

    assert _miss_thoughts(tmp_path) == []
    assert not [t for t in ctx.turns if t.get("kind") == "miss"]


@pytest.mark.asyncio
async def test_she_does_not_miss_anyone_while_asleep(tmp_path, monkeypatch):
    """睡着时不会冒出念头；醒来之后才会想起。"""
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(2, 0, day=2))
    ctx = _with_people(_make_ctx(tmp_path), _friend())
    await autonomy.run_tick(ctx)
    assert _miss_thoughts(tmp_path) == []

    monkeypatch.setattr(autonomy, "_now", lambda: _cn(9, 0, day=2))
    await autonomy.run_tick(ctx)
    assert len(_miss_thoughts(tmp_path)) == 1


@pytest.mark.asyncio
async def test_she_does_not_think_of_the_same_person_twice_within_days(tmp_path, monkeypatch):
    """同一个人，想过一次之后要隔 miss_again_after_days 天才会再想起。"""
    clock = [_cn(12)]
    monkeypatch.setattr(autonomy, "_now", lambda: clock[0])
    ctx = _with_people(
        _make_ctx(tmp_path, config={"miss_again_after_days": 7}),
        _friend(last_seen=_cn(12) - timedelta(days=10)),
    )
    await autonomy.run_tick(ctx)
    first = _miss_thoughts(tmp_path)
    assert len(first) == 1
    # 她问候过了（或者决定不打扰），这件事就了结了。
    intentions = IntentFileStore(tmp_path).load()
    intentions.resolve(first[0].intention_id, outcome="问候过了")
    IntentFileStore(tmp_path).save(intentions)

    for day in (2, 4, 7):
        clock[0] = _cn(12, day=day)
        await autonomy.run_tick(ctx)
    assert len(_miss_thoughts(tmp_path)) == 1

    clock[0] = _cn(12, day=9)
    await autonomy.run_tick(ctx)
    assert len(_miss_thoughts(tmp_path)) == 2


@pytest.mark.parametrize(("limit", "expected"), [(None, 1), (2, 2), (5, 2), (0, 0)])
@pytest.mark.asyncio
async def test_she_misses_only_a_few_people_a_day(tmp_path, monkeypatch, limit, expected):
    """一天惦记的人有上限（默认 1，最多 2），最亲近的人先被想起；第二天才轮到下一个。"""
    clock = [_cn(10)]
    monkeypatch.setattr(autonomy, "_now", lambda: clock[0])
    config = {} if limit is None else {"max_miss_per_day": limit}
    friends = [
        _friend("u-1", "阿青", count=10, engagement=0.5),
        _friend("u-2", "小林", count=60, engagement=0.9),
        _friend("u-3", "老周", count=30, engagement=0.6),
    ]
    ctx = _with_people(_make_ctx(tmp_path, config=config), *friends)

    for hour in (10, 13, 16, 19):
        clock[0] = _cn(hour)
        await autonomy.run_tick(ctx)

    thoughts = _miss_thoughts(tmp_path)
    assert len(thoughts) == expected
    if expected:
        assert "小林" in thoughts[0].what

    if limit is None:
        clock[0] = _cn(10, day=2)
        await autonomy.run_tick(ctx)
        assert len(_miss_thoughts(tmp_path)) == 2


@pytest.mark.asyncio
async def test_missing_people_can_be_turned_off(tmp_path):
    """miss_after_days 设为 0：她不再主动惦记谁。"""
    ctx = _with_people(_make_ctx(tmp_path, config={"miss_after_days": 0}), _friend())

    await autonomy.run_tick(ctx)

    assert _miss_thoughts(tmp_path) == []


# --- 想主人 -----------------------------------------------------------------------


def _with_master(ctx, *, hours_away, recent=None):
    master = _friend(user_id="qq_10001", name="临雀", last_seen=_cn(12) - timedelta(hours=hours_away))
    ctx.list_acquaintances = lambda: [master]
    ctx.list_audiences = lambda: [
        SimpleNamespace(chat_id="group-1", label="群group-1", kind="group"),
        SimpleNamespace(chat_id="private_10001", label="主人（私聊）", kind="private"),
    ]
    ctx.get_recent_messages = lambda _chat, _n=10: list(recent or [])
    return ctx


def _master_thoughts(tmp_path):
    return [i for i in IntentFileStore(tmp_path).load().all() if i.kind == "miss_master"]


@pytest.mark.asyncio
async def test_she_wants_to_find_her_master_after_hours_without_him(tmp_path):
    """主人几个小时没来找她：她想去找他，并且知道群里这会儿在热闹地聊什么。"""
    now = _cn(12)
    chat = [
        {
            "role": "human",
            "content": f"第{i}句关于新显卡的讨论",
            "timestamp": (now - timedelta(minutes=5 + i)).isoformat(),
        }
        for i in range(4)
    ]
    ctx = _with_master(_make_ctx(tmp_path), hours_away=8, recent=chat)

    await autonomy.run_tick(ctx)

    thoughts = _master_thoughts(tmp_path)
    assert len(thoughts) == 1 and thoughts[0].audience == ""
    assert "8个小时" in thoughts[0].why and "显卡" in thoughts[0].why
    turn = next(t for t in ctx.turns if t.get("autonomous"))
    assert turn["kind"] == "miss_master"
    assert ctx.delivered == [], "念头只是念头：说什么、发到哪由她自己决定"


@pytest.mark.parametrize("hours_away", [1, 5])
@pytest.mark.asyncio
async def test_she_does_not_chase_a_master_who_was_just_around(tmp_path, hours_away):
    ctx = _with_master(_make_ctx(tmp_path), hours_away=hours_away)

    await autonomy.run_tick(ctx)

    assert _master_thoughts(tmp_path) == []


@pytest.mark.asyncio
async def test_missing_master_is_bounded_per_day_and_can_be_turned_off(tmp_path, monkeypatch):
    ctx = _with_master(_make_ctx(tmp_path, config={"max_master_per_day": 1}), hours_away=8)
    await autonomy.run_tick(ctx)
    for item in _master_thoughts(tmp_path):
        store = IntentFileStore(tmp_path).load()
        store.mark_shared(item.intention_id, now=_cn(12).isoformat())
        IntentFileStore(tmp_path).save(store)
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(18))
    await autonomy.run_tick(ctx)

    assert len(_master_thoughts(tmp_path)) == 1, "一天只主动找一次"

    off = _with_master(
        _make_ctx(tmp_path / "off", config={"master_miss_after_hours": 0}), hours_away=8
    )
    await autonomy.run_tick(off)
    assert _master_thoughts(tmp_path / "off") == []
