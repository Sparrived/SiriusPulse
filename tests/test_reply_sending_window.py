from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.adapters.models import ParsedEvent
from sirius_pulse.config.models import ExpressivenessConfig
from sirius_pulse.core.cognition import CognitionAnalyzer
from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase
from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.core.pipeline import Pipeline
from sirius_pulse.core.rhythm import RhythmAnalyzer
from sirius_pulse.core.work_mode import WorkModeRun
from sirius_pulse.models.models import Message
from sirius_pulse.platforms.onebot_v11.napcat.adapter import (
    NapCatAdapter,
    _split_reply_for_delivery,
)

_DELAY_CONFIG = {
    "human_reply_chars_per_second": 10,
    "human_reply_min_delay_seconds": 0.5,
    "human_reply_max_delay_seconds": 2.0,
}

# 总字数超过 50，仍按换行拆成两条消息；第二行固定 10 个字符，等待 1 秒。
_LONG_MULTILINE_REPLY = "这是第一段比较长的回复内容，专门用来验证分段发送之间会等待一段时间，所以字数一定要足够多才行。" "\n" "1234567890"


@pytest.mark.asyncio
async def test_napcat_multiline_reply_waits_between_parts(monkeypatch):
    adapter = NapCatAdapter("ws://example.invalid", config=dict(_DELAY_CONFIG))
    sent: list[object] = []
    slept: list[float] = []

    async def fake_send_group_msg(group_id, message):
        sent.append(message)
        return {"ok": True}

    async def fake_sleep(seconds):
        slept.append(seconds)

    adapter.send_group_msg = fake_send_group_msg  # type: ignore[method-assign]
    monkeypatch.setattr(
        "sirius_pulse.platforms.onebot_v11.napcat.adapter.asyncio.sleep",
        fake_sleep,
    )

    ok = await adapter._send_group_text("100", _LONG_MULTILINE_REPLY)

    assert ok is True
    assert len(sent) == 2
    assert slept == [pytest.approx(1.0)]


@pytest.mark.asyncio
async def test_napcat_short_plain_multiline_reply_becomes_single_message():
    adapter = NapCatAdapter("ws://example.invalid", config=dict(_DELAY_CONFIG))
    sent: list[object] = []

    async def fake_send_group_msg(group_id, message):
        sent.append(message)
        return {"ok": True}

    adapter.send_group_msg = fake_send_group_msg  # type: ignore[method-assign]

    ok = await adapter._send_group_text("100", "第一行\n第二行\n第三行")

    assert ok is True
    assert sent == [[{"type": "text", "data": {"text": "第一行第二行第三行"}}]]


def test_split_reply_joins_a_sentence_broken_by_newline():
    text = "现在本猫天天监督他、偶尔咬他一口，\n还能名正言顺赖着，划算多啦，哼！"

    assert _split_reply_for_delivery(text) == [
        "现在本猫天天监督他、偶尔咬他一口，还能名正言顺赖着，划算多啦，哼！"
    ]


@pytest.mark.asyncio
async def test_napcat_mid_sentence_newline_is_sent_as_one_message():
    adapter = NapCatAdapter("ws://example.invalid", config=dict(_DELAY_CONFIG))
    sent: list[object] = []

    async def fake_send_group_msg(group_id, message):
        sent.append(message)
        return {"ok": True}

    adapter.send_group_msg = fake_send_group_msg  # type: ignore[method-assign]

    text = "现在本猫天天监督他、偶尔咬他一口，\n还能名正言顺赖着，划算多啦，哼！"
    ok = await adapter._send_group_text("100", text)

    assert ok is True
    assert sent == [
        [
            {
                "type": "text",
                "data": {
                    "text": "现在本猫天天监督他、偶尔咬他一口，还能名正言顺赖着，划算多啦，哼！"
                },
            }
        ]
    ]


def test_split_reply_uses_punctuation_without_relying_on_newlines():
    text = (
        "第一句足够长的回复内容在这里，超过二十个字。"
        "第二句足够长的回复内容也在这里，超过二十个字！"
    )

    assert _split_reply_for_delivery(text) == [
        "第一句足够长的回复内容在这里，超过二十个字。",
        "第二句足够长的回复内容也在这里，超过二十个字！",
    ]


def test_split_reply_does_not_break_at_commas_or_dunhao():
    text = "甲，乙、丙，丁。"

    assert _split_reply_for_delivery(text) == ["甲，乙、丙，丁。"]


@pytest.mark.asyncio
async def test_napcat_markdown_reply_still_sends_line_by_line():
    adapter = NapCatAdapter("ws://example.invalid", config=dict(_DELAY_CONFIG))
    sent: list[object] = []

    async def fake_send_group_msg(group_id, message):
        sent.append(message)
        return {"ok": True}

    adapter.send_group_msg = fake_send_group_msg  # type: ignore[method-assign]

    ok = await adapter._send_group_text("100", "# 标题\n正文")

    assert ok is True
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_napcat_delayed_partials_wait_between_each_sent_message(monkeypatch):
    adapter = NapCatAdapter(
        "ws://example.invalid",
        config={"allowed_group_ids": ["100"], **_DELAY_CONFIG},
    )
    sent: list[object] = []
    slept: list[float] = []

    async def fake_send_group_msg(group_id, message):
        sent.append(message)
        return {"ok": True}

    async def fake_sleep(seconds):
        slept.append(seconds)

    async def fake_tick_delayed_queue(
        group_id, on_partial_reply, *, adapter_type=None, adapter_route_id=None
    ):
        assert adapter_type == "napcat"
        assert adapter_route_id == ""
        await on_partial_reply("first")
        await on_partial_reply("second")
        return [{"reply": "final reply", "reply_references": [], "sticker_names": []}]

    adapter.send_group_msg = fake_send_group_msg  # type: ignore[method-assign]
    adapter._engine = SimpleNamespace(
        tick_delayed_queue=fake_tick_delayed_queue,
        _send_stickers_by_names=AsyncMock(),
    )
    monkeypatch.setattr(
        "sirius_pulse.platforms.onebot_v11.napcat.adapter.asyncio.sleep",
        fake_sleep,
    )

    await adapter._handle_event(
        SessionEvent(
            type=SessionEventType.DELAYED_RESPONSE_TRIGGERED,
            data={"group_id": "100"},
        )
    )

    assert len(sent) == 3
    assert slept == [pytest.approx(0.6), pytest.approx(1.1)]


@pytest.mark.asyncio
async def test_napcat_four_short_lines_are_merged_into_one_message():
    """超过三条的短句按句合并后仍是一条消息，不再碎成四个气泡。"""
    adapter = NapCatAdapter("ws://example.invalid", config=dict(_DELAY_CONFIG))
    sent: list[object] = []

    async def fake_send_group_msg(group_id, message):
        sent.append(message)
        return {"ok": True}

    adapter.send_group_msg = fake_send_group_msg  # type: ignore[method-assign]

    ok = await adapter._send_group_text("100", "第一行\n第二行\n第三行\n第四行")

    assert ok is True
    assert sent == [[{"type": "text", "data": {"text": "第一行第二行第三行第四行"}}]]


@pytest.mark.asyncio
async def test_napcat_four_line_reply_is_sent_as_two_messages():
    """四行短话按「攒满 20 字换句」合并后发两条，不再碎成四个气泡。"""
    adapter = NapCatAdapter("ws://example.invalid", config=dict(_DELAY_CONFIG))
    sent: list[object] = []

    async def fake_send_group_msg(group_id, message):
        sent.append(message)
        return {"ok": True}

    adapter.send_group_msg = fake_send_group_msg  # type: ignore[method-assign]

    ok = await adapter._send_group_text(
        "100",
        "才不给你看喵！\n" "万一把本猫的猫耳和尾巴重构丢了怎么办！\n" "而且现在指针全指向睡觉了，\n" "谁也别想动本猫的代码，呼噜噜——",
    )

    assert ok is True
    assert sent == [
        [
            {
                "type": "text",
                "data": {"text": "才不给你看喵！万一把本猫的猫耳和尾巴重构丢了怎么办！"},
            }
        ],
        [
            {
                "type": "text",
                "data": {"text": "而且现在指针全指向睡觉了，谁也别想动本猫的代码，呼噜噜——"},
            }
        ],
    ]


@pytest.mark.asyncio
async def test_napcat_delayed_short_multiline_reply_is_merged():
    adapter = NapCatAdapter(
        "ws://example.invalid",
        config={"allowed_group_ids": ["100"], **_DELAY_CONFIG},
    )
    sent: list[object] = []

    async def fake_send_group_msg(group_id, message):
        sent.append(message)
        return {"ok": True}

    async def fake_tick_delayed_queue(
        group_id, on_partial_reply, *, adapter_type=None, adapter_route_id=None
    ):
        return [{"reply": "第一行\n第二行", "reply_references": [], "sticker_names": []}]

    adapter.send_group_msg = fake_send_group_msg  # type: ignore[method-assign]
    adapter._engine = SimpleNamespace(
        tick_delayed_queue=fake_tick_delayed_queue,
        _send_stickers_by_names=AsyncMock(),
    )

    await adapter._handle_event(
        SessionEvent(
            type=SessionEventType.DELAYED_RESPONSE_TRIGGERED,
            data={"group_id": "100"},
        )
    )

    assert sent == [[{"type": "text", "data": {"text": "第一行第二行"}}]]


@pytest.mark.asyncio
async def test_napcat_forwards_inbound_message_while_multi_part_reply_is_sending(monkeypatch):
    """发送多段回复期间收到的新消息照常交给引擎，不再被发送窗口拦截或打标。"""
    adapter = NapCatAdapter("ws://example.invalid", config=dict(_DELAY_CONFIG))
    sent: list[object] = []

    async def fake_send_group_msg(group_id, message):
        sent.append(message)
        return {"ok": True}

    async def fake_sleep(seconds):
        # 两段之间的停顿里，群里来了新消息
        await adapter._on_group_message(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "100",
                "user_id": "200",
                "self_id": "300",
                "message": [{"type": "text", "data": {"text": "顺便问一下"}}],
            }
        )

    adapter.send_group_msg = fake_send_group_msg  # type: ignore[method-assign]
    adapter._engine = SimpleNamespace(is_ready=lambda: True)
    adapter._process_event = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        "sirius_pulse.platforms.onebot_v11.napcat.adapter.asyncio.sleep",
        fake_sleep,
    )

    ok = await adapter._send_group_text("100", _LONG_MULTILINE_REPLY)

    assert ok is True
    assert len(sent) == 2
    adapter._process_event.assert_awaited_once()
    event = adapter._process_event.await_args.args[0]
    assert event["group_id"] == "100"
    assert not [key for key in event if key.startswith("_sirius_received_during")]


def _engine_for_sending_window() -> (
    tuple[_EmotionalGroupChatEngineBase, list[tuple[str, Message, str]], list[str]]
):
    engine = object.__new__(_EmotionalGroupChatEngineBase)
    background_updates: list[tuple[str, Message, str]] = []
    persisted: list[str] = []

    engine.persona = SimpleNamespace(name="Luna", aliases=["月白"], reply_frequency="moderate")
    engine.config = {}
    engine._current_adapter_type = ""
    engine.expressiveness = ExpressivenessConfig()
    engine.cognition_analyzer = CognitionAnalyzer(ai_name="Luna", ai_aliases=["月白"])
    engine.cognition_store = SimpleNamespace(add=lambda **kwargs: None)
    engine.rhythm_analyzer = RhythmAnalyzer()
    engine._helpers = SimpleNamespace(get_recent_messages=lambda group_id, n: [])
    engine._last_reply_at = {}
    engine._topic_window = {}
    engine._topic_window_max_size = 10
    engine._delayed_event_emitted = {}
    engine.basic_memory = SimpleNamespace(get_context=lambda group_id, n: [])
    engine.event_bus = SimpleNamespace(emit=AsyncMock(return_value=True))
    engine.delayed_queue = DelayedResponseQueue()
    engine.assistant_emotion = SimpleNamespace(
        update_from_interaction=lambda emotion, user_id: None
    )
    engine.semantic_memory = SimpleNamespace(
        get_user_profile=lambda group_id, user_id: None,
        settle_engagement=lambda **kwargs: None,
        record_interaction=lambda **kwargs: None,
        set_user_profile_fields=lambda *args, **kwargs: None,
    )
    engine._persistence = SimpleNamespace(
        persist_group_state=lambda group_id: persisted.append(group_id)
    )
    engine._log_inner_thought = lambda *args, **kwargs: None
    engine._work_mode_runs = {}

    pipeline = Pipeline(engine)
    pipeline.perception = lambda group_id, message, participants: "u1"
    pipeline.background_update = (
        lambda group_id, message, emotion, intent, user_id: background_updates.append(
            (group_id, message, user_id)
        )
    )
    engine._pipeline = pipeline

    return engine, background_updates, persisted


def test_engine_scores_inbound_message_instead_of_using_a_send_window_gate():
    """同一时刻到达的消息只按自身内容评分，结果里不再出现发送窗口原因。"""
    engine, _, _ = _engine_for_sending_window()

    filler = engine.preview_dispatch(
        Message(role="user", content="今天大家下午有什么安排吗？", speaker="Alice"),
        [SimpleNamespace(user_id="u1", is_developer=False)],
        "group-1",
    )
    directed = engine.preview_dispatch(
        Message(role="user", content="Luna 这个报错怎么修？", speaker="Alice"),
        [SimpleNamespace(user_id="u1", is_developer=False)],
        "group-1",
    )

    assert directed["reason"] != "reply_send_window"
    assert filler["reason"] != "reply_send_window"
    assert directed["should_reply"] is True
    assert directed["score"] >= directed["threshold"]


@pytest.mark.asyncio
async def test_engine_when_directed_message_arrives_then_it_is_queued_by_score():
    engine, background_updates, persisted = _engine_for_sending_window()

    result = await engine.process_message(
        Message(role="user", content="Luna 这个报错怎么修？", speaker="Alice"),
        [SimpleNamespace(user_id="u1", is_developer=False)],
        "group-1",
    )

    pending = engine.delayed_queue.get_pending("group-1")
    assert result["strategy"] in {"immediate", "delayed"}
    assert len(pending) == 1
    assert pending[0].strategy_decision.reason not in {
        "received_during_bot_send_mention",
        "reply_send_window",
    }
    assert pending[0].strategy_decision.score >= pending[0].strategy_decision.threshold
    assert "Luna 这个报错怎么修？" in pending[0].message_content
    assert background_updates
    assert persisted == ["group-1"]


@pytest.mark.asyncio
async def test_engine_when_undirected_chatter_arrives_then_score_decides_silence():
    engine, background_updates, persisted = _engine_for_sending_window()

    result = await engine.process_message(
        Message(role="user", content="哈哈哈哈", speaker="Alice"),
        [SimpleNamespace(user_id="u1", is_developer=False)],
        "group-1",
    )

    assert result["strategy"] == "silent"
    assert engine.delayed_queue.get_pending("group-1") == []
    assert background_updates
    assert persisted == ["group-1"]


@pytest.mark.asyncio
async def test_engine_when_work_mode_is_running_then_message_is_stashed_not_queued():
    engine, background_updates, persisted = _engine_for_sending_window()
    run = WorkModeRun(group_id="group-1", goal="整理资料")
    engine.begin_work_mode("group-1", run)

    result = await engine.process_message(
        Message(role="user", content="你们聊什么呢", speaker="Alice"),
        [SimpleNamespace(is_developer=False)],
        "group-1",
    )

    assert result["strategy"] == "work_mode_stashed"
    assert engine.delayed_queue.get_pending("group-1") == []
    assert run.take_flushed() == []
    assert [message for message in run.stash] == ["你们聊什么呢"]
    assert background_updates
    assert persisted == []


@pytest.mark.asyncio
async def test_engine_when_work_mode_message_names_persona_then_whole_stash_is_released_once():
    engine, _, _ = _engine_for_sending_window()
    run = WorkModeRun(group_id="group-1", goal="整理资料")
    engine.begin_work_mode("group-1", run)

    await engine.process_message(
        Message(role="user", content="你们聊什么呢", speaker="Alice"),
        [SimpleNamespace(is_developer=False)],
        "group-1",
    )
    await engine.process_message(
        Message(role="user", content="顺便说一句", speaker="Bob"),
        [SimpleNamespace(is_developer=False)],
        "group-1",
    )
    await engine.process_message(
        Message(role="user", content="Luna 你弄完没有", speaker="Alice"),
        [SimpleNamespace(is_developer=False)],
        "group-1",
    )

    assert run.take_flushed() == ["你们聊什么呢", "顺便说一句", "Luna 你弄完没有"]
    assert run.take_flushed() == []

    engine.end_work_mode("group-1")
    assert engine.is_work_mode_active("group-1") is False


@pytest.mark.asyncio
async def test_engine_when_work_mode_ends_then_unanswered_messages_are_queued_again():
    """工作模式里没被点名的消息不能就这么没了：退出时排回队列，让她忙完接着回。"""
    engine, _, persisted = _engine_for_sending_window()
    run = WorkModeRun(group_id="group-1", goal="整理资料")
    engine.begin_work_mode("group-1", run)
    await engine.process_message(
        Message(role="user", content="你们聊什么呢", speaker="Alice"),
        [SimpleNamespace(is_developer=False)],
        "group-1",
    )
    await engine.process_message(
        Message(role="user", content="顺便说一句", speaker="Bob"),
        [SimpleNamespace(is_developer=False)],
        "group-1",
    )
    assert engine.delayed_queue.get_pending("group-1") == []

    engine.end_work_mode("group-1")

    pending = engine.delayed_queue.get_pending("group-1")
    assert len(pending) == 1
    assert "你们聊什么呢" in pending[0].message_content
    assert "顺便说一句" in pending[0].message_content
    # 她刚忙完，不必再等一个去抖窗口。
    assert pending[0].window_seconds == 0.0
    assert persisted == ["group-1"]
    assert engine.is_work_mode_active("group-1") is False


@pytest.mark.asyncio
async def test_engine_when_work_mode_messages_were_shown_then_they_are_not_queued_again():
    """已经补进她上下文的那批消息不该出来之后再回一遍。"""
    engine, _, _ = _engine_for_sending_window()
    run = WorkModeRun(group_id="group-1", goal="整理资料")
    engine.begin_work_mode("group-1", run)
    await engine.process_message(
        Message(role="user", content="你们聊什么呢", speaker="Alice"),
        [SimpleNamespace(is_developer=False)],
        "group-1",
    )
    await engine.process_message(
        Message(role="user", content="Luna 你弄完没有", speaker="Alice"),
        [SimpleNamespace(is_developer=False)],
        "group-1",
    )
    assert run.take_flushed() == ["你们聊什么呢", "Luna 你弄完没有"]

    engine.end_work_mode("group-1")

    assert engine.delayed_queue.get_pending("group-1") == []


@pytest.mark.asyncio
async def test_engine_tool_chain_injection_requires_explicit_persona_mention():
    engine, _, _ = _engine_for_sending_window()
    engine._active_tool_chain_groups = set()
    engine._tool_chain_messages = {}
    engine.begin_tool_chain("group-1")

    assert (
        await engine.inject_tool_chain_message(
            Message(role="user", content="普通群聊文本", speaker="Alice"),
            [SimpleNamespace(is_developer=False)],
            "group-1",
        )
        is False
    )
    assert (
        await engine.inject_tool_chain_message(
            Message(role="user", content="月白看这里", speaker="Alice"),
            [SimpleNamespace(is_developer=False)],
            "group-1",
        )
        is True
    )
    assert (
        await engine.inject_tool_chain_message(
            Message(role="user", content="Luna 也确认一下", speaker="Alice"),
            [SimpleNamespace(is_developer=False)],
            "group-1",
        )
        is True
    )
    assert (
        await engine.inject_tool_chain_message(
            Message(
                role="user",
                content="再看一下",
                speaker="Alice",
                mentions_current_bot=True,
            ),
            [SimpleNamespace(is_developer=False)],
            "group-1",
        )
        is True
    )
    assert [message.content for message in engine.pop_tool_chain_messages("group-1")] == [
        "月白看这里",
        "Luna 也确认一下",
        "再看一下",
    ]
    engine.end_tool_chain("group-1")


@pytest.mark.asyncio
async def test_engine_tool_chain_injection_active_during_work_mode():
    """工作模式开着时也认工具链窗口：点名的消息进下一轮，闲聊只暂存。"""
    engine, _, _ = _engine_for_sending_window()
    engine._active_tool_chain_groups = set()
    engine._tool_chain_messages = {}
    run = WorkModeRun(group_id="group-1", goal="整理资料")
    engine.begin_work_mode("group-1", run)

    assert engine.is_tool_chain_active("group-1") is True

    assert (
        await engine.inject_tool_chain_message(
            Message(role="user", content="月白看这里", speaker="Alice"),
            [SimpleNamespace(is_developer=False)],
            "group-1",
        )
        is True
    )
    assert [message.content for message in engine.pop_tool_chain_messages("group-1")] == ["月白看这里"]
    # 点名的消息进下一轮上下文，不留在退出时才重放的暂存里。
    assert run.stash == []

    await engine.observe_message(
        Message(role="user", content="今天天气真好", speaker="Bob"),
        [SimpleNamespace(is_developer=False)],
        "group-1",
    )

    assert run.stash == ["今天天气真好"]
    # 她正忙着，闲聊不该现在插一轮回复。
    assert engine.delayed_queue.get_pending("group-1") == []

    engine.end_work_mode("group-1")

    pending = engine.delayed_queue.get_pending("group-1")
    assert len(pending) == 1
    assert "今天天气真好" in pending[0].message_content
    assert engine.is_tool_chain_active("group-1") is False


@pytest.mark.asyncio
async def test_napcat_injects_explicit_group_text_before_normal_processing():
    adapter = NapCatAdapter("ws://example.invalid")
    parsed = ParsedEvent(
        group_id="100",
        user_id="200",
        self_id="300",
        message_type="group",
        prompt="月白继续看这个",
        nickname="Alice",
    )
    injected: list[Message] = []

    async def inject(message, participants, group_id):
        injected.append(message)
        return True

    adapter._engine = SimpleNamespace(
        is_ready=lambda: True,
        is_tool_chain_active=lambda group_id: group_id == "100",
        inject_tool_chain_message=inject,
    )

    async def fake_parse_event(event):
        return parsed

    adapter.parse_event = fake_parse_event  # type: ignore[method-assign]
    adapter._process_event = AsyncMock()

    await adapter._on_group_message(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": "100",
            "user_id": "200",
            "self_id": "300",
            "message": [{"type": "text", "data": {"text": "月白继续看这个"}}],
        }
    )

    assert [message.content for message in injected] == ["月白继续看这个"]
    adapter._process_event.assert_not_awaited()
