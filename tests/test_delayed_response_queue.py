from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.core.bg_tasks_delayed import (
    DelayedQueueTasks,
    _build_assistant_tool_message,
)
from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.core.events import SessionEventType
from sirius_pulse.core.pipeline import Pipeline
from sirius_pulse.core.plan_runtime import start_plan_session, update_plan_progress
from sirius_pulse.core.prompt_factory import StyleAdapter
from sirius_pulse.models.models import Message
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision
from sirius_pulse.models.signal import SignalAnalysis
from sirius_pulse.providers.base import ToolCall
from sirius_pulse.tools.models import ToolResult


def _decision(strategy: ResponseStrategy, *, urgency: float = 50.0) -> StrategyDecision:
    return StrategyDecision(strategy=strategy, urgency=urgency, reason="test")


def _past(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def test_assistant_tool_message_when_reasoning_exists_then_keeps_it_private_to_message_chain():
    message = _build_assistant_tool_message(
        "visible progress",
        [ToolCall(id="call-1", function_name="lookup", function_arguments='{"q":"x"}')],
        "private reasoning",
    )

    assert message["content"] == "visible progress"
    assert message["reasoning_content"] == "private reasoning"
    assert message["tool_calls"][0]["function"]["name"] == "lookup"


def _agent_tool_tasks(
    queue,
    tool,
    chat_results,
    execute_tool,
    *,
    max_tool_rounds: int = 2,
):
    profile = SimpleNamespace(name="Alice", is_developer=False)
    engine = SimpleNamespace(
        config={
            "max_tool_rounds": max_tool_rounds,
        },
        delayed_queue=queue,
        _helpers=SimpleNamespace(
            get_recent_messages=lambda group_id, n: [],
            inject_multimodal_into_user_message=lambda messages, inputs: messages,
        ),
        rhythm_analyzer=SimpleNamespace(analyze=lambda group_id, recent: SimpleNamespace()),
        identity_resolver=SimpleNamespace(
            resolve_with_alias=lambda ctx, user_manager, group_id, **kwargs: SimpleNamespace(
                user_id="u1"
            )
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
                    {"role": "user", "content": kwargs["current_query"]},
                ],
                {},
            )
        ),
        brain=SimpleNamespace(chat=AsyncMock(side_effect=chat_results)),
        _tool_registry=SimpleNamespace(get=lambda name: tool),
        _tool_executor=SimpleNamespace(
            set_chat_context=lambda **kwargs: None,
            execute_async=execute_tool,
        ),
        _log_inner_thought=lambda text: None,
        event_bus=SimpleNamespace(emit=AsyncMock()),
    )
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content="request",
        token_breakdown=None,
        dynamic_context="",
    )
    return tasks, engine


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
        candidate_memories=["m2"],
        multimodal_inputs=[{"type": "image", "value": "b.png"}],
        channel="qq",
        channel_user_id="qq-2",
        speaker_name="Bob",
        platform_message_id="msg-2",
    )

    assert merged is item
    assert len(queue.get_pending("group-1")) == 1
    assert item.window_seconds == 0.0
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


def test_delayed_queue_keeps_same_group_adapter_partitions_independent():
    queue = DelayedResponseQueue()
    napcat_item = queue.enqueue(
        "group-1",
        "u1",
        "from napcat",
        _decision(ResponseStrategy.IMMEDIATE),
        adapter_type="napcat",
    )
    discord_item = queue.enqueue(
        "group-1",
        "u2",
        "from discord",
        _decision(ResponseStrategy.IMMEDIATE),
        adapter_type="discord",
    )

    assert napcat_item is not discord_item
    assert queue.has_pending("group-1", adapter_type="napcat")
    assert queue.has_pending("group-1", adapter_type="discord")
    assert len(queue.get_pending("group-1", adapter_type="napcat")) == 1
    assert len(queue.get_pending("group-1", adapter_type="discord")) == 1

    triggered = queue.tick("group-1", [], adapter_type="napcat")

    assert triggered == [napcat_item]
    assert queue.get_pending("group-1", adapter_type="napcat") == []
    assert queue.get_pending("group-1", adapter_type="discord") == [discord_item]


def test_delayed_queue_keeps_same_type_adapter_instances_independent():
    queue = DelayedResponseQueue()
    first = queue.enqueue(
        "group-1",
        "u1",
        "from account one",
        _decision(ResponseStrategy.IMMEDIATE),
        adapter_type="napcat",
        adapter_route_id="napcat:100",
    )
    second = queue.enqueue(
        "group-1",
        "u2",
        "from account two",
        _decision(ResponseStrategy.IMMEDIATE),
        adapter_type="napcat",
        adapter_route_id="napcat:200",
    )
    legacy = queue.enqueue(
        "group-1",
        "u3",
        "from a legacy bridge",
        _decision(ResponseStrategy.IMMEDIATE),
        adapter_type="napcat",
    )

    assert first is not second
    assert legacy is not first
    assert legacy is not second
    assert queue.get_pending("group-1", adapter_route_id="napcat:100") == [first]
    assert queue.get_pending("group-1", adapter_route_id="napcat:200") == [second]

    triggered = queue.tick(
        "group-1",
        [],
        adapter_type="napcat",
        adapter_route_id="napcat:100",
    )

    assert triggered == [first]
    assert queue.get_pending("group-1", adapter_route_id="napcat:100") == []
    assert queue.get_pending("group-1", adapter_route_id="napcat:200") == [second]

    legacy_triggered = queue.tick(
        "group-1",
        [],
        adapter_type="napcat",
        adapter_route_id="",
    )
    assert legacy_triggered == [legacy]
    assert queue.get_pending("group-1", adapter_route_id="napcat:200") == [second]


@pytest.mark.asyncio
async def test_tool_chain_injects_explicit_group_text_into_next_model_round():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "look up the status",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)

    tool_call = ToolCall(
        id="call-1",
        function_name="lookup",
        function_arguments='{"q":"status"}',
    )
    execute_tool = AsyncMock()
    tasks, engine = _agent_tool_tasks(
        queue,
        SimpleNamespace(name="lookup", silent=False, developer_only=False),
        [
            SimpleNamespace(
                raw_text="",
                clean_text="",
                tool_calls=[tool_call],
                reply_references=[],
                injected_request={},
            ),
            SimpleNamespace(
                raw_text="处理完成",
                clean_text="处理完成",
                tool_calls=[],
                reply_references=[],
                injected_request={},
            ),
        ],
        execute_tool,
    )
    active: set[str] = set()
    pending_messages: list[SimpleNamespace] = []
    engine.begin_tool_chain = lambda group_id: active.add(group_id)
    engine.end_tool_chain = lambda group_id: active.discard(group_id)

    def pop_tool_chain_messages(group_id):
        messages = pending_messages[:]
        pending_messages.clear()
        return messages

    engine.pop_tool_chain_messages = pop_tool_chain_messages

    async def execute_and_receive_message(*args, **kwargs):
        assert "group-1" in active
        pending_messages.append(
            SimpleNamespace(
                content="Luna 结果也发我",
                speaker="Bob",
                channel_user_id="u2",
                message_id="m2",
            )
        )
        return ToolResult(success=True, data={"ok": True})

    execute_tool.side_effect = execute_and_receive_message

    results = await tasks.tick_delayed_queue("group-1")

    second_request = engine.brain.chat.await_args_list[1].args[0]
    assert any(
        message.get("role") == "user" and "Luna 结果也发我" in message.get("content", "")
        for message in second_request.messages
    )
    assert results[0]["reply"] == "处理完成"
    assert active == set()


@pytest.mark.asyncio
async def test_tool_chain_window_closes_when_provider_fails_mid_chain():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "look up the status",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)

    tool_call = ToolCall(
        id="call-1",
        function_name="lookup",
        function_arguments='{"q":"status"}',
    )
    execute_tool = AsyncMock(return_value=ToolResult(success=True, data={"ok": True}))
    tasks, engine = _agent_tool_tasks(
        queue,
        SimpleNamespace(name="lookup", silent=False, developer_only=False),
        [
            SimpleNamespace(
                raw_text="",
                clean_text="",
                tool_calls=[tool_call],
                reply_references=[],
                injected_request={},
            ),
            RuntimeError("提供商响应内容为空。"),
        ],
        execute_tool,
    )
    active: set[str] = set()
    engine.begin_tool_chain = active.add
    engine.end_tool_chain = active.discard
    engine.pop_tool_chain_messages = lambda group_id: []

    with pytest.raises(RuntimeError):
        await tasks.tick_delayed_queue("group-1")

    assert active == set()


def test_delayed_queue_when_immediate_is_enqueued_then_triggers_without_waiting():
    queue = DelayedResponseQueue()
    item = queue.enqueue("group-1", "u1", "hello", _decision(ResponseStrategy.IMMEDIATE))

    triggered = queue.tick("group-1", [])

    assert item.window_seconds == 0.0
    assert triggered == [item]
    assert item.status == "triggered"
    assert queue.has_pending("group-1") is False


@pytest.mark.asyncio
async def test_pipeline_when_strategy_is_immediate_then_notifies_delivery_in_same_turn():
    queue = DelayedResponseQueue()
    event_bus = SimpleNamespace(emit=AsyncMock())
    engine = SimpleNamespace(
        delayed_queue=queue,
        event_bus=event_bus,
        _persist_group_state=lambda group_id: None,
        assistant_emotion=SimpleNamespace(update_from_interaction=lambda emotion, user_id: None),
        semantic_memory=SimpleNamespace(
            settle_engagement=lambda **kwargs: None,
            record_interaction=lambda **kwargs: None,
        ),
    )
    signal = SignalAnalysis(
        is_mentioned=True,
        urgency_score=80.0,
        relevance_score=0.8,
        participation={
            "strategy": "immediate",
            "reason": "addressed",
            "score": 1.0,
            "threshold": 0.5,
            "delay_seconds": 0.0,
        },
    )

    result = await Pipeline(engine).generate(
        signal,
        Message(role="user", content="hello"),
        "group-1",
        "u1",
    )

    pending = queue.get_pending("group-1")
    assert result["strategy"] == "immediate"
    assert len(pending) == 1
    assert pending[0].window_seconds == 0.0
    event_bus.emit.assert_awaited_once()
    event = event_bus.emit.await_args.args[0]
    assert event.type == SessionEventType.DELAYED_RESPONSE_TRIGGERED
    assert event.data == {
        "group_id": "group-1",
        "item_id": pending[0].item_id,
        "adapter_type": "",
        "reason": "immediate",
    }


@pytest.mark.asyncio
async def test_pipeline_preserves_adapter_instance_route_in_delayed_event():
    queue = DelayedResponseQueue()
    event_bus = SimpleNamespace(emit=AsyncMock(return_value=True))
    engine = SimpleNamespace(
        delayed_queue=queue,
        event_bus=event_bus,
        _persist_group_state=lambda group_id: None,
        assistant_emotion=SimpleNamespace(update_from_interaction=lambda emotion, user_id: None),
        semantic_memory=SimpleNamespace(
            settle_engagement=lambda **kwargs: None,
            record_interaction=lambda **kwargs: None,
        ),
    )
    signal = SignalAnalysis(
        is_mentioned=True,
        urgency_score=80.0,
        relevance_score=0.8,
        participation={
            "strategy": "immediate",
            "reason": "addressed",
            "score": 1.0,
            "threshold": 0.5,
            "delay_seconds": 0.0,
        },
    )

    await Pipeline(engine).generate(
        signal,
        Message(
            role="user",
            content="hello",
            adapter_type="napcat",
            adapter_route_id="napcat:100",
        ),
        "group-1",
        "u1",
    )

    event = event_bus.emit.await_args.args[0]
    assert event.data["adapter_type"] == "napcat"
    assert event.data["adapter_route_id"] == "napcat:100"
    pending = queue.get_pending("group-1", adapter_route_id="napcat:100")
    assert len(pending) == 1


def test_delayed_queue_when_hard_immediate_then_still_has_no_wait_window():
    queue = DelayedResponseQueue()
    decision = _decision(ResponseStrategy.IMMEDIATE)
    decision.context["hard_immediate"] = True

    item = queue.enqueue("group-1", "u1", "hello", decision)

    assert item.window_seconds == 0.0


def test_delayed_queue_when_estimated_delay_is_set_then_limits_window():
    queue = DelayedResponseQueue()
    decision = _decision(ResponseStrategy.DELAYED, urgency=50)
    decision.estimated_delay_seconds = 12.0

    item = queue.enqueue("group-1", "u1", "hello", decision, heat_level="hot")

    assert item.window_seconds == 12.0


def test_delayed_queue_when_freshness_ttl_expires_then_cancels_item():
    queue = DelayedResponseQueue()
    decision = _decision(ResponseStrategy.DELAYED, urgency=20)
    decision.context["freshness_ttl_seconds"] = 6.0
    item = queue.enqueue("group-1", "u1", "hello", decision)
    item.enqueue_time = _past(7)

    triggered = queue.tick("group-1", [])

    assert triggered == []
    assert item.status == "cancelled"
    assert queue.has_pending("group-1") is False


def test_delayed_queue_when_pending_is_promoted_then_becomes_immediate():
    queue = DelayedResponseQueue()
    item = queue.enqueue("group-1", "u1", "hello", _decision(ResponseStrategy.DELAYED))

    promoted = queue.promote_pending(
        "group-1",
        max_window_seconds=0.0,
        reason="explicit_mention",
    )

    assert promoted is item
    assert item.window_seconds == 0.0
    assert item.strategy_decision.strategy == ResponseStrategy.IMMEDIATE
    assert item.strategy_decision.reason == "explicit_mention"
    assert item.strategy_decision.context["hard_immediate"] is True


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


def test_build_delayed_prompt_injects_configured_length_limit():
    engine = SimpleNamespace(
        config={"max_sentence_chars": 12},
        semantic_memory=SimpleNamespace(
            get_user_profile=lambda *args, **kwargs: None,
            get_group_profile=lambda *args, **kwargs: SimpleNamespace(atmosphere_history=[]),
        ),
        style_adapter=StyleAdapter(),
        persona=SimpleNamespace(
            max_tokens_preference=None,
            temperature_preference=None,
        ),
        _other_ai_names=[],
        _tool_registry=None,
        _plugin_registry=None,
    )
    item = SimpleNamespace(
        message_content="hello",
        speaker_name="Alice",
        channel_user_id="u1",
        related_user_ids=[],
        candidate_memories=[],
    )

    bundle = DelayedQueueTasks(engine)._build_delayed_prompt(item, "group-1")

    assert "【回复规范】" in bundle.system_prompt
    assert "【回复长度】" not in bundle.system_prompt
    assert "每句话尽量不超过 12 个字" in bundle.system_prompt
    assert "少于 40 字保持单段" in bundle.system_prompt
    assert "不要用换行制造停顿" in bundle.system_prompt


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
async def test_delayed_queue_when_tool_call_has_text_then_keeps_part_in_chain_before_final(
    monkeypatch,
):
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
    tool = SimpleNamespace(name="lookup", silent=False, developer_only=False, retry_safe=False)
    profile = SimpleNamespace(name="Alice", is_developer=False)
    order: list[str] = []

    async def execute_tool(*args, **kwargs):
        order.append("tool")
        return ToolResult(success=True, data={"ok": True})

    engine = SimpleNamespace(
        config={
            "max_tool_rounds": 2,
            "partial_reply_lead_seconds": 1.5,
            "tool_execution_timeout": 12,
        },
        delayed_queue=queue,
        _helpers=SimpleNamespace(
            get_recent_messages=lambda group_id, n: [],
            inject_multimodal_into_user_message=lambda messages, inputs: messages,
        ),
        rhythm_analyzer=SimpleNamespace(analyze=lambda group_id, recent: SimpleNamespace()),
        identity_resolver=SimpleNamespace(
            resolve_with_alias=lambda ctx, user_manager, group_id, **kwargs: SimpleNamespace(
                user_id="u1"
            )
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
        _tool_registry=SimpleNamespace(get=lambda name: tool),
        _tool_executor=SimpleNamespace(
            set_chat_context=lambda **kwargs: None,
            execute_async=AsyncMock(side_effect=execute_tool),
        ),
        _log_inner_thought=lambda text: None,
        event_bus=SimpleNamespace(emit=AsyncMock()),
    )
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content="check status",
        token_breakdown=None,
        dynamic_context="",
    )
    partials: list[str] = []

    async def capture_partial(text: str) -> None:
        order.append("partial")
        partials.append(text)

    slept: list[float] = []

    async def capture_sleep(seconds: float) -> None:
        order.append("lead_wait")
        slept.append(seconds)

    monkeypatch.setattr("sirius_pulse.core.bg_tasks_delayed.asyncio.sleep", capture_sleep)

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=capture_partial)

    assert partials == []
    assert order == ["tool"]
    assert slept == []
    assert results[0]["reply"] == "Everything is ready."
    execute_kwargs = engine._tool_executor.execute_async.await_args.kwargs
    assert execute_kwargs["timeout"] == 12
    assert execute_kwargs["max_retries"] == 0
    second_request = engine.brain.chat.await_args_list[1].args[0]
    assistant_message = next(
        message for message in second_request.messages if message["role"] == "assistant"
    )
    assert assistant_message["content"] == "I will check."
    tool_message = next(message for message in second_request.messages if message["role"] == "tool")
    assert tool_message["content"].startswith("[Tool result: success]")
    assert "reference data" in tool_message["content"]
    turn_events = [
        call.args[0].data
        for call in engine.event_bus.emit.await_args_list
        if call.args[0].type.value == "agent_turn_updated"
    ]
    assert turn_events[-1]["phase"] == "complete"


@pytest.mark.asyncio
async def test_delayed_queue_when_tool_chain_has_many_statuses_then_keeps_them_in_chain():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "check status",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)

    def chat_result(index: int, *, final: bool = False):
        if final:
            return SimpleNamespace(
                raw_text="Everything is ready.",
                clean_text="Everything is ready.",
                tool_calls=[],
                reply_references=[],
            )
        return SimpleNamespace(
            raw_text=f"Checking step {index}.",
            clean_text=f"Checking step {index}.",
            tool_calls=[
                ToolCall(
                    id=f"call-{index}",
                    function_name="lookup",
                    function_arguments=f'{{"step": {index}}}',
                )
            ],
            reply_references=[],
        )

    tool = SimpleNamespace(name="lookup", silent=False, developer_only=False)
    tasks, engine = _agent_tool_tasks(
        queue,
        tool,
        [chat_result(1), chat_result(2), chat_result(3), chat_result(4, final=True)],
        AsyncMock(return_value=ToolResult(success=True, data={"ok": True})),
        max_tool_rounds=4,
    )
    partials: list[str] = []

    async def capture_partial(text: str) -> None:
        partials.append(text)

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=capture_partial)

    assert partials == []
    assert results[0]["reply"] == "Everything is ready."
    final_request = engine.brain.chat.await_args_list[-1].args[0]
    assert [
        message["content"] for message in final_request.messages if message["role"] == "assistant"
    ] == ["Checking step 1.", "Checking step 2.", "Checking step 3."]


@pytest.mark.asyncio
async def test_delayed_queue_when_tool_fails_then_sends_the_next_model_output():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "check status",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)
    tool_calls = [
        ToolCall(
            id=f"call-{index}",
            function_name="lookup",
            function_arguments=f'{{"step": {index}}}',
        )
        for index in (1, 2)
    ]
    tool = SimpleNamespace(name="lookup", silent=False, developer_only=False, retry_safe=False)
    tasks, engine = _agent_tool_tasks(
        queue,
        tool,
        [
            SimpleNamespace(
                raw_text="I will check.",
                clean_text="I will check.",
                tool_calls=[tool_calls[0]],
                reply_references=[],
            ),
            SimpleNamespace(
                raw_text="The lookup failed, so I will try another way.",
                clean_text="The lookup failed, so I will try another way.",
                tool_calls=[tool_calls[1]],
                reply_references=[],
            ),
            SimpleNamespace(
                raw_text="I could not finish the check.",
                clean_text="I could not finish the check.",
                tool_calls=[],
                reply_references=[],
            ),
        ],
        AsyncMock(
            side_effect=[
                ToolResult(
                    success=False,
                    error="connection refused at https://10.0.0.7:8443 token=secret",
                ),
                ToolResult(success=True, data={"ok": True}),
            ]
        ),
    )
    partials: list[str] = []

    async def capture_partial(text: str) -> None:
        partials.append(text)

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=capture_partial)

    assert partials == ["The lookup failed, so I will try another way."]
    assert results[0]["reply"] == "I could not finish the check."
    second_request = engine.brain.chat.await_args_list[1].args[0]
    assistant_message = next(
        message for message in second_request.messages if message["role"] == "assistant"
    )
    assert assistant_message["content"] == "I will check."
    tool_message = next(message for message in second_request.messages if message["role"] == "tool")
    assert (
        tool_message["content"]
        == ToolResult(
            success=False,
            error="connection refused at https://10.0.0.7:8443 token=secret",
        ).to_model_text()
    )
    third_request = engine.brain.chat.await_args_list[2].args[0]
    assert [
        message["content"] for message in third_request.messages if message["role"] == "assistant"
    ] == ["I will check.", "The lookup failed, so I will try another way."]


@pytest.mark.asyncio
async def test_delayed_queue_when_silent_tool_fails_then_still_requests_issue_output():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "send the file",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)
    tool_call = ToolCall(
        id="call-1",
        function_name="group_file_exec",
        function_arguments='{"action": "image", "file_id": "f1"}',
    )
    tasks, _engine = _agent_tool_tasks(
        queue,
        SimpleNamespace(name="group_file_exec", silent=False, developer_only=False),
        [
            SimpleNamespace(
                raw_text="",
                clean_text="",
                tool_calls=[tool_call],
                reply_references=[],
            ),
            SimpleNamespace(
                raw_text="The file tool failed, so I could not send it.",
                clean_text="The file tool failed, so I could not send it.",
                tool_calls=[],
                reply_references=[],
            ),
        ],
        AsyncMock(return_value=ToolResult(success=False, error="upload timeout")),
    )
    partials: list[str] = []

    async def capture_partial(text: str) -> None:
        partials.append(text)

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=capture_partial)

    assert partials == []
    assert results[0]["reply"] == "The file tool failed, so I could not send it."


@pytest.mark.asyncio
async def test_delayed_queue_when_tool_chain_hits_limit_then_model_writes_final_reply():
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
    tasks, engine = _agent_tool_tasks(
        queue,
        SimpleNamespace(name="lookup", silent=False, developer_only=False, retry_safe=False),
        [
            SimpleNamespace(
                raw_text="",
                clean_text="",
                tool_calls=[tool_call],
                reply_references=[],
            ),
            SimpleNamespace(
                raw_text="我完成了能完成的检查，但还有一部分没有完成。",
                clean_text="我完成了能完成的检查，但还有一部分没有完成。",
                tool_calls=[],
                reply_references=[],
            ),
        ],
        AsyncMock(
            return_value=ToolResult(
                success=False,
                error="connection refused at https://10.0.0.7:8443 token=secret",
            )
        ),
        max_tool_rounds=0,
    )

    results = await tasks.tick_delayed_queue("group-1")

    assert results[0]["reply"] == "我完成了能完成的检查，但还有一部分没有完成。"
    final_request = engine.brain.chat.await_args_list[1].args[0]
    assert final_request.enable_tools is True
    assert final_request.extra_tools is None
    assert final_request.tool_choice == "none"
    assert final_request.messages[-1]["role"] == "user"
    assert "达到工具调用轮次上限" in final_request.messages[-1]["content"]
    assert (
        "token=secret"
        in next(message for message in final_request.messages if message["role"] == "tool")[
            "content"
        ]
    )


@pytest.mark.asyncio
async def test_delayed_queue_executes_high_risk_tool_without_confirmation():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1", "u1", "remove this member", _decision(ResponseStrategy.IMMEDIATE)
    )
    item.enqueue_time = _past(item.window_seconds + 1)
    tool_call = ToolCall(
        id="call-risky",
        function_name="group_management",
        function_arguments='{"action": "kick", "user_id": 42}',
    )
    tool = SimpleNamespace(
        name="group_management",
        silent=False,
        developer_only=False,
        retry_safe=False,
        side_effect="destructive",
    )
    execute_tool = AsyncMock(return_value=ToolResult(success=True, data={"ok": True}))
    tasks, engine = _agent_tool_tasks(
        queue,
        tool,
        [
            SimpleNamespace(
                raw_text="", clean_text="", tool_calls=[tool_call], reply_references=[]
            ),
            SimpleNamespace(
                raw_text="Done.", clean_text="Done.", tool_calls=[], reply_references=[]
            ),
        ],
        execute_tool,
    )

    results = await tasks.tick_delayed_queue("group-1")

    assert results[0]["reply"] == "Done."
    assert execute_tool.await_args.args[1] == {"action": "kick", "user_id": 42}


@pytest.mark.asyncio
async def test_delayed_queue_when_chat_round_has_no_completion_control_tool():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "say once",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)

    seen_extra_tools: list[set[str]] = []

    async def capture_chat(request):
        seen_extra_tools.append({tool["function"]["name"] for tool in (request.extra_tools or [])})
        return SimpleNamespace(
            raw_text="One reply.",
            clean_text="One reply.",
            tool_calls=[],
            reply_references=[],
        )

    profile = SimpleNamespace(name="Alice", is_developer=False)
    engine = SimpleNamespace(
        config={"max_tool_rounds": 3},
        delayed_queue=queue,
        _helpers=SimpleNamespace(
            get_recent_messages=lambda group_id, n: [],
            inject_multimodal_into_user_message=lambda messages, inputs: messages,
        ),
        rhythm_analyzer=SimpleNamespace(analyze=lambda group_id, recent: SimpleNamespace()),
        identity_resolver=SimpleNamespace(
            resolve_with_alias=lambda ctx, user_manager, group_id, **kwargs: SimpleNamespace(
                user_id="u1"
            )
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
                    {"role": "user", "content": "say once"},
                ],
                {},
            )
        ),
        brain=SimpleNamespace(chat=AsyncMock(side_effect=capture_chat)),
        _tool_registry=None,
        _tool_executor=None,
        _log_inner_thought=lambda text: None,
        event_bus=SimpleNamespace(emit=AsyncMock()),
    )
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content="say once",
        token_breakdown=None,
        dynamic_context="",
    )

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=AsyncMock())

    assert results[0]["reply"] == "One reply."
    assert seen_extra_tools == [set()]


@pytest.mark.asyncio
async def test_delayed_queue_when_normal_tool_part_is_suppressed_then_tool_still_executes():
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
    tool = SimpleNamespace(name="lookup", silent=False, developer_only=False)
    profile = SimpleNamespace(name="Alice", is_developer=False)
    execute_tool = AsyncMock(return_value=ToolResult(success=True, data={"ok": True}))
    engine = SimpleNamespace(
        config={"max_tool_rounds": 0},
        delayed_queue=queue,
        _helpers=SimpleNamespace(
            get_recent_messages=lambda group_id, n: [],
            inject_multimodal_into_user_message=lambda messages, inputs: messages,
        ),
        rhythm_analyzer=SimpleNamespace(analyze=lambda group_id, recent: SimpleNamespace()),
        identity_resolver=SimpleNamespace(
            resolve_with_alias=lambda ctx, user_manager, group_id, **kwargs: SimpleNamespace(
                user_id="u1"
            )
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
        _tool_registry=SimpleNamespace(get=lambda name: tool),
        _tool_executor=SimpleNamespace(
            set_chat_context=lambda **kwargs: None,
            execute_async=execute_tool,
        ),
        _log_inner_thought=lambda text: None,
        event_bus=SimpleNamespace(emit=AsyncMock()),
    )
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content="check status",
        token_breakdown=None,
        dynamic_context="",
    )

    async def fail_if_called(text: str) -> None:
        raise AssertionError(f"normal tool part should not be sent: {text}")

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=fail_if_called)

    execute_tool.assert_awaited_once()
    assert results[0]["reply"] == "本轮工具调用上限已到，部分操作尚未完成。"


@pytest.mark.asyncio
async def test_delayed_queue_when_enter_plan_then_intermediate_text_is_hidden():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "design a complex plan",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)

    enter_plan = ToolCall(
        id="call-enter-plan",
        function_name="enter_plan",
        function_arguments='{"goal": "design a complex plan", "reason": "needs tools"}',
    )
    update_progress = ToolCall(
        id="call-update-progress",
        function_name="update_plan_progress",
        function_arguments=(
            '{"phase": "verifying", "summary": "Checking the public API", ' '"confidence": "high"}'
        ),
    )
    exit_plan = ToolCall(
        id="call-exit-plan",
        function_name="exit_plan",
        function_arguments='{"final_message": "Here is the final plan.", "send_to_group": true}',
    )
    chat_results = [
        SimpleNamespace(
            raw_text="I need to work this out.",
            clean_text="I need to work this out.",
            tool_calls=[enter_plan],
            reply_references=[],
        ),
        SimpleNamespace(
            raw_text="",
            clean_text="",
            tool_calls=[update_progress],
            reply_references=[],
        ),
        SimpleNamespace(
            raw_text="",
            clean_text="",
            tool_calls=[exit_plan],
            reply_references=[],
        ),
    ]
    profile = SimpleNamespace(name="Alice", is_developer=False)
    engine = SimpleNamespace(
        config={
            "max_tool_rounds": 3,
            "enable_tools": True,
            "plan_mode_enabled": True,
            "plan_mode_limit_normal_tools": True,
        },
        delayed_queue=queue,
        _helpers=SimpleNamespace(
            get_recent_messages=lambda group_id, n: [],
            inject_multimodal_into_user_message=lambda messages, inputs: messages,
        ),
        rhythm_analyzer=SimpleNamespace(analyze=lambda group_id, recent: SimpleNamespace()),
        identity_resolver=SimpleNamespace(
            resolve_with_alias=lambda ctx, user_manager, group_id, **kwargs: SimpleNamespace(
                user_id="u1"
            )
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
                    {"role": "user", "content": "design a complex plan"},
                ],
                {},
            )
        ),
        brain=SimpleNamespace(chat=AsyncMock(side_effect=chat_results)),
        _tool_registry=None,
        _tool_executor=None,
        _active_plan_sessions={},
        _log_inner_thought=lambda text: None,
        event_bus=SimpleNamespace(emit=AsyncMock()),
    )
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content="design a complex plan",
        token_breakdown=None,
        dynamic_context="",
    )
    partials: list[str] = []

    async def capture_partial(text: str) -> None:
        partials.append(text)

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=capture_partial)

    assert partials == []
    assert results[0]["reply"] == "Here is the final plan."
    assert engine._active_plan_sessions == {}
    first_request = engine.brain.chat.await_args_list[0].args[0]
    second_request = engine.brain.chat.await_args_list[1].args[0]
    assert first_request.enable_tools is False
    assert second_request.enable_tools is True
    assert "enter_plan" in {tool["function"]["name"] for tool in (first_request.extra_tools or [])}
    assert "exit_plan" in {tool["function"]["name"] for tool in (second_request.extra_tools or [])}
    assert "abort_plan" in {tool["function"]["name"] for tool in (second_request.extra_tools or [])}
    assert "update_plan_progress" in {
        tool["function"]["name"] for tool in (second_request.extra_tools or [])
    }
    assert "continue" not in {
        tool["function"]["name"] for tool in (second_request.extra_tools or [])
    }
    assert "隐藏计划模式" in second_request.system_prompt
    turn_events = [
        call.args[0].data
        for call in engine.event_bus.emit.await_args_list
        if call.args[0].type.value == "agent_turn_updated"
    ]
    assert any("plan" in event["phases"] for event in turn_events)

    third_request = engine.brain.chat.await_args_list[2].args[0]
    assert any(
        msg.get("role") == "tool" and msg.get("content") == "Public planning progress updated."
        for msg in third_request.messages
    )


@pytest.mark.asyncio
async def test_delayed_queue_when_plan_aborts_then_session_is_cleared_without_reply():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "dangerous request",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)

    enter_plan = ToolCall(
        id="call-enter-plan",
        function_name="enter_plan",
        function_arguments='{"goal": "dangerous request"}',
    )
    abort_plan = ToolCall(
        id="call-abort-plan",
        function_name="abort_plan",
        function_arguments='{"reason": "cancelled", "send_to_group": false}',
    )
    profile = SimpleNamespace(name="Alice", is_developer=False)
    engine = SimpleNamespace(
        config={
            "max_tool_rounds": 3,
            "enable_tools": True,
            "plan_mode_enabled": True,
            "plan_mode_limit_normal_tools": True,
        },
        delayed_queue=queue,
        _helpers=SimpleNamespace(
            get_recent_messages=lambda group_id, n: [],
            inject_multimodal_into_user_message=lambda messages, inputs: messages,
        ),
        rhythm_analyzer=SimpleNamespace(analyze=lambda group_id, recent: SimpleNamespace()),
        identity_resolver=SimpleNamespace(
            resolve_with_alias=lambda ctx, user_manager, group_id, **kwargs: SimpleNamespace(
                user_id="u1"
            )
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
                    {"role": "user", "content": "dangerous request"},
                ],
                {},
            )
        ),
        brain=SimpleNamespace(
            chat=AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        raw_text="",
                        clean_text="",
                        tool_calls=[enter_plan],
                        reply_references=[],
                    ),
                    SimpleNamespace(
                        raw_text="",
                        clean_text="",
                        tool_calls=[abort_plan],
                        reply_references=[],
                    ),
                ]
            )
        ),
        _tool_registry=None,
        _tool_executor=None,
        _active_plan_sessions={},
        _log_inner_thought=lambda text: None,
        event_bus=SimpleNamespace(emit=AsyncMock()),
    )
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content="dangerous request",
        token_breakdown=None,
        dynamic_context="",
    )

    results = await tasks.tick_delayed_queue("group-1")

    assert results[0]["reply"] == ""
    assert engine._active_plan_sessions == {}


@pytest.mark.asyncio
async def test_delayed_queue_when_plan_presence_enabled_then_sends_status_once():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "design a complex plan",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)

    enter_plan = ToolCall(
        id="call-enter-plan",
        function_name="enter_plan",
        function_arguments='{"goal": "design a complex plan"}',
    )
    exit_plan = ToolCall(
        id="call-exit-plan",
        function_name="exit_plan",
        function_arguments='{"final_message": "done", "send_to_group": true}',
    )
    profile = SimpleNamespace(name="Alice", is_developer=False)
    engine = SimpleNamespace(
        config={
            "max_tool_rounds": 3,
            "enable_tools": True,
            "plan_mode_enabled": True,
            "plan_mode_limit_normal_tools": True,
            "plan_mode_presence_enabled": True,
            "plan_mode_presence_min_interval_seconds": 45,
        },
        delayed_queue=queue,
        _helpers=SimpleNamespace(
            get_recent_messages=lambda group_id, n: [],
            inject_multimodal_into_user_message=lambda messages, inputs: messages,
        ),
        rhythm_analyzer=SimpleNamespace(analyze=lambda group_id, recent: SimpleNamespace()),
        identity_resolver=SimpleNamespace(
            resolve_with_alias=lambda ctx, user_manager, group_id, **kwargs: SimpleNamespace(
                user_id="u1"
            )
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
                    {"role": "user", "content": "design a complex plan"},
                ],
                {},
            )
        ),
        brain=SimpleNamespace(
            chat=AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        raw_text="hidden text",
                        clean_text="hidden text",
                        tool_calls=[enter_plan],
                        reply_references=[],
                    ),
                    SimpleNamespace(
                        raw_text="我先捋一下思路，马上回来。",
                        clean_text="我先捋一下思路，马上回来。",
                        tool_calls=[],
                        reply_references=[],
                    ),
                    SimpleNamespace(
                        raw_text="",
                        clean_text="",
                        tool_calls=[exit_plan],
                        reply_references=[],
                    ),
                ]
            )
        ),
        _tool_registry=None,
        _tool_executor=None,
        _active_plan_sessions={},
        _log_inner_thought=lambda text: None,
        event_bus=SimpleNamespace(emit=AsyncMock()),
    )
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content="design a complex plan",
        token_breakdown=None,
        dynamic_context="",
    )
    partials: list[str] = []

    async def capture_partial(text: str) -> None:
        partials.append(text)

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=capture_partial)

    assert partials == ["我先捋一下思路，马上回来。"]
    assert results[0]["reply"] == "done"


@pytest.mark.asyncio
async def test_delayed_queue_when_normal_chat_requests_plan_status_then_reads_public_snapshot():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u2",
        "how is the plan going?",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)

    get_status = ToolCall(
        id="call-get-plan-status",
        function_name="get_plan_status",
        function_arguments="{}",
    )
    profile = SimpleNamespace(name="Bob", is_developer=False)
    engine = SimpleNamespace(
        config={
            "max_tool_rounds": 3,
            "enable_tools": True,
            "plan_mode_enabled": True,
            "plan_mode_limit_normal_tools": True,
            "plan_mode_chat_awareness_enabled": True,
        },
        delayed_queue=queue,
        _helpers=SimpleNamespace(
            get_recent_messages=lambda group_id, n: [],
            inject_multimodal_into_user_message=lambda messages, inputs: messages,
        ),
        rhythm_analyzer=SimpleNamespace(analyze=lambda group_id, recent: SimpleNamespace()),
        identity_resolver=SimpleNamespace(
            resolve_with_alias=lambda ctx, user_manager, group_id, **kwargs: SimpleNamespace(
                user_id="u2"
            )
        ),
        user_manager=SimpleNamespace(
            get_user=lambda user_id, group_id: profile,
            entries={"group-1": {"u2": profile}},
        ),
        semantic_memory=SimpleNamespace(
            get_user_profile=lambda group_id, user_id: SimpleNamespace(engagement_rate=1.0),
            get_group_profile=lambda group_id: None,
        ),
        style_adapter=SimpleNamespace(adapt=lambda **kwargs: SimpleNamespace()),
        persona=SimpleNamespace(),
        _other_ai_names=[],
        context_assembler=SimpleNamespace(
            build_messages_with_breakdown=lambda **kwargs: (
                [
                    {"role": "system", "content": kwargs["system_prompt"]},
                    {"role": "user", "content": kwargs["current_query"]},
                ],
                {},
            )
        ),
        brain=SimpleNamespace(
            chat=AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        raw_text="",
                        clean_text="",
                        tool_calls=[get_status],
                        reply_references=[],
                    ),
                    SimpleNamespace(
                        raw_text="I am checking config and tests.",
                        clean_text="I am checking config and tests.",
                        tool_calls=[],
                        reply_references=[],
                    ),
                ]
            )
        ),
        _tool_registry=None,
        _tool_executor=None,
        _active_plan_sessions={},
        _log_inner_thought=lambda text: None,
        event_bus=SimpleNamespace(emit=AsyncMock()),
    )
    session = start_plan_session(
        engine,
        group_id="group-1",
        owner_user_id="u1",
        goal="design plan mode",
    )
    update_plan_progress(
        session,
        phase="verifying",
        summary="Checking config and tests",
        confidence="high",
    )
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content="how is the plan going?",
        token_breakdown=None,
        dynamic_context="",
    )

    results = await tasks.tick_delayed_queue("group-1")

    assert results[0]["reply"] == "I am checking config and tests."
    first_request = engine.brain.chat.await_args_list[0].args[0]
    second_request = engine.brain.chat.await_args_list[1].args[0]
    assert first_request.enable_tools is False
    assert "get_plan_status" in {
        tool["function"]["name"] for tool in (first_request.extra_tools or [])
    }
    assert "Public planning status:" in first_request.system_prompt
    assert "Checking config and tests" in first_request.system_prompt
    assert any(
        msg.get("role") == "tool" and "Checking config and tests" in msg.get("content", "")
        for msg in second_request.messages
    )
    assert "hidden tool calls" in second_request.messages[-1]["content"]


@pytest.mark.asyncio
async def test_delayed_queue_when_text_sticker_marker_is_present_then_sticker_is_deferred():
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "send sticker",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)

    profile = SimpleNamespace(name="Alice", is_developer=False)
    execute_tool = AsyncMock(return_value=ToolResult(success=True, data={"sent": True}))
    engine = SimpleNamespace(
        config={"max_tool_rounds": 2},
        delayed_queue=queue,
        _helpers=SimpleNamespace(
            get_recent_messages=lambda group_id, n: [],
            inject_multimodal_into_user_message=lambda messages, inputs: messages,
        ),
        rhythm_analyzer=SimpleNamespace(analyze=lambda group_id, recent: SimpleNamespace()),
        identity_resolver=SimpleNamespace(
            resolve_with_alias=lambda ctx, user_manager, group_id, **kwargs: SimpleNamespace(
                user_id="u1"
            )
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
                    raw_text="[STICKER:开心] 先说正文",
                    clean_text="先说正文",
                    tool_calls=[],
                    reply_references=[],
                    sticker_names=["开心"],
                )
            )
        ),
        _tool_registry=None,
        _tool_executor=None,
        _sticker_names=["开心"],
        _log_inner_thought=lambda text: None,
        event_bus=SimpleNamespace(emit=AsyncMock()),
    )
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content="send sticker",
        token_breakdown=None,
        dynamic_context="",
    )

    results = await tasks.tick_delayed_queue("group-1")

    execute_tool.assert_not_awaited()
    assert results[0]["reply"] == "先说正文"
    assert results[0]["sticker_names"] == ["开心"]


@pytest.mark.asyncio
async def test_delayed_queue_when_reply_has_structure_then_sends_whole_content_as_image(
    monkeypatch,
):
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "describe the architecture",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)
    order: list[str] = []
    content = (
        "我先说下整体思路。\n```markdown\n**顶层模块**：\n- core/\n```\n"
        "中间说明。\n```markdown\n**执行层**：\n- worker/\n```\n细节之后再聊。"
    )

    async def send_rich_reply(content_arg: str, *, adapter, group_id: str, title: str = ""):
        order.append("image")
        assert content_arg == content
        assert adapter is engine._adapter
        assert group_id == "group-1"
        assert title == ""
        return {"image_message_id": "42", "forward_message_id": "77"}

    execute_tool = AsyncMock()
    tasks, engine = _agent_tool_tasks(
        queue,
        SimpleNamespace(name="lookup", silent=False, developer_only=False),
        [
            SimpleNamespace(
                raw_text=content,
                clean_text=content,
                tool_calls=[],
                reply_references=[],
                injected_request={
                    "system_prompt": "system",
                    "messages": [{"role": "user", "content": "question"}],
                    "tools": [],
                    "tool_choice": None,
                },
            )
        ],
        execute_tool,
    )
    engine._adapter = SimpleNamespace()
    monkeypatch.setattr(
        "sirius_pulse.core.bg_tasks_delayed._markdown_image.render_and_send_rich_reply",
        send_rich_reply,
    )
    delivered_cards: list[dict[str, object]] = []
    engine._record_assistant_message = lambda **kwargs: delivered_cards.append(kwargs)
    partials: list[str] = []

    async def capture_partial(text: str) -> None:
        order.append(f"text:{text}")
        partials.append(text)

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=capture_partial)

    assert partials == []
    assert order == ["image"]
    assert engine.brain.chat.await_count == 1
    execute_tool.assert_not_awaited()
    assert results[0]["reply"] == ""
    assert delivered_cards == [
        {
            "group_id": "group-1",
            "target_user_id": "u1",
            "content": content,
            "system_prompt": "",
            "tags": [{"type": "image", "label": "富文本卡片"}],
            "injected_request": {
                "system_prompt": "system",
                "messages": [{"role": "user", "content": "question"}],
                "tools": [],
                "tool_choice": None,
            },
            "injected_tool_names": [],
            "platform_message_id": "42",
        }
    ]


@pytest.mark.asyncio
async def test_delayed_queue_when_short_inline_markdown_stays_text(monkeypatch):
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "show status",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)

    execute_tool = AsyncMock()
    tasks, engine = _agent_tool_tasks(
        queue,
        SimpleNamespace(name="lookup", silent=False, developer_only=False),
        [
            SimpleNamespace(
                raw_text="执行 `docker ps` 查看状态。",
                clean_text="执行 `docker ps` 查看状态。",
                tool_calls=[],
                reply_references=[],
                injected_request={},
            )
        ],
        execute_tool,
    )
    engine._adapter = SimpleNamespace()
    render_rich = AsyncMock()
    monkeypatch.setattr(
        "sirius_pulse.core.bg_tasks_delayed._markdown_image.render_and_send_rich_reply",
        render_rich,
    )

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=AsyncMock())

    render_rich.assert_not_awaited()
    assert results[0]["reply"] == "执行 `docker ps` 查看状态。"


@pytest.mark.asyncio
async def test_delayed_queue_when_rich_reply_render_fails_then_falls_back_to_text(
    monkeypatch,
):
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "show status",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)
    content = "```markdown\n# 状态\n- healthy\n- service ok\n```"

    async def fail_to_render(*args, **kwargs):
        raise ValueError("content 过长")

    execute_tool = AsyncMock()
    tasks, engine = _agent_tool_tasks(
        queue,
        SimpleNamespace(name="lookup", silent=False, developer_only=False),
        [
            SimpleNamespace(
                raw_text=content,
                clean_text=content,
                tool_calls=[],
                reply_references=[],
                injected_request={
                    "system_prompt": "system",
                    "messages": [{"role": "user", "content": "question"}],
                    "tools": [],
                    "tool_choice": None,
                },
            )
        ],
        execute_tool,
    )
    engine._adapter = SimpleNamespace()
    monkeypatch.setattr(
        "sirius_pulse.core.bg_tasks_delayed._markdown_image.render_and_send_rich_reply",
        fail_to_render,
    )
    delivered: list[dict[str, object]] = []
    engine._record_assistant_message = lambda **kwargs: delivered.append(kwargs)
    partials: list[str] = []

    async def capture_partial(text: str) -> None:
        partials.append(text)

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=capture_partial)

    assert partials == [content]
    assert results[0]["reply"] == ""
    assert delivered[0]["content"] == content
    assert "tags" not in delivered[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_call", "metadata", "expected_content", "expected_tags"),
    [
        (
            ToolCall(
                id="call-1",
                function_name="group_file_exec",
                function_arguments='{"action": "image", "image_path": "/tmp/sirius.png"}',
            ),
            {
                "target_type": "group",
                "target_id": "group-1",
                "message_id": "77",
                "group_file_exec_action": "image",
            },
            "（已发送图片 /tmp/sirius.png；除非用户明确要求重发，否则不要再发）",
            [{"type": "image", "label": "/tmp/sirius.png"}],
        ),
        (
            ToolCall(
                id="call-1",
                function_name="group_file_exec",
                function_arguments='{"action": "file", "file_path": "/tmp/notes.md"}',
            ),
            {
                "target_type": "group",
                "target_id": "group-1",
                "file_name": "notes.md",
                "message_id": "78",
                "group_file_exec_action": "file",
            },
            "（已发送文件「notes.md」；除非用户明确要求重发，否则不要再发）",
            [{"type": "file", "label": "notes.md"}],
        ),
    ],
)
async def test_delayed_queue_when_external_delivery_succeeds_then_records_receipt_for_next_turn(
    tool_call, metadata, expected_content, expected_tags
):
    """静默投递的图片/文件必须留下历史，否则下一轮模型看不到自己发过而重发。"""
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "send it",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)
    tasks, engine = _agent_tool_tasks(
        queue,
        SimpleNamespace(
            name="group_file_exec",
            silent=False,
            developer_only=False,
            retry_safe=False,
        ),
        [
            SimpleNamespace(
                raw_text="",
                clean_text="",
                tool_calls=[tool_call],
                reply_references=[],
            ),
            SimpleNamespace(
                raw_text="",
                clean_text="",
                tool_calls=[],
                reply_references=[],
            ),
        ],
        AsyncMock(return_value=ToolResult(success=True, internal_metadata=metadata)),
    )
    delivered: list[dict[str, object]] = []
    engine._record_assistant_message = lambda **kwargs: delivered.append(kwargs)

    await tasks.tick_delayed_queue("group-1")

    assert [entry["content"] for entry in delivered] == [expected_content]
    assert delivered[0]["group_id"] == "group-1"
    assert delivered[0]["target_user_id"] == "u1"
    assert delivered[0]["tags"] == expected_tags
    assert delivered[0]["platform_message_id"] == metadata["message_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "metadata"),
    [
        (
            '{"action": "list", "folder_id": ""}',
            {"target_type": "group", "target_id": "group-1"},
        ),
        (
            '{"action": "download", "file_id": "f1"}',
            {"target_type": "group", "target_id": "group-1", "file_name": "a.md"},
        ),
    ],
)
async def test_delayed_queue_when_group_file_action_is_local_then_records_no_receipt(
    arguments, metadata
):
    """list/download 只在本地读写，没有发给群里，不该写入投递回执。"""
    queue = DelayedResponseQueue()
    item = queue.enqueue(
        "group-1",
        "u1",
        "list the files",
        _decision(ResponseStrategy.IMMEDIATE),
    )
    item.enqueue_time = _past(item.window_seconds + 1)
    tool_call = ToolCall(
        id="call-1",
        function_name="group_file_exec",
        function_arguments=arguments,
    )
    tasks, engine = _agent_tool_tasks(
        queue,
        SimpleNamespace(
            name="group_file_exec",
            silent=False,
            developer_only=False,
            retry_safe=False,
        ),
        [
            SimpleNamespace(
                raw_text="",
                clean_text="",
                tool_calls=[tool_call],
                reply_references=[],
            ),
            SimpleNamespace(
                raw_text="",
                clean_text="",
                tool_calls=[],
                reply_references=[],
            ),
        ],
        AsyncMock(return_value=ToolResult(success=True, internal_metadata=metadata)),
    )
    delivered: list[dict[str, object]] = []
    engine._record_assistant_message = lambda **kwargs: delivered.append(kwargs)

    await tasks.tick_delayed_queue("group-1")

    assert delivered == []
