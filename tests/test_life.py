"""她的生活在规则层与聊天侧的表现：作息、精力、日程/回顾的校验、作品文件，以及聊天里看得见的她。

业务视角：
- 作息决定她哪天、是不是醒着；凌晨还没睡也仍算"昨天"；
- 模型给的日程和回顾都要经过校验：越界、重叠、超量的内容被丢掉，乱写的整份作废；
- 聊天时她知道自己此刻在做什么、累不累；睡着时只回应真正叫她的人，被吵醒会记得；
- 昨晚的梦她早上还记得，只在梦的素材来源的那个群里提，过了上午就忘了；
- 心情会被她自己的生活推动，但基线只会被轻轻挪一点；
- 她认识的人按"一个人"来记：在哪儿聊过、多久没出现、有多亲近。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sirius_pulse.core import life_bridge
from sirius_pulse.core.participation import ParticipationPolicy
from sirius_pulse.life import life_dir
from sirius_pulse.life.agenda import AgendaStore, Block, DayAgenda
from sirius_pulse.life.clock import DaySchedule
from sirius_pulse.life.dreams import Dream, DreamStore, is_dream_time, parse_dream
from sirius_pulse.life.projects import WORK_MAX_CHARS, ProjectStore
from sirius_pulse.life.reflection import parse_plan, parse_reflection
from sirius_pulse.life.state import LifeStateStore, energy_at
from sirius_pulse.models.emotion import AssistantEmotionState
from sirius_pulse.models.response_strategy import ResponseStrategy
from sirius_pulse.models.signal import SignalAnalysis

_CN_TZ = timezone(timedelta(hours=8))
_SCHEDULE = DaySchedule()  # 07:30 起、23:30 睡


def _cn(hour: int, minute: int = 0, day: int = 1) -> datetime:
    return datetime(2026, 3, day, hour, minute, tzinfo=_CN_TZ).astimezone(timezone.utc)


# --- 作息与精力 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "phase", "life_date"),
    [
        (_cn(7, 40), "morning", "2026-03-01"),
        (_cn(15), "day", "2026-03-01"),
        (_cn(21), "evening", "2026-03-01"),
        (_cn(23, 45), "asleep", "2026-03-01"),
        (_cn(1, 0, day=2), "asleep", "2026-03-01"),
        (_cn(7, 0, day=2), "asleep", "2026-03-01"),
    ],
)
def test_her_day_follows_her_schedule_not_the_calendar(moment, phase, life_date):
    assert _SCHEDULE.phase(moment) == phase
    assert _SCHEDULE.life_date(moment) == life_date


def test_a_night_owl_is_still_awake_after_midnight():
    owl = DaySchedule.from_strings("10:00", "02:30")
    assert owl.is_asleep(_cn(1, 0, day=2)) is False
    assert owl.life_date(_cn(1, 0, day=2)) == "2026-03-01"
    assert owl.is_asleep(_cn(9, 0, day=2)) is True


def test_a_broken_schedule_falls_back_to_the_default():
    assert DaySchedule.from_strings("25:99", "nope") == DaySchedule()
    assert DaySchedule.from_strings("08:00", "08:00") == DaySchedule()


def test_her_energy_dips_with_the_day_and_with_what_she_did():
    fresh = energy_at(_SCHEDULE, _cn(9, 30))
    evening = energy_at(_SCHEDULE, _cn(22, 30))
    worked = energy_at(_SCHEDULE, _cn(9, 30), work_done_today=3, replies_today=60)
    woken = energy_at(_SCHEDULE, _cn(9, 30), woken_last_night=2)

    assert fresh > evening
    assert fresh > worked
    assert fresh > woken


# --- 早上的日程：模型说了算，但要合规矩 ----------------------------------------------


def _parse_plan(blocks, *, new_projects=None, max_work_blocks=3, free_slots=3, not_before=0):
    text = (
        "好的，这是我的安排：\n```json\n"
        + json.dumps(
            {"note": "慢慢来", "new_projects": new_projects or [], "blocks": blocks},
            ensure_ascii=False,
        )
        + "\n```"
    )
    return parse_plan(
        text,
        schedule=_SCHEDULE,
        project_ids={"p-0000abcd"},
        intention_ids=set(),
        max_work_blocks=max_work_blocks,
        free_project_slots=free_slots,
        not_before=not_before,
    )


def test_a_plan_keeps_only_blocks_that_fit_her_day():
    plan = _parse_plan(
        [
            {"start": "09:00", "end": "10:00", "activity": "读书", "project": "p-0000abcd"},
            {"start": "09:30", "end": "10:30", "activity": "重叠了"},
            {"start": "03:00", "end": "04:00", "activity": "半夜不行"},
            {"start": "12:00", "end": "12:05", "activity": "太短"},
            {"start": "13:00", "end": "14:00", "activity": "陌生项目", "project": "p-ffffffff"},
            {"start": "23:00", "end": "23:59", "activity": "睡过头了"},
        ]
    )

    assert plan is not None
    assert [(b.start, b.activity, b.project_id) for b in plan.blocks] == [
        ("09:00", "读书", "p-0000abcd"),
        ("13:00", "陌生项目", ""),
    ]


def test_a_plan_cannot_exceed_her_daily_work_limit():
    plan = _parse_plan(
        [
            {"start": "09:00", "end": "10:00", "activity": "一", "project": "p-0000abcd"},
            {"start": "11:00", "end": "12:00", "activity": "二", "project": "p-0000abcd"},
        ],
        max_work_blocks=1,
    )
    assert [b.activity for b in plan.blocks] == ["一"]


def test_a_late_plan_drops_what_is_already_over():
    plan = _parse_plan(
        [
            {"start": "09:00", "end": "10:00", "activity": "早上的"},
            {"start": "16:00", "end": "17:00", "activity": "下午的"},
        ],
        not_before=_SCHEDULE.offset_of(15 * 60),
    )
    assert [b.activity for b in plan.blocks] == ["下午的"]


def test_a_new_project_only_starts_if_it_made_it_onto_the_plan():
    plan = _parse_plan(
        [{"start": "09:00", "end": "10:00", "activity": "写", "project": "n1"}],
        new_projects=[
            {"key": "n1", "title": "写一篇关于雨的小文", "kind": "write"},
            {"key": "n2", "title": "没排进去的", "kind": "learn"},
        ],
    )
    assert [p.title for p in plan.new_projects] == ["写一篇关于雨的小文"]


@pytest.mark.parametrize("text", ["今天想随便过过", "{}", '{"blocks": "nope"}'])
def test_an_unusable_plan_is_rejected_as_a_whole(text):
    assert (
        parse_plan(
            text,
            schedule=_SCHEDULE,
            project_ids=set(),
            intention_ids=set(),
            max_work_blocks=3,
            free_project_slots=3,
        )
        is None
    )


# --- 夜里的回顾：小、截断、覆盖 -----------------------------------------------------


def test_a_reflection_is_clamped_and_cannot_touch_unknown_projects():
    result = parse_reflection(
        json.dumps(
            {
                "recent": "长" * 1000,
                "interests": [f"兴趣{i}" for i in range(20)] + ["兴趣0"],
                "mood_shift": -3,
                "projects": [
                    {"id": "p-0000abcd", "status": "done"},
                    {"id": "p-ffffffff", "status": "dropped"},
                    {"id": "p-0000abcd", "status": "active"},
                ],
            },
            ensure_ascii=False,
        ),
        project_ids={"p-0000abcd"},
    )

    assert len(result.note.recent) == 300
    assert len(result.note.interests) == 8
    assert result.mood_shift == -0.05
    assert [(u.project_id, u.status) for u in result.project_updates] == [
        ("p-0000abcd", "done"),
        ("p-0000abcd", ""),
    ]


def test_a_reflection_without_a_self_description_is_rejected():
    assert parse_reflection('{"interests": ["雪"]}', project_ids=set()) is None


# --- 作品文件 ---------------------------------------------------------------------


def test_a_long_work_keeps_its_recent_part_and_archives_the_rest(tmp_path):
    store = ProjectStore(life_dir(tmp_path))
    project = store.start(title="长篇草稿", kind="write", now=_cn(9))
    for i in range(12):
        store.append_work(project.project_id, f"第{i}段\n" + "字" * 5000, now=_cn(9, i))

    text = store.read_work(project.project_id)
    archive = (store.works_dir / f"{project.project_id}.old.md").read_text(encoding="utf-8")
    assert len(text) <= WORK_MAX_CHARS
    assert text.startswith("# 长篇草稿")
    assert "第11段" in text and "第0段" in archive


def test_work_files_cannot_escape_the_works_directory(tmp_path):
    store = ProjectStore(life_dir(tmp_path))
    with pytest.raises(ValueError):
        store.work_path("../../persona")


# --- 心情 -------------------------------------------------------------------------


def test_her_own_life_moves_her_mood_but_baseline_only_drifts():
    mood = AssistantEmotionState()
    before = mood.valence
    mood.apply_event(0.15, 0.05)
    assert mood.valence == pytest.approx(before + 0.15)

    mood.shift_baseline(0.9)
    assert mood.baseline_valence == pytest.approx(0.25)


# --- 聊天侧：看得见的她 ------------------------------------------------------------


def _engine(tmp_path, config: dict | None = None):
    return SimpleNamespace(
        work_path=str(tmp_path),
        _tool_executor=SimpleNamespace(get_data_store=lambda _name: dict(config or {})),
        assistant_emotion=AssistantEmotionState(valence=0.6, arousal=0.3),
    )


def test_the_chat_knows_what_she_is_doing_and_how_she_feels(tmp_path):
    life = life_dir(tmp_path)
    LifeStateStore(life).update(lambda s: s.set_activity("读《雪国》第二章", _cn(16)))
    AgendaStore(life).save(
        DayAgenda(
            date="2026-03-01",
            blocks=[Block(start="19:00", end="20:00", activity="写读书笔记")],
        )
    )

    text = life_bridge.presence_context(_engine(tmp_path), _cn(15, 30))

    assert "【此刻的你】" in text
    assert "读《雪国》第二章" in text
    assert "19:00 写读书笔记" in text
    assert "精力" in text and "心情" in text


def test_a_reply_at_night_wakes_her_and_she_remembers(tmp_path):
    engine = _engine(tmp_path)

    life_bridge.record_reply(engine, _cn(2, 0, day=2))
    asleep = life_bridge.presence_context(engine, _cn(2, 1, day=2))
    morning = life_bridge.presence_context(engine, _cn(8, 30, day=2))

    assert "吵醒" in asleep
    assert "被吵醒过" in morning


def test_asleep_she_answers_only_someone_who_really_calls_for_her(tmp_path):
    engine = _engine(tmp_path)
    awake = life_bridge.reply_factor(engine, _cn(15))
    asleep = life_bridge.reply_factor(engine, _cn(3))
    policy = ParticipationPolicy()
    chatter = SignalAnalysis(
        directed_score=0.25,
        is_question=True,
        urgency_score=55,
        relevance_score=0.55,
        social_intent="help_seeking",
        heat_level="warm",
        pace="steady",
        turn_gap_readiness=0.45,
    )
    called = SignalAnalysis(
        directed_score=0.9,
        is_mentioned=True,
        is_question=True,
        urgency_score=80,
        relevance_score=0.8,
        social_intent="help_seeking",
    )

    def decide(signal, factor):
        return policy.evaluate(
            signal=signal,
            content="这个报错有没有办法绕过去？",
            is_private=False,
            seconds_since_reply=120,
            cooldown_seconds=30,
            directed_gate=0.55,
            life_factor=factor,
        ).strategy

    assert awake == 1.0
    assert decide(chatter, awake) != ResponseStrategy.SILENT
    assert decide(chatter, asleep) == ResponseStrategy.SILENT
    assert decide(called, asleep) != ResponseStrategy.SILENT


@pytest.mark.parametrize(
    ("dream_chat", "chat", "moment", "remembered"),
    [
        ("group-A", "group-A", _cn(8, 30, day=2), True),
        ("group-A", "group-B", _cn(8, 30, day=2), False),
        ("", "group-B", _cn(8, 30, day=2), True),
        ("group-A", "group-A", _cn(15, 0, day=2), False),
        ("group-A", "group-A", _cn(8, 30, day=3), False),
    ],
    ids=["morning-same-chat", "other-chat", "own-day-dream", "afternoon", "next-morning"],
)
def test_she_remembers_last_nights_dream_only_in_the_morning_and_its_chat(
    tmp_path, dream_chat, chat, moment, remembered
):
    DreamStore(life_dir(tmp_path)).save(
        Dream(date="2026-03-01", text="梦见图书馆漂在海上", feeling="很安静", chat_id=dream_chat)
    )

    text = life_bridge.presence_context(_engine(tmp_path), moment, chat_id=chat)

    assert ("梦见图书馆漂在海上" in text) is remembered
    if remembered:
        assert "梦是假的" in text


@pytest.mark.parametrize(
    ("moment", "due"),
    [(_cn(23, 45), False), (_cn(1, 30, day=2), True), (_cn(15), False)],
)
def test_she_dreams_only_after_she_has_been_asleep_a_while(moment, due):
    assert is_dream_time(_SCHEDULE, moment) is due


@pytest.mark.parametrize("text", ["", "做了个梦", '{"dream": "嗯"}', '{"feeling": "开心"}'])
def test_an_unusable_dream_is_dropped(text):
    assert parse_dream(text, date="2026-03-01") is None


def test_a_missing_life_never_breaks_a_chat_reply():
    engine = SimpleNamespace(work_path="", _tool_executor=None)
    assert life_bridge.presence_context(engine) == ""
    assert life_bridge.reply_factor(engine) == 1.0
    life_bridge.record_reply(engine)


# --- 她认识的人 -------------------------------------------------------------------


def test_someone_she_talks_with_in_several_places_is_one_person_to_her(tmp_path):
    """群里和私聊都聊过的人只算一个人：最近聊过的地方排前面，她的好感也让对方更亲近。"""
    from sirius_pulse.core.tool_engine_context import ToolEngineContextImpl
    from sirius_pulse.memory.semantic.manager import SemanticMemoryManager

    semantic = SemanticMemoryManager(None)
    for i in range(20):
        semantic.record_interaction("group-1", "u-1", _cn(10, i, day=1).isoformat())
    semantic.set_user_profile_fields("group-1", "u-1", name="阿青")
    semantic.record_interaction("private_10001", "u-1", _cn(9, day=3).isoformat())
    semantic.record_interaction("group-1", "u-2", _cn(9, day=3).isoformat())
    semantic.set_user_profile_fields("group-1", "u-2", name="小林")

    def people(bias):
        engine = SimpleNamespace(
            identity_resolver=None,
            user_manager=SimpleNamespace(get_user=lambda *_a: None),
            semantic_memory=semantic,
            assistant_emotion=AssistantEmotionState(user_bias=bias),
        )
        return {p.user_id: p for p in ToolEngineContextImpl(engine).list_acquaintances()}

    plain = people({})
    warm = people({"u-1": (0.8, 0.5)})

    qing = plain["u-1"]
    assert qing.name == "阿青" and qing.interaction_count == 21
    assert qing.chats == ["private_10001", "group-1"]
    assert round(qing.days_away(_cn(9, day=8))) == 5
    assert qing.closeness > plain["u-2"].closeness
    assert warm["u-1"].closeness > qing.closeness
