"""群里的主动：有分寸。只对惦记的熟人顺势问一句，受档位、额度、安静要求和冷场反馈约束。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sirius_pulse.core import initiative
from sirius_pulse.core.initiative import InitiativeLedger, InitiativeService, level_for
from sirius_pulse.memory.basic import BasicMemoryManager
from sirius_pulse.memory.context_assembler import ContextAssembler
from sirius_pulse.memory.impressions import OpenThread, PersonImpression, impression_store
from sirius_pulse.persona_config import PersonaExperienceConfig

GROUP = "1057020972"
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


# --- 档位与额度 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "allowed_today"),
    [("normal", 8), ("quiet", 3), ("off", 0)],
)
def test_daily_budget_follows_the_group_level(tmp_path, level, allowed_today):
    ledger = InitiativeLedger(tmp_path / "initiative.json")
    acted = 0
    for hour in range(12):
        now = NOW + timedelta(hours=hour)
        if ledger.blocked_reason(GROUP, now, level):
            continue
        ledger.record(GROUP, now, kind="follow_up", target=f"qq_{hour}")
        ledger.note_message(GROUP, f"qq_{hour}", now + timedelta(minutes=1), addressed=False)
        acted += 1
    assert acted == allowed_today


def test_being_ignored_halves_the_budget_until_someone_answers(tmp_path):
    ledger = InitiativeLedger(tmp_path / "initiative.json")
    for hour in (0, 1):
        ledger.record(GROUP, NOW + timedelta(hours=hour), kind="follow_up", target="qq_1")
    later = NOW + timedelta(hours=3)
    for hour in (2, 3):
        ledger.record(GROUP, NOW + timedelta(hours=hour, minutes=30), kind="x", target="qq_2")
    # 两次都没人接：normal 的 8 次减半为 4，今天已用 4 次。
    assert ledger.blocked_reason(GROUP, later + timedelta(hours=1), "normal") == "daily_budget"

    answered = InitiativeLedger(tmp_path / "other.json")
    answered.record(GROUP, NOW, kind="follow_up", target="qq_1")
    answered.note_message(GROUP, "qq_1", NOW + timedelta(minutes=2), addressed=False)
    assert answered._ignored_streak(answered.acts(GROUP), NOW + timedelta(hours=1)) == 0


def test_levels_come_from_experience_with_per_group_override():
    exp = PersonaExperienceConfig.from_dict(
        {"initiative_level": "quiet", "group_initiative_levels": {GROUP: "off", "2": "bogus"}}
    )
    config = {
        "initiative_level": exp.initiative_level,
        "group_initiative_levels": exp.group_initiative_levels,
    }

    assert level_for(config, GROUP) == "off"
    assert level_for(config, "elsewhere") == "quiet"
    assert "2" not in exp.group_initiative_levels
    assert PersonaExperienceConfig().initiative_level == "normal"


# --- 顺势问一句 -----------------------------------------------------------------


class Brain:
    def __init__(self, reply):
        self.reply = reply
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        return SimpleNamespace(clean_text=self.reply, sticker_names=[], poke_user_ids=[])


def _engine(tmp_path, *, reply="诶临雀，你那个面试怎么样啦？", mentions_bot=False):
    basic = BasicMemoryManager()
    sent = []

    async def dispatch(**kwargs):
        sent.append(kwargs)
        return True

    engine = SimpleNamespace(
        work_path=tmp_path,
        config={},
        basic_memory=basic,
        context_assembler=ContextAssembler(basic),
        brain=Brain(reply),
        dispatch_proactive_message=dispatch,
        _message_explicitly_mentions_current_bot=lambda _m: mentions_bot,
        _log_inner_thought=lambda *_a, **_k: None,
        _work_mode_runs={},
        _other_ai_names=[],
        _sticker_names=[],
        sent=sent,
    )
    soon = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    impression_store(engine).save(
        PersonImpression(
            user_id="qq_2388389247",
            name="临雀",
            impression="开发我的人。",
            threads=[OpenThread(text="他说要去面试", due=soon)],
            warmth=0.9,
        )
    )
    return engine


def _message(text="我回来啦", sender_type="human"):
    return SimpleNamespace(content=text, sender_type=sender_type)


def _run_follow_ups(service):
    async def drain():
        tasks = list(service._pending.values())
        if tasks:
            await asyncio.gather(*tasks)

    async def main(engine_message):
        service.on_message(GROUP, "qq_2388389247", engine_message)
        await drain()

    return main


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(initiative, "FOLLOW_UP_DELAY_SECONDS", (0.0, 0.0))


def test_when_he_shows_up_she_asks_about_what_she_remembered_once(tmp_path):
    engine = _engine(tmp_path)
    service = InitiativeService(engine)

    asyncio.run(_run_follow_ups(service)(_message()))
    asyncio.run(_run_follow_ups(service)(_message("还在吗")))

    assert [s["text"] for s in engine.sent] == ["诶临雀，你那个面试怎么样啦？"]
    assert "面试" in engine.brain.requests[0].messages[-1]["content"]
    thread = impression_store(engine).get("qq_2388389247").threads[0]
    assert thread.asked_at, "问过的事记下来，不会反复追问"
    assert service.ledger.acts(GROUP)[0]["target"] == "qq_2388389247"


def test_she_may_decide_not_to_ask_and_nothing_is_sent(tmp_path):
    engine = _engine(tmp_path, reply="")
    service = InitiativeService(engine)

    asyncio.run(_run_follow_ups(service)(_message()))

    assert engine.sent == []
    assert service.ledger.acts(GROUP) == []


@pytest.mark.parametrize(
    "situation",
    ["already_replied", "busy_group", "other_bot", "hushed", "level_off"],
)
def test_she_holds_back_when_it_would_be_intrusive(tmp_path, situation):
    engine = _engine(tmp_path)
    service = InitiativeService(engine)
    message = _message()
    now = datetime.now(timezone.utc)
    if situation == "already_replied":
        engine.basic_memory.add_entry(
            GROUP,
            "assistant",
            "assistant",
            "欢迎回来～",
            timestamp=(now + timedelta(seconds=5)).isoformat(),
        )
    elif situation == "busy_group":
        for index in range(5):
            engine.basic_memory.add_entry(
                GROUP, f"qq_{index}", "human", f"热聊{index}", timestamp=now.isoformat()
            )
    elif situation == "other_bot":
        message = _message(sender_type="other_ai")
    elif situation == "hushed":
        service.ledger.hush(GROUP, now)
    elif situation == "level_off":
        engine.config = {"group_initiative_levels": {GROUP: "off"}}

    asyncio.run(_run_follow_ups(service)(message))

    assert engine.sent == []


def test_telling_her_to_be_quiet_silences_the_group(tmp_path):
    engine = _engine(tmp_path, mentions_bot=True)
    service = InitiativeService(engine)

    asyncio.run(_run_follow_ups(service)(_message("月白你安静点")))

    assert engine.sent == []
    assert service.ledger.hushed(GROUP, datetime.now(timezone.utc))
    reloaded = InitiativeLedger(tmp_path / "engine_state" / "initiative.json")
    assert reloaded.hushed(GROUP, datetime.now(timezone.utc)), "安静要求重启后仍然有效"
