from __future__ import annotations

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


@pytest.mark.asyncio
async def test_delayed_queue_when_only_send_sticker_tool_then_retries_text_without_sticker_tool():
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
        function_arguments='{"names": ["happy"]}',
    )
    chat_results = [
        SimpleNamespace(
            raw_text="",
            clean_text="",
            tool_calls=[tool_call],
            reply_references=[],
            sticker_names=[],
        ),
        SimpleNamespace(
            raw_text="text after sticker",
            clean_text="text after sticker",
            tool_calls=[],
            reply_references=[],
            sticker_names=[],
        ),
    ]
    skill = SimpleNamespace(name="send_sticker", silent=True, developer_only=False)
    profile = SimpleNamespace(name="Alice", is_developer=False)
    execute_skill = AsyncMock(return_value=SkillResult(success=True, data={"sent": True}))
    brain_chat = AsyncMock(side_effect=chat_results)
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
        brain=SimpleNamespace(chat=brain_chat),
        _skill_registry=SimpleNamespace(get=lambda name: skill),
        _skill_executor=SimpleNamespace(
            set_chat_context=lambda **kwargs: None,
            execute_async=execute_skill,
        ),
        _sticker_names=["happy"],
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
    assert brain_chat.await_count == 2
    assert brain_chat.await_args_list[0].args[0].disabled_skill_names == set()
    assert brain_chat.await_args_list[1].args[0].disabled_skill_names == {"send_sticker"}
    assert results[0]["reply"] == "text after sticker"
    assert results[0]["sticker_names"] == ["happy"]
