"""自主性：意图驱动的判定、自主回合的产出与分享边界。

业务视角：
- 她因为"惦记着某件事"才行动，而不是因为空闲；
- 行动分两种：自己去弄明白（do），和把想说的话说给某个人（tell）；
- 说出去的话只讲一次，不会反复重播；
- 她说给谁由她自己定，且只会说给确实发得出去的地方。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sirius_pulse.core.autonomy import AutonomyPolicy
from sirius_pulse.core.intent import (
    RESOLUTION_TELL,
    IntentFileStore,
    Intention,
)
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.tools.builtin import autonomy, intend_share
from sirius_pulse.tools.executor import _self_initiated_block_reason
from sirius_pulse.tools.models import ToolDefinition, ToolSideEffect

_NOW = datetime.now(timezone.utc)


class _Store:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}
        self.saved = 0

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        self.saved += 1

    def reload(self):
        pass


def _make_ctx(
    tmp_path,
    *,
    messages: list[dict] | None = None,
    state: dict | None = None,
    groups: dict[str, list[dict]] | None = None,
    audiences: list[dict] | None = None,
) -> SimpleNamespace:
    store = _Store({"state": dict(state or {})})
    recorded: list = []
    delivered: list[dict] = []
    by_group = groups if groups is not None else {"group-1": list(messages or [])}
    reachable = audiences if audiences is not None else []

    async def run_autonomous_turn(**kwargs):
        recorded.append(kwargs)
        return {"text": "今天读到一篇讲潮汐的文章，记了两句。"}

    ctx = SimpleNamespace(
        get_data_store=lambda _name: store,
        get_active_groups=lambda: list(by_group),
        get_recent_messages=lambda gid, _n=10: list(by_group.get(gid, [])),
        run_autonomous_turn=run_autonomous_turn,
        add_memory_unit=lambda unit: recorded.append(unit) or True,
        get_persona=lambda: PersonaProfile(name="小星"),
        get_expressiveness=lambda: 0.5,
        get_work_path=lambda: str(tmp_path),
        log_inner_thought=lambda *_a, **_k: None,
        emit_event=_emit,
        list_audiences=lambda: list(reachable),
        deliver_share=_deliver(delivered),
    )
    ctx.store = store
    ctx.recorded = recorded
    ctx.delivered = delivered
    return ctx


def _deliver(sink: list[dict]):
    async def _deliver_share(*, audience, text, event_id="", adapter_type=""):
        sink.append({"audience": audience, "text": text, "event_id": event_id})
        return True

    return _deliver_share


async def _emit(*_args, **_kwargs) -> bool:
    return True


def _seed_intention(tmp_path, *, what="弄懂潮汐", resolution="do", audience="", **kwargs):
    store = IntentFileStore(tmp_path)
    intentions = store.load()
    intention = Intention.create(what=what, resolution=resolution, audience=audience, **kwargs)
    intentions.add(intention)
    store.save(intentions)
    return intention


# --- 判定：动机来自惦记，不来自空闲 -------------------------------------------------


def test_policy_does_nothing_without_any_intention(tmp_path):
    """空闲再久也不会凭空产生动机。"""
    decision = AutonomyPolicy().evaluate(seconds_since_episode=10_000_000)

    assert decision.should_act is False
    assert decision.reason == "no_intention"


def test_policy_pursues_an_existing_intention(tmp_path):
    _seed_intention(tmp_path, kind="reading", urgency=0.9)
    store = IntentFileStore(tmp_path).load()

    decision = AutonomyPolicy().evaluate(seconds_since_episode=60, intentions=store)

    assert decision.should_act is True
    assert decision.reason == "pursuing_intention"
    assert decision.kind == "reading"
    assert decision.resolution == "do"
    assert decision.seed == "弄懂潮汐"


def test_intention_fades_instead_of_being_revived_by_waiting(tmp_path):
    """陈旧意图会自然淡去：等待本身不能让一件事变重要。"""
    _seed_intention(tmp_path, urgency=0.5)
    store = IntentFileStore(tmp_path).load()
    store.all()[0].created_at = (_NOW - timedelta(days=30)).isoformat()

    decision = AutonomyPolicy().evaluate(seconds_since_episode=10_000_000, intentions=store)

    assert decision.should_act is False


# --- 自己去做 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_acts_whenever_she_is_carrying_something(tmp_path):
    """没有每日配额：只要她确实惦记着什么，就可以随时行动。"""
    ctx = _make_ctx(
        tmp_path,
        messages=[{"role": "user", "content": "https://example.com/tides"}],
        # 今天已经自主过很多次，也不应被计数挡住。
        state={"episode_count_date": _NOW.date().isoformat(), "episode_count_today": 99},
    )

    episode = await autonomy.run_tick(ctx)

    assert episode is not None
    assert episode.kind == "reading"
    assert ctx.recorded != []


@pytest.mark.asyncio
async def test_she_can_act_on_consecutive_heartbeats(tmp_path):
    """心跳之间没有额外冷却：只要每次都惦记着新东西，就都能行动。"""
    ctx = _make_ctx(
        tmp_path,
        messages=[{"role": "user", "content": "https://example.com/tides"}],
    )

    first = await autonomy.run_tick(ctx)
    assert first is not None

    # 紧接着的下一次心跳带来新素材，不该被"刚自主过"挡住。
    ctx.get_recent_messages = lambda _gid, _n=10: [
        {"role": "user", "content": "https://example.com/moons"}
    ]

    second = await autonomy.run_tick(ctx)

    assert second is not None
    assert second.seed.startswith("https://example.com/moons")


@pytest.mark.asyncio
async def test_one_unfinishable_intention_does_not_loop_forever(tmp_path):
    """同一件始终没结果的事不能每个心跳都烧一次模型调用。"""
    ctx = _make_ctx(
        tmp_path,
        messages=[{"role": "user", "content": "https://example.com/tides"}],
    )

    async def decline(**_kwargs):
        return {"text": "什么也不做"}

    ctx.run_autonomous_turn = decline

    # 前几次心跳允许尝试，之后这条意图就该被放下。
    for _ in range(5):
        await autonomy.run_tick(ctx)

    calls = len(ctx.recorded)
    assert calls <= 3, f"同一意图被反复重试了 {calls} 次"


@pytest.mark.asyncio
async def test_tick_records_episode_and_memory_without_sending_anything(tmp_path):
    ctx = _make_ctx(
        tmp_path,
        messages=[{"role": "user", "content": "https://example.com/tides"}],
    )

    episode = await autonomy.run_tick(ctx)

    assert episode is not None
    assert episode.kind == "reading"
    assert episode.outcome.startswith("今天读到")

    saved = json.loads((tmp_path / "memory" / "autonomy" / "episodes.json").read_text("utf-8"))
    assert saved["episodes"][0]["seed"].startswith("https://example.com/tides")

    unit = next(item for item in ctx.recorded if not isinstance(item, dict))
    assert unit.scope == "persona"
    assert unit.metadata["origin"] == "self_initiated"

    # 自主回合只产素材：不向任何群/私聊发送任何东西。
    assert ctx.delivered == []


@pytest.mark.asyncio
async def test_tick_skips_episode_when_persona_declines(tmp_path):
    ctx = _make_ctx(
        tmp_path,
        messages=[{"role": "user", "content": "https://example.com/tides"}],
    )

    async def decline(**_kwargs):
        return {"text": "什么也不做"}

    ctx.run_autonomous_turn = decline

    assert await autonomy.run_tick(ctx) is None
    assert not (tmp_path / "memory" / "autonomy" / "episodes.json").exists()


@pytest.mark.asyncio
async def test_tick_picks_material_from_most_recently_active_group(tmp_path):
    """活跃群列表按首次出现排序，素材要取最近真正聊过话的那个群。"""
    ctx = _make_ctx(
        tmp_path,
        groups={
            "group-old": [
                {"role": "user", "content": "旧的", "timestamp": "2026-01-01T00:00:00+00:00"}
            ],
            "group-new": [
                {
                    "role": "user",
                    "content": "https://example.com/tides",
                    "timestamp": "2026-09-01T00:00:00+00:00",
                }
            ],
        },
    )

    episode = await autonomy.run_tick(ctx)

    assert episode is not None
    assert episode.seed == "https://example.com/tides"
    assert ctx.recorded[0]["group_id"] == "group-new"


@pytest.mark.asyncio
async def test_encountering_something_plants_a_durable_intention(tmp_path):
    """动机产生于"看到了什么"，并且会留下来等下一次 tick。"""
    ctx = _make_ctx(
        tmp_path,
        messages=[{"role": "user", "content": "https://example.com/tides"}],
    )

    # 这一 tick 她决定不做，于是没有产出……
    async def decline(**_kwargs):
        return {"text": "什么也不做"}

    ctx.run_autonomous_turn = decline

    assert await autonomy.run_tick(ctx) is None

    # ……但"想弄明白"这件事已经记下来了，不会因为这次没做而消失。
    carried = IntentFileStore(tmp_path).load()
    assert [item.what for item in carried.all()] == ["https://example.com/tides"]


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

    assert episode is not None
    assert episode.kind == "share"
    assert ctx.delivered == [
        {
            "audience": "private_10001",
            "text": "今天的晚霞特别好看",
            "event_id": f"autonomy-share:{episode.intention_id}",
        }
    ]


@pytest.mark.asyncio
async def test_share_is_never_repeated(tmp_path):
    """被说过一次的内容不会再次重播，否则看起来像失忆。"""
    _seed_intention(
        tmp_path,
        what="今天的晚霞特别好看",
        resolution=RESOLUTION_TELL,
        audience="private_10001",
        urgency=0.9,
    )
    first = _make_ctx(tmp_path)
    await autonomy.run_tick(first)
    assert len(first.delivered) == 1

    # 后续 tick 反复经过同一个意图，也不应再说第二遍。
    for _ in range(3):
        later = _make_ctx(
            tmp_path,
            state={"last_share_at": "1970-01-01T00:00:00+00:00"},
        )
        await autonomy.run_tick(later)
        assert later.delivered == []


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
    _seed_intention(
        tmp_path,
        what="还有一件想说的",
        resolution=RESOLUTION_TELL,
        audience="private_10001",
        urgency=0.9,
    )
    ctx = _make_ctx(tmp_path, state={"last_share_at": _NOW.isoformat()})

    episode = await autonomy.run_tick(ctx)

    assert episode is None
    assert ctx.delivered == []


@pytest.mark.asyncio
async def test_share_without_audience_is_not_guessed(tmp_path):
    """她还没想好说给谁时，不能替她挑一个地方发出去。"""
    _seed_intention(
        tmp_path,
        what="有点想找人说说话",
        resolution=RESOLUTION_TELL,
        audience="",
        urgency=0.9,
    )
    ctx = _make_ctx(tmp_path)

    episode = await autonomy.run_tick(ctx)

    assert ctx.delivered == []
    assert episode is None or episode.kind != "share"


@pytest.mark.asyncio
async def test_she_can_write_down_something_she_wants_to_say(tmp_path):
    """她自己登记"想说的话"时，只登记、不发送。"""
    ctx = _make_ctx(
        tmp_path,
        audiences=[{"chat_id": "private_10001", "label": "主人（私聊）", "kind": "private"}],
    )
    ctx.list_audiences = lambda: [SimpleNamespace(chat_id="private_10001", label="主人（私聊）")]

    result = intend_share.run(
        what="我有点想你了",
        why="只是想说",
        audience="private_10001",
        engine_context=ctx,
    )

    assert result["success"] is True
    assert ctx.delivered == []

    carried = IntentFileStore(tmp_path).load()
    item = carried.all()[0]
    assert item.what == "我有点想你了"
    assert item.resolution == RESOLUTION_TELL
    assert item.audience == "private_10001"
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


def test_self_initiated_turn_allows_recording_tools():
    """只做登记的工具有明确豁免，否则她无法决定说给谁。"""
    recording = ToolDefinition(
        name="intend_share",
        description="",
        side_effect=ToolSideEffect.EXTERNAL_WRITE,
        allowed_when_self_initiated=True,
    )

    assert _self_initiated_block_reason(recording) == ""
