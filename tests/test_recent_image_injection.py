"""最近消息的图片（图文同条或分开发的纯图片）应作为真实视觉输入进入模型。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.core.bg_tasks_delayed import DelayedQueueTasks
from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.core.helpers import Helpers
from sirius_pulse.memory.basic.manager import BasicMemoryManager
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision


def _engine(queue, *, brain_chat) -> SimpleNamespace:
    engine = SimpleNamespace(
        config={"max_tool_rounds": 1, "basic_memory_history_token_budget": 0},
        delayed_queue=queue,
        basic_memory=BasicMemoryManager(),
        rhythm_analyzer=SimpleNamespace(analyze=lambda group_id, recent: SimpleNamespace()),
        identity_resolver=SimpleNamespace(
            resolve_with_alias=lambda ctx, user_manager, group_id, **kwargs: SimpleNamespace(
                user_id="u1"
            )
        ),
        user_manager=SimpleNamespace(
            get_user=lambda user_id, group_id: SimpleNamespace(name="Alice", is_developer=False),
            entries={"group-1": {"u1": SimpleNamespace(name="Alice", is_developer=False)}},
        ),
        semantic_memory=SimpleNamespace(
            get_user_profile=lambda group_id, user_id: SimpleNamespace(engagement_rate=1.0)
        ),
        context_assembler=SimpleNamespace(
            build_messages_with_breakdown=lambda **kwargs: (
                [
                    {"role": "system", "content": kwargs["system_prompt"]},
                    {"role": "user", "content": kwargs["current_query"]},
                ],
                {},
            )
        ),
        brain=SimpleNamespace(chat=AsyncMock(side_effect=brain_chat)),
        _tool_registry=None,
        _tool_executor=None,
        _log_inner_thought=lambda text: None,
        event_bus=SimpleNamespace(emit=AsyncMock()),
    )
    engine._helpers = Helpers(engine)
    return engine


def _pending_item(queue, content: str):
    item = queue.enqueue(
        "group-1",
        "u1",
        content,
        StrategyDecision(strategy=ResponseStrategy.IMMEDIATE, urgency=50.0, reason="test"),
    )
    item.enqueue_time = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    return item


def _chat_ok(text: str = "好的"):
    return SimpleNamespace(raw_text=text, clean_text=text, tool_calls=[], reply_references=[])


def _first_user_message(engine) -> object:
    request = engine.brain.chat.await_args_list[0].args[0]
    return next(m for m in request.messages if m["role"] == "user")["content"]


def _tasks(engine, user_content: str) -> DelayedQueueTasks:
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content=user_content,
        token_breakdown=None,
        dynamic_context="",
    )
    return tasks


@pytest.mark.asyncio
async def test_recent_image_message_when_image_sent_separately_then_injected_as_vision_input():
    """分开发的纯图片消息没有触发延迟队列，但模型仍应看到它。"""
    queue = DelayedResponseQueue()
    _pending_item(queue, "刚才那张图是什么")
    engine = _engine(queue, brain_chat=[_chat_ok()])
    engine.basic_memory.add_entry(
        "group-1",
        "u2",
        "human",
        "[图片] [图片描述：一只橘猫]",
        multimodal_inputs=[{"type": "image", "value": "cat.png", "file_path": "cat.png"}],
    )

    await _tasks(engine, "刚才那张图是什么").tick_delayed_queue("group-1")

    content = _first_user_message(engine)
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert [part["image_url"]["url"] for part in content[1:]] == ["cat.png"]


@pytest.mark.asyncio
async def test_recent_image_when_already_answered_then_not_reinjected():
    """上一轮已回复过的图片不应反复占用视觉输入额度。"""
    queue = DelayedResponseQueue()
    _pending_item(queue, "继续说")
    engine = _engine(queue, brain_chat=[_chat_ok()])
    engine.basic_memory.add_entry(
        "group-1",
        "u2",
        "human",
        "[图片]",
        multimodal_inputs=[{"type": "image", "value": "old.png", "file_path": "old.png"}],
    )
    engine.basic_memory.add_entry("group-1", "assistant", "assistant", "我看过了")

    await _tasks(engine, "继续说").tick_delayed_queue("group-1")

    assert _first_user_message(engine) == "继续说"


@pytest.mark.asyncio
async def test_recent_images_when_repeated_then_deduplicated():
    """同一张图在多条消息里出现时只注入一次。"""
    queue = DelayedResponseQueue()
    item = _pending_item(queue, "再看看")
    item.multimodal_inputs = [{"type": "image", "value": "same.png"}]
    engine = _engine(queue, brain_chat=[_chat_ok()])
    engine.basic_memory.add_entry(
        "group-1",
        "u2",
        "human",
        "[图片]",
        multimodal_inputs=[{"type": "image", "value": "same.png", "file_path": "same.png"}],
    )

    await _tasks(engine, "再看看").tick_delayed_queue("group-1")

    content = _first_user_message(engine)
    assert [part["image_url"]["url"] for part in content[1:]] == ["same.png"]
