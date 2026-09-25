"""业务视角验证工作模式：进入/退出、正文不外发、暂存消息、轨迹落盘。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.core.bg_tasks_delayed import DelayedQueueTasks
from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.core.work_mode import (
    ENTER_WORK_MODE,
    QUIT_WORK_MODE,
    SEND_MIDWAY_MSG,
    WorkModeRun,
    WorkModeStore,
)
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision
from sirius_pulse.providers.base import ToolCall
from sirius_pulse.tools.models import ToolResult


def _decision(strategy: ResponseStrategy) -> StrategyDecision:
    return StrategyDecision(strategy=strategy, urgency=50.0, reason="test")


def _past(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _call(name: str, arguments: str, call_id: str) -> ToolCall:
    return ToolCall(id=call_id, function_name=name, function_arguments=arguments)


def _round(text: str, *tool_calls: ToolCall) -> SimpleNamespace:
    return SimpleNamespace(
        raw_text=text,
        clean_text=text,
        tool_calls=list(tool_calls),
        reply_references=[],
    )


def _work_mode_tasks(tmp_path, queue, chat_fn, *, max_tool_rounds: int = 4):
    """A delayed-queue ticker whose engine records work-mode runs."""
    runs: dict[str, WorkModeRun] = {}
    profile = SimpleNamespace(name="Alice", is_developer=False)
    engine = SimpleNamespace(
        work_path=tmp_path,
        config={
            "max_tool_rounds": max_tool_rounds,
            "tool_execution_timeout": 5,
            "partial_reply_lead_seconds": 0,
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
        brain=SimpleNamespace(chat=AsyncMock(side_effect=chat_fn)),
        _tool_registry=SimpleNamespace(get=lambda name: TOOL),
        _tool_executor=SimpleNamespace(
            set_chat_context=lambda **kwargs: None,
            execute_async=AsyncMock(return_value=ToolResult(success=True, data={"ok": True})),
        ),
        _log_inner_thought=lambda text: None,
        event_bus=SimpleNamespace(emit=AsyncMock()),
    )
    engine._work_mode_runs = runs
    engine.begin_work_mode = lambda group_id, run: runs.__setitem__(group_id, run)
    engine.is_work_mode_active = lambda group_id: group_id in runs
    engine.end_work_mode = lambda group_id: runs.pop(group_id, None)
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content="request",
        token_breakdown=None,
        dynamic_context="",
    )
    return tasks, engine, runs


TOOL = SimpleNamespace(name="bash", silent=False, developer_only=False, retry_safe=False)


def _queued_job(queue) -> None:
    item = queue.enqueue("group-1", "u1", "整理一下", _decision(ResponseStrategy.IMMEDIATE))
    item.enqueue_time = _past(item.window_seconds + 1)


def _session(tmp_path, index: int = -1) -> dict:
    sessions = WorkModeStore(tmp_path).load()
    assert sessions
    return sessions[index]


@pytest.mark.asyncio
async def test_work_mode_when_entered_then_heavy_tools_unlock_and_text_stops_going_out(tmp_path):
    queue = DelayedResponseQueue()
    _queued_job(queue)
    calls: list = []

    async def chat(request):
        calls.append(request)
        if len(calls) == 1:
            return _round(
                "我先看一眼。",
                _call(ENTER_WORK_MODE, '{"goal": "整理群文件"}', "c-enter"),
            )
        if len(calls) == 2:
            return _round(
                "内部笔记：先列目录。",
                _call("bash", '{"command": "ls"}', "c-bash"),
            )
        return _round("", _call(QUIT_WORK_MODE, '{"result": "整理完了：3 个文件"}', "c-quit"))

    tasks, engine, runs = _work_mode_tasks(tmp_path, queue, chat)
    partials: list[str] = []

    async def capture_partial(text: str) -> None:
        partials.append(text)

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=capture_partial)

    # 进入工作模式前的那句正文照常发出；工作模式内的正文一律不出门。
    assert partials == ["我先看一眼。"]
    assert results[0]["reply"] == "整理完了：3 个文件"
    assert [tool["function"]["name"] for tool in calls[0].extra_tools] == [ENTER_WORK_MODE]
    assert calls[0].work_mode is False
    assert {tool["function"]["name"] for tool in calls[1].extra_tools} == {
        QUIT_WORK_MODE,
        SEND_MIDWAY_MSG,
    }
    assert calls[1].work_mode is True
    engine._tool_executor.execute_async.assert_awaited_once()

    session = _session(tmp_path)
    assert session["goal"] == "整理群文件"
    assert session["result"] == "整理完了：3 个文件"
    assert session["status"] == "completed"
    assert session["ended_at"]
    assert [step["text"] for step in session["steps"]][:2] == [
        "我先看一眼。",
        "内部笔记：先列目录。",
    ]
    bash_step = next(step for step in session["steps"] if step["results"])
    assert bash_step["tools"][0]["name"] == "bash"
    assert "[Tool result: success]" in bash_step["results"][0]["output"]
    # 退出后群里恢复正常回复。
    assert runs == {}


@pytest.mark.asyncio
async def test_work_mode_when_send_midway_msg_then_group_sees_it_and_work_continues(tmp_path):
    queue = DelayedResponseQueue()
    _queued_job(queue)
    calls: list = []

    async def chat(request):
        calls.append(request)
        if len(calls) == 1:
            return _round("", _call(ENTER_WORK_MODE, '{"goal": "核对名单"}', "c-enter"))
        if len(calls) == 2:
            return _round(
                "内部思考：先问一下人数。",
                _call(SEND_MIDWAY_MSG, '{"message": "进展：名单列完了，人数对吗？"}', "c-mid"),
            )
        return _round("", _call(QUIT_WORK_MODE, '{"result": "核对完成"}', "c-quit"))

    tasks, _, _ = _work_mode_tasks(tmp_path, queue, chat)
    partials: list[str] = []

    async def capture_partial(text: str) -> None:
        partials.append(text)

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=capture_partial)

    assert partials == ["进展：名单列完了，人数对吗？"]
    assert results[0]["reply"] == "核对完成"
    midway_tool_messages = [
        message["content"]
        for message in calls[-1].messages
        if message.get("role") == "tool" and "消息已发送给群里" in str(message.get("content"))
    ]
    assert midway_tool_messages
    session = _session(tmp_path)
    midway_steps = [step for step in session["steps"] if step.get("kind") == "midway"]
    assert [step["text"] for step in midway_steps] == ["进展：名单列完了，人数对吗？"]


@pytest.mark.asyncio
async def test_work_mode_when_persona_is_named_then_whole_stash_enters_at_once(tmp_path):
    queue = DelayedResponseQueue()
    _queued_job(queue)
    calls: list = []
    snapshots: list[list[dict]] = []

    async def chat(request):
        calls.append(request)
        snapshots.append([dict(message) for message in request.messages])
        if len(calls) == 1:
            return _round("", _call(ENTER_WORK_MODE, '{"goal": "整理群文件"}', "c-enter"))
        if len(calls) == 2:
            return _round("", _call("bash", '{"command": "ls"}', "c-bash"))
        return _round("", _call(QUIT_WORK_MODE, '{"result": "完成"}', "c-quit"))

    tasks, engine, runs = _work_mode_tasks(tmp_path, queue, chat)

    async def capture_partial(text: str) -> None:
        return None

    # 第三轮开始前群里来消息：先闲聊、再被点名，暂存必须一次性全部补进来。
    original_chat = engine.brain.chat.side_effect

    async def chat_with_inbound(request):
        result = await original_chat(request)
        if len(calls) == 2:
            run = runs["group-1"]
            run.stash_message("你们聊什么呢", mentions_persona=False)
            run.stash_message("Luna 你弄完没有", mentions_persona=True)
            run.stash_message("顺便说一句", mentions_persona=False)
        return result

    engine.brain = SimpleNamespace(chat=chat_with_inbound)

    await tasks.tick_delayed_queue("group-1", on_partial_reply=capture_partial)

    third_request_contents = [message.get("content") for message in snapshots[2]]
    assert "你们聊什么呢" not in [message.get("content") for message in snapshots[1]]
    assert [
        content
        for content in third_request_contents
        if content in {"你们聊什么呢", "Luna 你弄完没有", "顺便说一句"}
    ] == ["你们聊什么呢", "Luna 你弄完没有", "顺便说一句"]


@pytest.mark.asyncio
async def test_work_mode_when_round_limit_hits_then_run_is_recorded_as_aborted(tmp_path):
    queue = DelayedResponseQueue()
    _queued_job(queue)
    calls: list = []

    async def chat(request):
        calls.append(request)
        if request.tool_choice == "none":
            return _round("我先停下来汇报。")
        if len(calls) == 1:
            return _round("", _call(ENTER_WORK_MODE, '{"goal": "跑很久的任务"}', "c-enter"))
        return _round("", _call("bash", '{"command": "sleep 1"}', f"c-bash-{len(calls)}"))

    tasks, _, runs = _work_mode_tasks(tmp_path, queue, chat, max_tool_rounds=1)

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=AsyncMock())

    session = _session(tmp_path)
    assert session["status"] == "aborted"
    assert session["result"]
    assert session["ended_at"]
    assert runs == {}
    assert results[0]["reply"]


def test_work_mode_store_when_many_runs_then_keeps_the_newest(tmp_path, monkeypatch):
    monkeypatch.setattr("sirius_pulse.core.work_mode.MAX_RECORDED_SESSIONS", 2)
    store = WorkModeStore(tmp_path)

    for index in range(3):
        run = WorkModeRun(group_id="group-1", goal=f"目标{index}", session_id=f"s{index}")
        run.finish(result=f"结果{index}")
        store.save_run(run)

    sessions = store.load()
    assert [session["session_id"] for session in sessions] == ["s1", "s2"]
    assert store.path.exists()


@pytest.mark.asyncio
async def test_work_mode_when_model_is_configured_then_only_work_rounds_use_it(tmp_path):
    """工作模式可以换模型：进入之后的那几轮走配置的任务名，进入前的普通回合不变。"""
    WorkModeStore(tmp_path).save_settings(task_name="work_mode_generate")
    queue = DelayedResponseQueue()
    _queued_job(queue)
    calls: list = []

    async def chat(request):
        calls.append(request)
        if len(calls) == 1:
            return _round("我先看一眼。", _call(ENTER_WORK_MODE, '{"goal": "整理群文件"}', "c-enter"))
        if len(calls) == 2:
            return _round("", _call("bash", '{"command": "ls"}', "c-bash"))
        return _round("", _call(QUIT_WORK_MODE, '{"result": "整理完了"}', "c-quit"))

    tasks, _, _ = _work_mode_tasks(tmp_path, queue, chat)

    await tasks.tick_delayed_queue("group-1", on_partial_reply=AsyncMock())

    assert [request.task_name for request in calls] == [
        "response_generate",
        "work_mode_generate",
        "work_mode_generate",
    ]
    session = _session(tmp_path)
    assert session["task_name"] == "work_mode_generate"
    assert session["source"] == "chat"


@pytest.mark.asyncio
async def test_work_mode_when_model_is_not_configured_then_keeps_normal_task_name(tmp_path):
    queue = DelayedResponseQueue()
    _queued_job(queue)
    calls: list = []

    async def chat(request):
        calls.append(request)
        if len(calls) == 1:
            return _round("我先看一眼。", _call(ENTER_WORK_MODE, '{"goal": "整理群文件"}', "c-enter"))
        return _round("", _call(QUIT_WORK_MODE, '{"result": "整理完了"}', "c-quit"))

    tasks, _, _ = _work_mode_tasks(tmp_path, queue, chat)

    await tasks.tick_delayed_queue("group-1", on_partial_reply=AsyncMock())

    assert [request.task_name for request in calls] == ["response_generate", "response_generate"]
