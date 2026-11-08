"""群调度不应吞掉插件命令。

三个回归点：
1. 未注册的 `/foo` 不能拿到 plugin_candidate 的 1.5 分；
2. 插件候选在回复冷却窗口内仍应被授予（不能被 observed/reply_cooldown 丢掉）；
3. 工作模式下 `/抽卡` 应直接执行插件，而不是被 stash。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase as EngineCore
from sirius_pulse.core.group_dispatcher import GroupDispatcher


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _dispatcher(path: Path, clock: _Clock) -> GroupDispatcher:
    return GroupDispatcher(
        path,
        worker_id="alpha",
        account_id="100",
        clock=clock,
        min_reply_interval_seconds=3,
        peer_cooldown_seconds=5,
    )


class _Registry:
    def match_message(self, text: str):
        return SimpleNamespace(plugin_name="gacha") if text.strip() == "/抽卡" else None


class _PreviewEngine:
    _plugin_registry = _Registry()
    config: dict = {}

    def _message_explicitly_mentions_current_bot(self, message) -> bool:
        return False

    def _compute_signal(self, *args, **kwargs):
        return SimpleNamespace(
            participation={
                "score": 0.1,
                "threshold": 0.66,
                "reason": "below_participation_threshold",
                "strategy": "silent",
                "delay_seconds": 0.0,
            },
            is_mentioned=False,
            urgency_score=0.0,
            directed_score=0.0,
        )

    def _pre_filter(self, *args, **kwargs) -> str:
        return "reject"


def _message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=text,
        adapter_type="napcat",
        sender_type="human",
        mentions_current_bot=False,
        speaker="tester",
        channel_user_id="1",
    )


def test_preview_dispatch_does_not_claim_unknown_slash_command():
    engine = _PreviewEngine()
    result = EngineCore.preview_dispatch(engine, _message("/unknown"), [], "g1")
    assert result["reason"] != "plugin_candidate"
    assert result["should_reply"] is False


def test_preview_dispatch_claims_registered_plugin_command():
    engine = _PreviewEngine()
    result = EngineCore.preview_dispatch(engine, _message("/抽卡"), [], "g1")
    assert result["reason"] == "plugin_candidate"
    assert result["score"] == 1.5


def test_admit_plugin_command_bypasses_reply_cooldown(tmp_path: Path):
    clock = _Clock()
    dispatcher = _dispatcher(tmp_path / "dispatcher.db", clock)

    first = dispatcher.admit(event_id="m1", group_id="g1")
    assert first.granted
    assert dispatcher.finish(first.lease_id, sent=True)

    clock.value += 1  # 仍在 3 秒回复冷却内
    command = dispatcher.admit(
        event_id="m2",
        group_id="g1",
        message_text="/抽卡",
    )
    assert command.granted
    assert command.reason == "selected"


def test_coordinate_plugin_candidate_bypasses_reply_cooldown(tmp_path: Path):
    async def run() -> None:
        clock = _Clock()
        dispatcher = _dispatcher(tmp_path / "dispatcher.db", clock)

        first = await dispatcher.coordinate(
            event_id="m1",
            group_id="g1",
            base_score=1.0,
            should_reply=True,
            message_text="你好",
        )
        assert first.granted
        assert dispatcher.finish(first.lease_id, sent=True)

        clock.value += 1  # 仍在 3 秒回复冷却内
        command = await dispatcher.coordinate(
            event_id="m2",
            group_id="g1",
            base_score=1.5,
            should_reply=True,
            reason="plugin_candidate",
            message_text="/抽卡",
        )
        assert command.granted
        assert command.worker_id == "alpha"

    asyncio.run(run())


class _WorkRun:
    def __init__(self) -> None:
        self.stashed: list[str] = []

    def stash_message(self, content, *, mentions_persona, replay) -> None:
        self.stashed.append(content)


class _WorkModeEngine:
    _plugin_registry = _Registry()
    _work_mode_runs: dict
    _current_adapter_type = ""

    def __init__(self) -> None:
        self.work_run = _WorkRun()
        self._work_mode_runs = {"g1": self.work_run}
        self.executed: list[str] = []
        self.event_bus = SimpleNamespace(emit=self._emit)

    async def _emit(self, event) -> None:
        return None

    def _perception(self, group_id, message, participants) -> str:
        return "qq_1"

    async def _caption_message_images(self, group_id, message) -> None:
        return None

    def _log_inner_thought(self, text: str) -> None:
        return None

    def _message_explicitly_mentions_current_bot(self, message) -> bool:
        return False

    def _background_update(self, *args, **kwargs) -> None:
        return None

    async def _execute_verified_plugin(self, plugin_result, message, group_id, user_id):
        self.executed.append(plugin_result.plugin_name)
        return {"strategy": "plugin", "reply": "ok"}


def test_work_mode_executes_plugin_command_instead_of_stashing():
    async def run() -> None:
        engine = _WorkModeEngine()
        participant = SimpleNamespace(is_developer=False)
        result = await EngineCore.process_message(
            engine,
            _message("/抽卡"),
            [participant],
            "g1",
        )
        assert result["strategy"] == "plugin"
        assert engine.executed == ["gacha"]
        assert engine.work_run.stashed == []

    asyncio.run(run())


def test_work_mode_still_stashes_unknown_slash_command():
    async def run() -> None:
        engine = _WorkModeEngine()
        participant = SimpleNamespace(is_developer=False)
        result = await EngineCore.process_message(
            engine,
            _message("/unknown"),
            [participant],
            "g1",
        )
        assert result["strategy"] == "work_mode_stashed"
        assert engine.executed == []
        assert engine.work_run.stashed == ["/unknown"]

    asyncio.run(run())
