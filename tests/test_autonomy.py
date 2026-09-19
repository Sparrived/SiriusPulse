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

from sirius_pulse.core.autonomy import AutonomyPolicy, is_quiet_hours
from sirius_pulse.core.intent import (
    RESOLUTION_TELL,
    IntentFileStore,
    Intention,
)
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.tools.builtin import autonomy, intend_pursue, intend_share
from sirius_pulse.tools.executor import _self_initiated_block_reason
from sirius_pulse.tools.models import ToolDefinition, ToolSideEffect

_REAL_NOW = datetime.now(timezone.utc)
_CN_TZ = timezone(timedelta(hours=8))

# Pin the tick clock to local noon so the night gate cannot make these tests pass
# or fail depending on when CI happens to run.  The shift always moves *forward*
# (to the next local noon), so a pinned "now" is never earlier than the real
# instant an intention was created at — otherwise its age, and therefore its
# urgency decay, would go negative.
_NOW = _REAL_NOW + timedelta(hours=(12 - _REAL_NOW.astimezone(_CN_TZ).hour) % 24)


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    """所有用例都在本地白天运行，避免夜间静默期让分享类断言随机失败。"""
    monkeypatch.setattr(autonomy, "_now", lambda: _NOW)


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
    config: dict | None = None,
) -> SimpleNamespace:
    data: dict = {"state": dict(state or {})}
    data.update(config or {})
    store = _Store(data)
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
    """空闲本身不会让一件不存在的事变重要（自由时间另有独立门槛）。"""
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
    _seed_intention(tmp_path, kind="reading", urgency=0.9)
    ctx = _make_ctx(tmp_path)

    episode = await autonomy.run_tick(ctx)

    assert episode is not None
    assert episode.kind == "reading"
    assert ctx.recorded != []


@pytest.mark.asyncio
async def test_she_can_act_on_consecutive_heartbeats(tmp_path):
    """心跳之间没有额外冷却：只要每次都惦记着新东西，就都能行动。"""
    first_intention = _seed_intention(tmp_path, what="弄懂潮汐", kind="reading", urgency=0.9)
    ctx = _make_ctx(tmp_path)

    first = await autonomy.run_tick(ctx)
    assert first is not None

    # 紧接着的下一次心跳前她又惦记上一件新事，不该被"刚自主过"挡住。
    store = IntentFileStore(tmp_path)
    intentions = store.load()
    intentions.drop(first_intention.intention_id)
    intentions.add(Intention.create(what="弄懂月亮", kind="reading", urgency=0.9))
    store.save(intentions)

    second = await autonomy.run_tick(ctx)

    assert second is not None
    assert second.seed == "弄懂月亮"


@pytest.mark.asyncio
async def test_one_unfinishable_intention_does_not_loop_forever(tmp_path):
    """同一件始终没结果的事不能每个心跳都烧一次模型调用。"""
    _seed_intention(tmp_path, kind="reading", urgency=0.9)
    ctx = _make_ctx(tmp_path)

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
    _seed_intention(tmp_path, what="https://example.com/tides", kind="reading", urgency=0.9)
    ctx = _make_ctx(tmp_path)

    episode = await autonomy.run_tick(ctx)

    assert episode is not None
    assert episode.kind == "reading"
    assert episode.outcome.startswith("今天读到")

    saved = json.loads((tmp_path / "memory" / "autonomy" / "episodes.json").read_text("utf-8"))
    assert saved["episodes"][0]["seed"] == "https://example.com/tides"

    unit = next(item for item in ctx.recorded if not isinstance(item, dict))
    assert unit.scope == "persona"
    assert unit.metadata["origin"] == "self_initiated"

    # 自主回合只产素材：不向任何群/私聊发送任何东西。
    assert ctx.delivered == []


@pytest.mark.asyncio
async def test_tick_skips_episode_when_persona_declines(tmp_path):
    _seed_intention(tmp_path, kind="reading", urgency=0.9)
    ctx = _make_ctx(tmp_path)

    async def decline(**_kwargs):
        return {"text": "什么也不做"}

    ctx.run_autonomous_turn = decline

    assert await autonomy.run_tick(ctx) is None
    assert not (tmp_path / "memory" / "autonomy" / "episodes.json").exists()


@pytest.mark.asyncio
async def test_pursuing_happens_in_the_group_she_was_in(tmp_path):
    """自主时间在她当初产生这件事的场景里进行，而不是随便挑一个群。"""
    _seed_intention(
        tmp_path,
        what="https://example.com/tides",
        kind="reading",
        urgency=0.9,
        origin_group="group-new",
    )
    ctx = _make_ctx(tmp_path, groups={"group-old": [{"role": "user", "content": "旧的"}]})

    episode = await autonomy.run_tick(ctx)

    assert episode is not None
    assert ctx.recorded[0]["group_id"] == "group-new"


@pytest.mark.asyncio
async def test_pursuing_falls_back_when_origin_group_is_gone(tmp_path):
    """来源群没了（比如退群）也不能因此就不做了。"""
    _seed_intention(tmp_path, what="弄懂潮汐", kind="reading", urgency=0.9)
    ctx = _make_ctx(tmp_path, groups={"group-1": []})

    episode = await autonomy.run_tick(ctx)

    assert episode is not None
    assert ctx.recorded[0]["group_id"] == "group-1"


# --- 动机从哪来 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_never_invents_motivation_from_the_chat_log(tmp_path):
    """心跳不翻群聊记录找素材：那些内容她在正常回复时已经处理过了。"""
    ctx = _make_ctx(
        tmp_path,
        messages=[
            {"role": "user", "content": "https://example.com/tides"},
            {"role": "user", "content": "这个重构你帮我看看"},
        ],
    )

    assert await autonomy.run_tick(ctx) is None

    # 既没有行动，也没有凭空登记出"想弄明白"的事。
    assert ctx.recorded == []
    assert IntentFileStore(tmp_path).load().all() == []


@pytest.mark.asyncio
async def test_she_can_write_down_something_she_wants_to_work_out(tmp_path):
    """她在真实对话里判断"这件事值得回头弄明白"时，只登记、不立刻去做。"""
    ctx = _make_ctx(tmp_path)

    result = intend_pursue.run(
        what="潮汐为什么一天有两次",
        why="刚才聊到一半没弄明白",
        kind="reading",
        urgency=0.8,
        engine_context=ctx,
    )

    assert result["success"] is True

    carried = IntentFileStore(tmp_path).load()
    item = carried.all()[0]
    assert item.what == "潮汐为什么一天有两次"
    assert item.why == "刚才聊到一半没弄明白"
    assert item.resolution == "do"
    assert item.kind == "reading"
    assert item.source == "intend_pursue"
    # 只登记：这一次回复里不去做。
    assert ctx.recorded == []


@pytest.mark.asyncio
async def test_a_recorded_intention_is_pursued_on_a_later_heartbeat(tmp_path):
    """登记下来的事会留下来等心跳，并在那时被真正推进。"""
    ctx = _make_ctx(tmp_path)
    intend_pursue.run(what="潮汐为什么一天有两次", why="想弄懂", engine_context=ctx)

    episode = await autonomy.run_tick(ctx)

    assert episode is not None
    assert episode.seed == "潮汐为什么一天有两次"
    assert episode.resolution == "do"


def test_intend_pursue_refuses_without_engine_context():
    """没有运行上下文时不能假装登记成功。"""
    assert intend_pursue.run(what="弄懂潮汐")["success"] is False
    assert intend_pursue.run(what="", engine_context=SimpleNamespace())["success"] is False


# --- 没人找她的时候，她也能开始 -----------------------------------------------------


@pytest.mark.asyncio
async def test_first_tick_does_not_hand_her_free_time(tmp_path):
    """刚启动不等于"被冷落很久"：不能一重启就白送一段自由时间。"""
    ctx = _make_ctx(tmp_path)

    assert await autonomy.run_tick(ctx) is None
    assert ctx.recorded == []


@pytest.mark.asyncio
async def test_she_starts_something_herself_after_being_left_alone(tmp_path):
    """没有别人说话、也没有任何意图时，久到一定程度她仍能自己开始。"""
    ctx = _make_ctx(
        tmp_path,
        state={"last_free_time_at": (_NOW - timedelta(hours=4)).isoformat()},
    )

    episode = await autonomy.run_tick(ctx)

    assert episode is not None
    # 这一段是她自己的，不挂在任何意图上。
    assert episode.intention_id == ""
    assert ctx.recorded[0]["free_time"] is True
    assert ctx.recorded[0]["seed"] == ""


@pytest.mark.asyncio
async def test_she_may_still_do_nothing_with_free_time(tmp_path):
    """自由时间不是必须产出：她可以拒绝，且拒绝同样消耗掉这次机会。"""
    ctx = _make_ctx(
        tmp_path,
        state={"last_free_time_at": (_NOW - timedelta(hours=4)).isoformat()},
    )

    async def decline(**_kwargs):
        return {"text": "什么也不做"}

    ctx.run_autonomous_turn = decline

    assert await autonomy.run_tick(ctx) is None
    assert not (tmp_path / "memory" / "autonomy" / "episodes.json").exists()

    # 紧接着的下一个心跳不再重复提供，否则每个心跳都会烧一次调用。
    ctx2 = _make_ctx(tmp_path)
    ctx2.store.data["state"] = ctx.store.data["state"]
    assert await autonomy.run_tick(ctx2) is None


@pytest.mark.asyncio
async def test_free_time_is_paced_by_its_own_interval(tmp_path):
    """间隔没到就不提供：默认不会每个心跳都给一段自由时间。"""
    ctx = _make_ctx(
        tmp_path,
        state={"last_free_time_at": (_NOW - timedelta(minutes=30)).isoformat()},
    )

    assert await autonomy.run_tick(ctx) is None
    assert ctx.recorded == []


@pytest.mark.asyncio
async def test_free_time_can_be_turned_off_entirely(tmp_path):
    """设成 0 就退回纯意图闸门：不惦记任何事时永远不会开始。"""
    ctx = _make_ctx(
        tmp_path,
        state={"last_free_time_at": (_NOW - timedelta(days=30)).isoformat()},
        config={"free_time_interval_seconds": 0},
    )

    assert await autonomy.run_tick(ctx) is None
    assert ctx.recorded == []


@pytest.mark.asyncio
async def test_free_time_records_episode_and_memory_without_sending_anything(tmp_path):
    """自由时间的产出照常留档，但同样不向任何地方发送。"""
    ctx = _make_ctx(
        tmp_path,
        state={"last_free_time_at": (_NOW - timedelta(hours=4)).isoformat()},
    )

    episode = await autonomy.run_tick(ctx)

    assert episode is not None
    saved = json.loads((tmp_path / "memory" / "autonomy" / "episodes.json").read_text("utf-8"))
    assert saved["episodes"][0]["outcome"] == episode.outcome

    unit = next(item for item in ctx.recorded if not isinstance(item, dict))
    assert unit.metadata["origin"] == "self_initiated"
    assert ctx.delivered == []


@pytest.mark.asyncio
async def test_she_can_start_a_new_thread_while_nothing_is_carried(tmp_path):
    """自由时间里冒出的新念头要能登记下来，否则线索在回合结束时就断了。"""
    ctx = _make_ctx(
        tmp_path,
        state={"last_free_time_at": (_NOW - timedelta(hours=4)).isoformat()},
    )

    async def writes_it_down(**kwargs):
        ctx.recorded.append(kwargs)
        intend_pursue.run(
            what="潮汐为什么一天有两次",
            why="刚才自己翻资料时想到的",
            engine_context=ctx,
        )
        return {"text": "我翻了点东西，还留了个想接着弄明白的问题。"}

    ctx.run_autonomous_turn = writes_it_down

    episode = await autonomy.run_tick(ctx)

    assert episode is not None
    carried = IntentFileStore(tmp_path).load()
    assert [item.what for item in carried.all()] == ["潮汐为什么一天有两次"]

    # 下一次心跳就能接着推进这条她自己开出来的线索。
    ctx2 = _make_ctx(tmp_path)
    ctx2.store.data["state"] = ctx.store.data["state"]
    follow_up = await autonomy.run_tick(ctx2)
    assert follow_up is not None
    assert follow_up.seed == "潮汐为什么一天有两次"


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


def test_both_recording_tools_are_allowed_on_her_own_time():
    """做和说都要能登记，否则她的自由时间只能攒出"想说的"，攒不出"想做的"。"""
    assert intend_share.TOOL_META["allowed_when_self_initiated"] is True
    assert intend_pursue.TOOL_META["allowed_when_self_initiated"] is True
    assert intend_pursue.TOOL_META["model_visible"] is True


# --- 夜间：可以做事，但不可以发出去 -----------------------------------------------


def _cn(hour: int, minute: int = 0) -> datetime:
    """本地（中国）某个整点对应的 UTC 时刻。"""
    return datetime(2026, 3, 1, hour, minute, tzinfo=_CN_TZ).astimezone(timezone.utc)


@pytest.mark.parametrize(
    "moment",
    [_cn(23, 0), _cn(23, 59), _cn(0, 0), _cn(3, 30), _cn(7, 59)],
)
def test_sending_is_blocked_through_the_night(moment):
    """23:00 到次日 08:00 之间一律不发送，含跨零点两侧。"""
    assert is_quiet_hours(moment) is True


@pytest.mark.parametrize("moment", [_cn(8, 0), _cn(12, 0), _cn(22, 59)])
def test_sending_is_allowed_outside_the_night(moment):
    """08:00 整点即恢复，22:59 仍在窗口之外。"""
    assert is_quiet_hours(moment) is False


def test_night_window_is_read_in_china_time_not_utc():
    """判定按中国时间，而不是 UTC，否则静默期会整整错开八小时。"""
    # 中国的凌晨 3 点 == UTC 前一天 19 点，恰好是 UTC 的"白天"。
    assert is_quiet_hours(_cn(3)) is True
    assert is_quiet_hours(datetime(2026, 3, 1, 19, tzinfo=timezone.utc)) is True


@pytest.mark.asyncio
async def test_she_can_still_work_on_things_at_night(tmp_path, monkeypatch):
    """夜里她照样可以自己想事情：静默的只是"发出去"。"""
    _seed_intention(tmp_path, what="弄懂潮汐", kind="reading", urgency=0.9)
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(3))
    ctx = _make_ctx(tmp_path)

    episode = await autonomy.run_tick(ctx)

    assert episode is not None
    assert ctx.recorded[0]["kind"] == "reading"


@pytest.mark.asyncio
async def test_a_message_written_at_night_waits_until_morning(tmp_path, monkeypatch):
    """夜里想说的话不会被丢掉，也不会被标记成已说，只是等到早上再发。"""
    intention = _seed_intention(
        tmp_path,
        what="今天的晚霞特别好看",
        resolution=RESOLUTION_TELL,
        audience="private_10001",
        urgency=0.9,
    )
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(23, 30))
    night = _make_ctx(tmp_path)
    await autonomy.run_tick(night)

    assert night.delivered == []

    # 到了早上，同一条意图仍然待发，且一个字都没改。
    monkeypatch.setattr(autonomy, "_now", lambda: _cn(8, 5))
    morning = _make_ctx(tmp_path, state=night.store.data["state"])
    episode = await autonomy.run_tick(morning)

    assert len(morning.delivered) == 1
    assert morning.delivered[0]["text"] == "今天的晚霞特别好看"
    assert episode is not None and episode.kind == "share"
    carried = IntentFileStore(tmp_path).load()
    assert carried.get(intention.intention_id).shared_at != ""


# --- 天亮时不会一次轰炸 -----------------------------------------------------------


def _seed_pending_shares(tmp_path, count: int) -> None:
    """夜里攒下若干条待发的话（不设上限，她想记多少记多少）。"""
    for i in range(count):
        _seed_intention(
            tmp_path,
            what=f"夜里第{i + 1}件想说的",
            resolution=RESOLUTION_TELL,
            audience="private_10001",
            urgency=0.9,
        )


def test_she_may_record_any_number_of_messages_at_night(tmp_path):
    """夜里登记不设上限：她晚上做了多少事是她自己的事。

    三条就足以证伪"上限为 1"，不必写更多——每次登记都是一次原子落盘，
    条数越多越容易撞上 Windows 上杀软抢占重命名的已知抖动。
    """
    ctx = _make_ctx(tmp_path)

    for i in range(3):
        assert intend_share.run(what=f"第{i + 1}条", engine_context=ctx)["success"] is True

    assert len(IntentFileStore(tmp_path).load().all()) == 3


@pytest.mark.asyncio
async def test_a_night_of_backlog_does_not_burst_at_dawn(tmp_path, monkeypatch):
    """要紧的是 08:00 不能轰炸：积压 8 条时，天亮后第一个小时也只发一条。"""
    _seed_pending_shares(tmp_path, 8)
    clock = [_cn(7, 45)]
    monkeypatch.setattr(autonomy, "_now", lambda: clock[0])
    ctx = _make_ctx(tmp_path)

    for _ in range(4):  # 08:00 起按 15 分钟心跳走满一小时
        clock[0] += timedelta(minutes=15)
        await autonomy.run_tick(ctx)

    assert len(ctx.delivered) == 1


@pytest.mark.asyncio
async def test_the_backlog_drains_one_per_cooldown_interval(tmp_path, monkeypatch):
    """积压按 share_cooldown_seconds 一条条摊开，不会攒到某一刻一起倒出来。"""
    _seed_pending_shares(tmp_path, 3)
    clock = [_cn(7, 45)]
    monkeypatch.setattr(autonomy, "_now", lambda: clock[0])
    ctx = _make_ctx(tmp_path)

    for _ in range(4 * 3):  # 三小时，每 15 分钟一跳
        clock[0] += timedelta(minutes=15)
        await autonomy.run_tick(ctx)

    assert len(ctx.delivered) == 3
    assert ctx.delivered[0]["text"] == "夜里第1件想说的"
