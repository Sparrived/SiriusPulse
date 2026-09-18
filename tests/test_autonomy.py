"""自主性：纯规则判定 + 自主回合的产出与边界。

业务视角：人格会自己找事做，把结果留给自己；她不会因此往群里发消息。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sirius_pulse.core.autonomy import AutonomyPolicy, build_seed
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.tools.builtin import autonomy
from sirius_pulse.tools.executor import _self_initiated_block_reason
from sirius_pulse.tools.models import ToolDefinition, ToolSideEffect


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
    tmp_path, *, messages: list[dict] | None = None, state: dict | None = None
) -> SimpleNamespace:
    store = _Store({"state": dict(state or {})})
    recorded: list = []

    async def run_autonomous_turn(**kwargs):
        recorded.append(kwargs)
        return {"text": "今天读到一篇讲潮汐的文章，记了两句。"}

    ctx = SimpleNamespace(
        get_data_store=lambda _name: store,
        get_active_groups=lambda: ["group-1"],
        get_recent_messages=lambda _gid, _n=10: list(messages or []),
        run_autonomous_turn=run_autonomous_turn,
        add_memory_unit=lambda unit: recorded.append(unit) or True,
        get_persona=lambda: PersonaProfile(name="小星"),
        get_expressiveness=lambda: 0.5,
        get_work_path=lambda: str(tmp_path),
        log_inner_thought=lambda *_a, **_k: None,
        emit_event=_emit,
    )
    ctx.store = store
    ctx.recorded = recorded
    return ctx


async def _emit(*_args, **_kwargs) -> bool:
    return True


def test_policy_stays_silent_without_material():
    decision = AutonomyPolicy().evaluate(seconds_since_episode=10_000, seeds=[])

    assert decision.should_act is False
    assert decision.reason == "no_seed"


def test_policy_acts_on_idle_time_with_material():
    decision = AutonomyPolicy().evaluate(
        seconds_since_episode=10_000,
        seeds=[build_seed("https://example.com/tides")],
    )

    assert decision.should_act is True
    assert decision.kind == "reading"


@pytest.mark.asyncio
async def test_tick_does_nothing_when_daily_budget_is_spent(tmp_path):
    """预算用尽时既不调用模型，也不留下任何记录。"""
    ctx = _make_ctx(
        tmp_path,
        messages=[{"role": "user", "content": "https://example.com/tides"}],
        state={
            "episode_count_date": datetime.now(timezone.utc).date().isoformat(),
            "episode_count_today": 3,
            "last_attempt_at": "1970-01-01T00:00:00+00:00",
        },
    )

    episode = await autonomy.run_tick(ctx)

    assert episode is None
    assert ctx.recorded == []
    assert not (tmp_path / "memory" / "autonomy" / "episodes.json").exists()


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

    # 自主回合跑在"无会话上下文"里：只产素材，不向任何群/私聊发送。
    assert ctx.recorded[0]["group_id"] == "group-1"
    assert not hasattr(ctx, "dispatch_proactive_message")


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


def test_self_initiated_turn_refuses_delivery_tools():
    """自主时间禁止一切对外的工具，只读工具不受影响。"""
    sending = ToolDefinition(
        name="group_file_exec", description="", side_effect=ToolSideEffect.EXTERNAL_WRITE
    )
    reading = ToolDefinition(
        name="web_lookup", description="", side_effect=ToolSideEffect.READ_ONLY
    )

    assert _self_initiated_block_reason(sending) != ""
    assert _self_initiated_block_reason(reading) == ""
