from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.core.bg_tasks_delayed import DelayedQueueTasks
from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision
from sirius_pulse.providers.base import ToolCall
from sirius_pulse.skills.models import SkillResult


def _decision(strategy: ResponseStrategy, *, urgency: float = 50.0) -> StrategyDecision:
    return StrategyDecision(strategy=strategy, urgency=urgency, reason="test")


def _past(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def test_delayed_queue_when_immediate_messages_share_group_then_merges_into_one_item():
    queue = DelayedResponseQueue()

    item = queue.enqueue(
        "group-1",
        "u1",
        "first",
        _decision(ResponseStrategy.IMMEDIATE),
        candidate_memories=["m1"],
        multimodal_inputs=[{"type": "image", "value": "a.png"}],
        channel="qq",
        channel_user_id="qq-1",
        speaker_name="Alice",
        platform_message_id="msg-1",
    )
    merged = queue.enqueue(
        "group-1",
        "u2",
        "second",
        _decision(ResponseStrategy.IMMEDIATE),
        emotion_state={"mood": "warm"},
        candidate_memories=["m2"],
        multimodal_inputs=[{"type": "image", "value": "b.png"}],
        channel="qq",
        channel_user_id="qq-2",
        speaker_name="Bob",
        platform_message_id="msg-2",
    )

    assert merged is item
    assert len(queue.get_pending("group-1")) == 1
    assert item.window_seconds == 6.0
    assert item.user_id == "u2"
    assert item.channel_user_id == "qq-2"
    assert item.related_user_ids == ["u1", "u2"]
    assert item.candidate_memories == ["m1", "m2"]
    assert item.multimodal_inputs == [
        {"type": "image", "value": "a.png"},
        {"type": "image", "value": "b.png"},
    ]
    assert "first" in item.message_content
    assert "second" in item.message_content


def test_delayed_queue_when_immediate_window_expires_then_triggers_item():
    queue = DelayedResponseQueue()
    item = queue.enqueue("group-1", "u1", "hello", _decision(ResponseStrategy.IMMEDIATE))
    item.enqueue_time = _past(item.window_seconds + 1)

    triggered = queue.tick("group-1", [])

    assert triggered == [item]
    assert item.status == "triggered"
    assert queue.has_pending("group-1") is False


def test_delayed_queue_when_topic_gap_exceeds_threshold_then_delayed_item_triggers_early():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "hello",
        _decision(ResponseStrategy.DELAYED, urgency=50),
        heat_level="cold",
    )
    item.enqueue_time = datetime.now(timezone.utc).isoformat()
    recent_messages = [{"timestamp": _past(6)}]

    triggered = queue.tick("group-1", recent_messages)

    assert triggered == [item]
    assert item.status == "triggered"


def test_delayed_queue_when_merging_incoming_then_appends_to_existing_pending_item():
    queue = DelayedResponseQueue()
    item = queue.enqueue("group-1", "u1", "first", _decision(ResponseStrategy.DELAYED))

    assert (
        queue.merge_incoming(
            "group-1",
            "u2",
            "second",
            speaker_name="Bob",
            channel_user_id="qq-2",
            multimodal_inputs=[{"type": "image", "value": "b.png"}],
        )
        is True
    )

    assert "second" in item.message_content
    assert item.related_user_ids == ["u1", "u2"]
    assert item.multimodal_inputs == [{"type": "image", "value": "b.png"}]
    assert queue.merge_incoming("missing", "u3", "third") is False


def test_delayed_queue_when_cancelled_or_cleared_then_pending_items_disappear():
    queue = DelayedResponseQueue()
    first = queue.enqueue("group-1", "u1", "first", _decision(ResponseStrategy.DELAYED))
    second = queue.enqueue("group-2", "u2", "second", _decision(ResponseStrategy.DELAYED))

    assert queue.cancel_all_for_user("group-1", "u1") == 1
    assert first.status == "cancelled"
    assert queue.get_pending("group-1") == []

    queue.clear_group("group-2")

    assert queue.get_pending("group-2") == []
    assert second.status == "pending"


def test_delayed_queue_when_corrupted_entry_exists_then_tick_filters_it_out():
    queue = DelayedResponseQueue()
    queue._queues["group-1"] = [{"bad": "entry"}]  # type: ignore[list-item]

    assert queue.tick("group-1", []) == []
    assert queue.get_pending("group-1") == []


@pytest.mark.asyncio
async def test_delayed_queue_when_tool_call_has_text_then_partial_leads_final_reply(monkeypatch):
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "check status",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)

    tool_call = ToolCall(
        id="call-1",
        function_name="lookup",
        function_arguments='{"query": "status"}',
    )
    chat_results = [
        SimpleNamespace(
            raw_text="I will check.",
            clean_text="I will check.",
            tool_calls=[tool_call],
            reply_references=[],
        ),
        SimpleNamespace(
            raw_text="Everything is ready.",
            clean_text="Everything is ready.",
            tool_calls=[],
            reply_references=[],
        ),
    ]
    skill = SimpleNamespace(name="lookup", silent=False, developer_only=False)
    profile = SimpleNamespace(name="Alice", is_developer=False)
    order: list[str] = []

    async def execute_skill(*args, **kwargs):
        order.append("tool")
        return SkillResult(success=True, data={"ok": True})

    engine = SimpleNamespace(
        config={"max_skill_rounds": 2, "partial_reply_lead_seconds": 1.5},
        delayed_queue=queue,
        _helpers=SimpleNamespace(
            get_recent_messages=lambda group_id, n: [],
            inject_multimodal_into_user_message=lambda messages, inputs: messages,
        ),
        rhythm_analyzer=SimpleNamespace(analyze=lambda group_id, recent: SimpleNamespace()),
        identity_resolver=SimpleNamespace(
            resolve_with_alias=lambda ctx, user_manager, group_id: SimpleNamespace(user_id="u1")
        ),
        user_manager=SimpleNamespace(
            get_user=lambda user_id, group_id: profile,
            entries={"group-1": {"u1": profile}},
        ),
        semantic_memory=SimpleNamespace(
            get_user_profile=lambda group_id, user_id: SimpleNamespace(engagement_rate=1.0)
        ),
        context_assembler=SimpleNamespace(
            build_messages_with_breakdown=lambda **kwargs: (
                [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "check status"},
                ],
                {},
            )
        ),
        brain=SimpleNamespace(chat=AsyncMock(side_effect=chat_results)),
        _skill_registry=SimpleNamespace(get=lambda name: skill),
        _skill_executor=SimpleNamespace(
            set_chat_context=lambda **kwargs: None,
            execute_async=AsyncMock(side_effect=execute_skill),
        ),
        _pending_biography={},
        _log_inner_thought=lambda text: None,
        event_bus=SimpleNamespace(emit=AsyncMock()),
    )
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content="check status",
        token_breakdown=None,
    )
    partials: list[str] = []
    partial_started = asyncio.Event()
    allow_partial_to_finish = asyncio.Event()

    async def capture_partial(text: str) -> None:
        order.append("partial")
        partials.append(text)
        partial_started.set()
        await allow_partial_to_finish.wait()

    slept: list[float] = []

    async def capture_sleep(seconds: float) -> None:
        order.append("lead_wait")
        slept.append(seconds)

    monkeypatch.setattr("sirius_pulse.core.bg_tasks_delayed.asyncio.sleep", capture_sleep)

    tick_task = asyncio.create_task(
        tasks.tick_delayed_queue("group-1", on_partial_reply=capture_partial)
    )
    await partial_started.wait()
    engine._skill_executor.execute_async.assert_not_awaited()
    allow_partial_to_finish.set()
    results = await tick_task

    assert partials == ["I will check."]
    assert order == ["partial", "tool", "lead_wait"]
    assert slept[0] == pytest.approx(1.5, abs=0.1)
    assert results[0]["reply"] == "Everything is ready."


@pytest.mark.asyncio
async def test_delayed_queue_when_partial_send_fails_then_tool_is_not_executed():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "check status",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)

    tool_call = ToolCall(
        id="call-1",
        function_name="lookup",
        function_arguments='{"query": "status"}',
    )
    skill = SimpleNamespace(name="lookup", silent=False, developer_only=False)
    profile = SimpleNamespace(name="Alice", is_developer=False)
    execute_skill = AsyncMock(return_value=SkillResult(success=True, data={"ok": True}))
    engine = SimpleNamespace(
        config={"max_skill_rounds": 2},
        delayed_queue=queue,
        _helpers=SimpleNamespace(
            get_recent_messages=lambda group_id, n: [],
            inject_multimodal_into_user_message=lambda messages, inputs: messages,
        ),
        rhythm_analyzer=SimpleNamespace(analyze=lambda group_id, recent: SimpleNamespace()),
        identity_resolver=SimpleNamespace(
            resolve_with_alias=lambda ctx, user_manager, group_id: SimpleNamespace(user_id="u1")
        ),
        user_manager=SimpleNamespace(
            get_user=lambda user_id, group_id: profile,
            entries={"group-1": {"u1": profile}},
        ),
        semantic_memory=SimpleNamespace(
            get_user_profile=lambda group_id, user_id: SimpleNamespace(engagement_rate=1.0)
        ),
        context_assembler=SimpleNamespace(
            build_messages_with_breakdown=lambda **kwargs: (
                [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "check status"},
                ],
                {},
            )
        ),
        brain=SimpleNamespace(
            chat=AsyncMock(
                return_value=SimpleNamespace(
                    raw_text="I will check.",
                    clean_text="I will check.",
                    tool_calls=[tool_call],
                    reply_references=[],
                )
            )
        ),
        _skill_registry=SimpleNamespace(get=lambda name: skill),
        _skill_executor=SimpleNamespace(
            set_chat_context=lambda **kwargs: None,
            execute_async=execute_skill,
        ),
        _pending_biography={},
        _log_inner_thought=lambda text: None,
        event_bus=SimpleNamespace(emit=AsyncMock()),
    )
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content="check status",
        token_breakdown=None,
    )

    async def fail_partial_send(text: str) -> None:
        raise RuntimeError("send failed")

    with pytest.raises(RuntimeError, match="send failed"):
        await tasks.tick_delayed_queue("group-1", on_partial_reply=fail_partial_send)

    execute_skill.assert_not_awaited()


@pytest.mark.asyncio
async def test_delayed_queue_when_send_sticker_tool_is_called_then_sticker_is_deferred():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "send sticker",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)

    tool_call = ToolCall(
        id="call-sticker",
        function_name="send_sticker",
        function_arguments='{"names": ["开心"]}',
    )
    skill = SimpleNamespace(name="send_sticker", silent=True, developer_only=False)
    profile = SimpleNamespace(name="Alice", is_developer=False)
    execute_skill = AsyncMock(return_value=SkillResult(success=True, data={"sent": True}))
    engine = SimpleNamespace(
        config={"max_skill_rounds": 2},
        delayed_queue=queue,
        _helpers=SimpleNamespace(
            get_recent_messages=lambda group_id, n: [],
            inject_multimodal_into_user_message=lambda messages, inputs: messages,
        ),
        rhythm_analyzer=SimpleNamespace(analyze=lambda group_id, recent: SimpleNamespace()),
        identity_resolver=SimpleNamespace(
            resolve_with_alias=lambda ctx, user_manager, group_id: SimpleNamespace(user_id="u1")
        ),
        user_manager=SimpleNamespace(
            get_user=lambda user_id, group_id: profile,
            entries={"group-1": {"u1": profile}},
        ),
        semantic_memory=SimpleNamespace(
            get_user_profile=lambda group_id, user_id: SimpleNamespace(engagement_rate=1.0)
        ),
        context_assembler=SimpleNamespace(
            build_messages_with_breakdown=lambda **kwargs: (
                [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "send sticker"},
                ],
                {},
            )
        ),
        brain=SimpleNamespace(
            chat=AsyncMock(
                return_value=SimpleNamespace(
                    raw_text="先说正文",
                    clean_text="先说正文",
                    tool_calls=[tool_call],
                    reply_references=[],
                    sticker_names=[],
                )
            )
        ),
        _skill_registry=SimpleNamespace(get=lambda name: skill),
        _skill_executor=SimpleNamespace(
            set_chat_context=lambda **kwargs: None,
            execute_async=execute_skill,
        ),
        _sticker_names=["开心"],
        _pending_biography={},
        _log_inner_thought=lambda text: None,
        event_bus=SimpleNamespace(emit=AsyncMock()),
    )
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content="send sticker",
        token_breakdown=None,
    )

    results = await tasks.tick_delayed_queue("group-1")

    execute_skill.assert_not_awaited()
    assert results[0]["reply"] == "先说正文"
    assert results[0]["sticker_names"] == ["开心"]
