from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.core.bg_tasks_delayed import DelayedQueueTasks
from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.core.plan_runtime import start_plan_session, update_plan_progress
from sirius_pulse.core.prompt_factory import StyleAdapter
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision
from sirius_pulse.providers.base import ToolCall
from sirius_pulse.skills.models import SkillResult


def _decision(strategy: ResponseStrategy, *, urgency: float = 50.0) -> StrategyDecision:
    return StrategyDecision(strategy=strategy, urgency=urgency, reason="test")


def _past(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _agent_tool_tasks(queue, skill, chat_results, execute_skill):
    profile = SimpleNamespace(name="Alice", is_developer=False)
    engine = SimpleNamespace(
        config={"max_skill_rounds": 2},
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
        _skill_registry=SimpleNamespace(get=lambda name: skill),
        _skill_executor=SimpleNamespace(
            set_chat_context=lambda **kwargs: None,
            execute_async=execute_skill,
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


def test_delayed_queue_when_hard_immediate_then_uses_short_window():
    queue = DelayedResponseQueue()
    decision = _decision(ResponseStrategy.IMMEDIATE)
    decision.context["hard_immediate"] = True

    item = queue.enqueue("group-1", "u1", "hello", decision)

    assert item.window_seconds == 1.0


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
        glossary_manager=SimpleNamespace(build_prompt_section=lambda *args, **kwargs: ""),
        semantic_memory=SimpleNamespace(
            get_user_profile=lambda *args, **kwargs: None,
            get_group_profile=lambda *args, **kwargs: SimpleNamespace(atmosphere_history=[]),
        ),
        style_adapter=StyleAdapter(),
        persona=SimpleNamespace(
            max_tokens_preference=None,
            temperature_preference=None,
            communication_style="",
            emoji_preference="",
        ),
        _other_ai_names=[],
        _skill_registry=None,
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
    assert "不超过 12 个汉字" in bundle.system_prompt
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
async def test_delayed_queue_when_tool_call_has_text_then_partial_leads_final_reply(
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
    skill = SimpleNamespace(name="lookup", silent=False, developer_only=False, retry_safe=False)
    profile = SimpleNamespace(name="Alice", is_developer=False)
    order: list[str] = []

    async def execute_skill(*args, **kwargs):
        order.append("tool")
        return SkillResult(success=True, data={"ok": True})

    engine = SimpleNamespace(
        config={
            "max_skill_rounds": 2,
            "partial_reply_lead_seconds": 1.5,
            "skill_execution_timeout": 12,
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
        _skill_registry=SimpleNamespace(get=lambda name: skill),
        _skill_executor=SimpleNamespace(
            set_chat_context=lambda **kwargs: None,
            execute_async=AsyncMock(side_effect=execute_skill),
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
    execute_kwargs = engine._skill_executor.execute_async.await_args.kwargs
    assert execute_kwargs["timeout"] == 12
    assert execute_kwargs["max_retries"] == 0
    second_request = engine.brain.chat.await_args_list[1].args[0]
    tool_message = next(message for message in second_request.messages if message["role"] == "tool")
    assert tool_message["content"].startswith("[Tool result: success]")
    assert "reference data" in tool_message["content"]
    first_request = engine.brain.chat.await_args_list[0].args[0]
    assert first_request.skill_query == "check status"
    assert first_request.max_skill_candidates == 8
    turn_events = [
        call.args[0].data
        for call in engine.event_bus.emit.await_args_list
        if call.args[0].type.value == "agent_turn_updated"
    ]
    assert turn_events[-1]["phase"] == "complete"


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
    skill = SimpleNamespace(
        name="group_management",
        silent=False,
        developer_only=False,
        retry_safe=False,
        side_effect="destructive",
    )
    execute_skill = AsyncMock(return_value=SkillResult(success=True, data={"ok": True}))
    tasks, engine = _agent_tool_tasks(
        queue,
        skill,
        [
            SimpleNamespace(
                raw_text="", clean_text="", tool_calls=[tool_call], reply_references=[]
            ),
            SimpleNamespace(raw_text="Done.", clean_text="Done.", tool_calls=[], reply_references=[]),
        ],
        execute_skill,
    )

    results = await tasks.tick_delayed_queue("group-1")

    assert results[0]["reply"] == "Done."
    assert execute_skill.await_args.args[1] == {"action": "kick", "user_id": 42}


@pytest.mark.asyncio
async def test_delayed_queue_when_chat_round_uses_stop_only_flow_control():
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
        config={"max_skill_rounds": 3},
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
        _skill_registry=None,
        _skill_executor=None,
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
    assert seen_extra_tools == [{"stop"}]


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
        _skill_registry=SimpleNamespace(get=lambda name: skill),
        _skill_executor=SimpleNamespace(
            set_chat_context=lambda **kwargs: None,
            execute_async=execute_skill,
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

    async def fail_partial_send(text: str) -> None:
        raise RuntimeError("send failed")

    with pytest.raises(RuntimeError, match="send failed"):
        await tasks.tick_delayed_queue("group-1", on_partial_reply=fail_partial_send)

    execute_skill.assert_not_awaited()


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
            "max_skill_rounds": 3,
            "enable_skills": True,
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
        _skill_registry=None,
        _skill_executor=None,
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
    assert first_request.enable_skills is False
    assert second_request.enable_skills is True
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
            "max_skill_rounds": 3,
            "enable_skills": True,
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
        _skill_registry=None,
        _skill_executor=None,
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
            "max_skill_rounds": 3,
            "enable_skills": True,
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
        _skill_registry=None,
        _skill_executor=None,
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
            "max_skill_rounds": 3,
            "enable_skills": True,
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
        glossary_manager=SimpleNamespace(build_prompt_section=lambda *args, **kwargs: ""),
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
        _skill_registry=None,
        _skill_executor=None,
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
    assert first_request.enable_skills is False
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
async def test_delayed_queue_when_interaction_sticker_tool_is_called_then_sticker_is_deferred():
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
        function_name="interaction",
        function_arguments='{"action": "sticker", "names": ["开心"]}',
    )
    skill = SimpleNamespace(name="interaction", silent=False, developer_only=False)
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

    execute_skill.assert_not_awaited()
    assert results[0]["reply"] == "先说正文"
    assert results[0]["sticker_names"] == ["开心"]
