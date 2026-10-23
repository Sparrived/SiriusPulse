"""业务视角验证工作模式：进入/退出、正文不外发、暂存消息、轨迹落盘。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from sirius_pulse.core.bg_tasks_delayed import DelayedQueueTasks
from sirius_pulse.core.brain import Brain
from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase
from sirius_pulse.core.work_mode import (
    ENTER_WORK_MODE,
    QUIT_WORK_MODE,
    SEND_MIDWAY_MSG,
    WorkModeRun,
    WorkModeStore,
)
from sirius_pulse.exceptions import ProviderAuthError
from sirius_pulse.models.models import Message
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision
from sirius_pulse.providers.base import ToolCall
from sirius_pulse.providers.mock import MockProvider
from sirius_pulse.tools import ToolDefinition, ToolRegistry
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
    # 退出路径用真实实现：它要负责把工作期间没进上下文的消息排回队列。
    engine.end_work_mode = MethodType(_EmotionalGroupChatEngineBase.end_work_mode, engine)
    engine._requeue_after_work_mode = MethodType(
        _EmotionalGroupChatEngineBase._requeue_after_work_mode, engine
    )
    engine._persist_group_state = lambda group_id: None
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content="request",
        token_breakdown=None,
        dynamic_context="",
    )
    return tasks, engine, runs


TOOL = SimpleNamespace(name="bash", silent=False, developer_only=False, retry_safe=False)


class _ToolRoundProvider(MockProvider):
    """MockProvider 的文字与请求记录照用，只补预设的 function-call 回合。"""

    def __init__(self, rounds):
        super().__init__([getattr(result, "raw_text", "") for result in rounds])
        self.rounds = rounds

    async def generate_async(self, request, return_reasoning=False):
        index = len(self.requests)
        assert index < len(self.rounds), "工作循环发起了未预期的模型调用"
        result = await super().generate_async(request, return_reasoning=return_reasoning)
        if isinstance(self.rounds[index], BaseException):
            raise self.rounds[index]
        result.tool_calls = self.rounds[index].tool_calls
        return result


def _provider_work_mode_tasks(tmp_path, queue, rounds, *, max_tool_rounds=8):
    provider = _ToolRoundProvider(rounds)
    registry = ToolRegistry()
    for name in ("group_file_exec", "interaction_with_master", "silent_notice", "bash"):
        registry.register(
            ToolDefinition(name=name, description=name, silent=name == "silent_notice")
        )
    brain = Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda task_name, **kwargs: SimpleNamespace(model_name=task_name, timeout=1)
        ),
        persona=SimpleNamespace(name="Luna", build_system_prompt=lambda: ""),
        tool_registry=registry,
    )
    # 只跑回复路径，不启动抽取任务或后台服务。
    tasks, engine, runs = _work_mode_tasks(
        tmp_path, queue, brain.chat, max_tool_rounds=max_tool_rounds
    )
    engine.config["pending_message_threshold"] = 0
    engine._tool_registry = registry
    return tasks, engine, runs, provider


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
async def test_work_mode_when_first_avatar_is_sent_then_second_and_confirmation_reach_quit(
    tmp_path,
):
    """用户要两张头像并尝试更换：发出第一张不是整个目标完成。"""
    queue = DelayedResponseQueue()
    request = "帮我裁剪两张闺蜜头像，分别发给我，再尝试更换头像并确认结果。"
    item = queue.enqueue("group-1", "u1", request, _decision(ResponseStrategy.IMMEDIATE))
    item.enqueue_time = _past(item.window_seconds + 1)
    source = tmp_path / "pair.png"
    Image.new("RGB", (8, 4), "coral").save(source)
    avatars = [tmp_path / "avatar-left.png", tmp_path / "avatar-right.png"]
    confirmation = tmp_path / "avatar-confirmation.json"
    final_reply = "两张头像已分别发送；已尝试更换，确认需要你在客户端手动设置。"
    rounds = [
        _round("", _call(ENTER_WORK_MODE, json.dumps({"goal": request}), "enter")),
        _round("内部：先裁剪。", _call("bash", '{"command": "crop-pair"}', "crop")),
        *[
            _round(
                "",
                _call(
                    "group_file_exec",
                    json.dumps({"action": "image", "image_path": str(path)}),
                    f"send-{index}",
                ),
            )
            for index, path in enumerate(avatars)
        ],
        _round(
            "内部：两张发完再核实更换结果。",
            _call("bash", '{"command": "confirm-avatar-change"}', "confirm"),
        ),
        _round("", _call(QUIT_WORK_MODE, json.dumps({"result": final_reply}), "quit")),
    ]
    tasks, engine, runs, provider = _provider_work_mode_tasks(tmp_path, queue, rounds)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system", user_content=request, token_breakdown=None, dynamic_context=""
    )
    sent = []
    midway_statuses = []

    async def fake_tool(tool, params, **kwargs):
        midway_statuses.append(_session(tmp_path)["status"])
        if tool.name == "group_file_exec":
            path = next(path for path in avatars if str(path) == params["image_path"])
            with Image.open(path) as avatar:
                assert avatar.size == (4, 4)
            sent.append(path.name)
        elif params["command"] == "crop-pair":
            with Image.open(source) as image:
                image.crop((0, 0, 4, 4)).save(avatars[0])
                image.crop((4, 0, 8, 4)).save(avatars[1])
        else:
            assert params["command"] == "confirm-avatar-change"
            assert sent == [path.name for path in avatars]
            confirmation.write_text(
                json.dumps({"attempted": True, "requires_manual_change": True}), encoding="utf-8"
            )
        return ToolResult(success=True, data={"ok": True})

    engine._tool_executor.execute_async.side_effect = fake_tool
    partial = AsyncMock()

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=partial)

    session = _session(tmp_path)
    assert session["status"] == "completed", session
    assert session["result"] == results[0]["reply"] == final_reply
    assert session["ended_at"]
    assert sent == [path.name for path in avatars]  # 每张只发送一次，没有重放第一张。
    assert json.loads(confirmation.read_text(encoding="utf-8")) == {
        "attempted": True,
        "requires_manual_change": True,
    }
    assert midway_statuses == ["running"] * 4
    assert len(provider.requests) == len(rounds)
    assert session["steps"][-1]["tools"][0]["name"] == QUIT_WORK_MODE
    assert all("内部：" not in call.args[0] for call in partial.await_args_list)
    assert runs == {}
    assert not engine.is_work_mode_active("group-1")
    assert queue.get_pending("group-1") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["work", "enter_same_round", "chat"])
@pytest.mark.parametrize(
    "tool_name,arguments,silent",
    [
        ("group_file_exec", '{"action": "image", "image_path": "avatar.png"}', True),
        ("interaction_with_master", '{"action": "message", "message": "进展"}', True),
        ("silent_notice", "{}", True),
        ("group_file_exec", '{"action": "file", "file_path": "avatar.png"}', False),
    ],
    ids=["image", "master-message", "custom-silent", "file-nonsilent"],
)
async def test_silent_delivery_when_working_then_reaches_quit_but_chat_keeps_its_exit(
    tmp_path, mode, tool_name, arguments, silent
):
    queue = DelayedResponseQueue()
    _queued_job(queue)
    enter = _call(ENTER_WORK_MODE, '{"goal": "投递后确认"}', "enter")
    send = _call(tool_name, arguments, "send")
    rounds = [_round("", send)]
    if mode == "work":
        rounds.insert(0, _round("", enter))
    elif mode == "enter_same_round":
        rounds[0] = _round("", enter, send)
    rounds.append(
        _round("确认完成")
        if mode == "chat"
        else _round("", _call(QUIT_WORK_MODE, '{"result": "投递后确认完成"}', "quit"))
    )
    tasks, engine, runs, provider = _provider_work_mode_tasks(tmp_path, queue, rounds)

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=AsyncMock())

    engine._tool_executor.execute_async.assert_awaited_once()
    if mode == "chat":
        assert results[0]["reply"] == ("" if silent else "确认完成")
        assert len(provider.requests) == (1 if silent else 2)
        assert WorkModeStore(tmp_path).load() == []
    else:
        session = _session(tmp_path)
        assert session["status"] == "completed"
        assert session["result"] == results[0]["reply"] == "投递后确认完成"
        assert session["steps"][-1]["tools"][0]["name"] == QUIT_WORK_MODE
        assert session["ended_at"]
        assert len(provider.requests) == len(rounds)
        assert any(
            message.get("tool_call_id") == "send"
            and "[Tool result: success]" in str(message.get("content"))
            for message in provider.requests[-1].messages
        )
    assert runs == {}
    assert not engine.is_work_mode_active("group-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["round_limit", "provider_error", "cancelled"])
async def test_work_mode_when_interrupted_after_silent_success_then_stays_aborted(tmp_path, ending):
    """单次投递成功不会自动完成目标；预算、异常、取消仍要如实收尾。"""
    queue = DelayedResponseQueue()
    _queued_job(queue)
    rounds = [
        _round("", _call(ENTER_WORK_MODE, '{"goal": "发送两张头像"}', "enter")),
        _round(
            "",
            _call("group_file_exec", '{"action": "image", "image_path": "first.png"}', "send"),
        ),
    ]
    if ending == "round_limit":
        rounds.append(_round("只发了第一张，预算已用尽。"))
    else:
        error = (
            ProviderAuthError("mock", "provider unavailable")
            if ending == "provider_error"
            else asyncio.CancelledError()
        )
        rounds.append(error)
    tasks, engine, runs, provider = _provider_work_mode_tasks(
        tmp_path, queue, rounds, max_tool_rounds=1 if ending == "round_limit" else 8
    )

    if ending == "round_limit":
        results = await tasks.tick_delayed_queue("group-1", on_partial_reply=AsyncMock())
        assert results[0]["reply"] == "只发了第一张，预算已用尽。"
        assert provider.requests[-1].tool_choice == "none"
    else:
        with pytest.raises(type(error)):
            await tasks.tick_delayed_queue("group-1", on_partial_reply=AsyncMock())

    session = _session(tmp_path)
    assert session["status"] == "aborted"
    assert session["result"]
    assert session["ended_at"]
    assert session["steps"][-1]["tools"][0]["name"] == "group_file_exec"
    assert "[Tool result: success]" in session["steps"][-1]["results"][0]["output"]
    assert len(provider.requests) == 3
    engine._tool_executor.execute_async.assert_awaited_once()
    assert runs == {}
    assert not engine.is_work_mode_active("group-1")


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

    # 进入轮模型没写正文，框架补一句进展；随后的 send_midway_msg 照常送达。
    assert partials == ["收到，开始处理：核对名单", "进展：名单列完了，人数对吗？"]
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


@pytest.mark.asyncio
async def test_work_mode_when_quit_result_is_empty_then_group_still_hears_something(tmp_path):
    """模型退出工作模式却没给出 result 时，群里不能一声不响。"""
    queue = DelayedResponseQueue()
    _queued_job(queue)
    calls: list = []

    async def chat(request):
        calls.append(request)
        if len(calls) == 1:
            return _round("", _call(ENTER_WORK_MODE, '{"goal": "整理群文件"}', "c-enter"))
        if len(calls) == 2:
            return _round("目录已经列完，正在整理。", _call("bash", '{"command": "ls"}', "c-bash"))
        return _round("", _call(QUIT_WORK_MODE, "{}", "c-quit"))

    tasks, _, _ = _work_mode_tasks(tmp_path, queue, chat)

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=AsyncMock())

    assert results[0]["reply"]


@pytest.mark.asyncio
async def test_work_mode_when_work_is_cut_off_then_group_hears_it_is_unfinished(tmp_path):
    """撞上轮次上限、收尾轮也没正文时，框架要替她说明没做完，而不是静默消失。"""
    queue = DelayedResponseQueue()
    _queued_job(queue)
    calls: list = []

    async def chat(request):
        calls.append(request)
        if request.tool_choice == "none":
            return _round("")
        if len(calls) == 1:
            return _round("", _call(ENTER_WORK_MODE, '{"goal": "跑很久的任务"}', "c-enter"))
        return _round("", _call("bash", '{"command": "sleep 1"}', f"c-bash-{len(calls)}"))

    tasks, _, _ = _work_mode_tasks(tmp_path, queue, chat, max_tool_rounds=1)

    results = await tasks.tick_delayed_queue("group-1", on_partial_reply=AsyncMock())

    assert results[0]["reply"]
    assert _session(tmp_path)["status"] == "aborted"


@pytest.mark.asyncio
async def test_work_mode_when_generation_fails_then_group_is_released(tmp_path):
    """工作模式中途抛错也不能把群永久留在暂存窗口里——那会让她从此收不到话也不说话。"""
    queue = DelayedResponseQueue()
    _queued_job(queue)
    calls: list = []

    async def chat(request):
        calls.append(request)
        if len(calls) == 1:
            return _round("", _call(ENTER_WORK_MODE, '{"goal": "整理群文件"}', "c-enter"))
        raise RuntimeError("provider down")

    tasks, _, runs = _work_mode_tasks(tmp_path, queue, chat)

    with pytest.raises(RuntimeError):
        await tasks.tick_delayed_queue("group-1", on_partial_reply=AsyncMock())

    assert runs == {}


@pytest.mark.asyncio
async def test_work_mode_when_chatter_arrives_then_next_round_answers_it(tmp_path):
    """工作期间没点名的消息：出了工作模式后由下一轮正常回复，而不是就此消失。"""
    queue = DelayedResponseQueue()
    _queued_job(queue)
    calls: list = []
    holder: dict = {}
    inbound = Message(role="user", content="你们聊什么呢", speaker="Alice")

    async def chat(request):
        calls.append(request)
        if len(calls) == 1:
            return _round("", _call(ENTER_WORK_MODE, '{"goal": "整理群文件"}', "c-enter"))
        if len(calls) == 2:
            holder["engine"]._work_mode_runs["group-1"].stash_message(
                "你们聊什么呢",
                mentions_persona=False,
                replay=(inbound, "u1"),
            )
            return _round("", _call(QUIT_WORK_MODE, '{"result": "整理完了"}', "c-quit"))
        return _round("我在整理群文件，刚忙完——你们聊什么呢？")

    tasks, engine, _ = _work_mode_tasks(tmp_path, queue, chat)
    holder["engine"] = engine

    first = await tasks.tick_delayed_queue("group-1", on_partial_reply=AsyncMock())
    assert first[0]["reply"] == "整理完了"
    pending = queue.get_pending("group-1")
    assert [item for item in pending if "你们聊什么呢" in item.message_content]

    second = await tasks.tick_delayed_queue("group-1", on_partial_reply=AsyncMock())

    assert second[0]["reply"] == "我在整理群文件，刚忙完——你们聊什么呢？"


@pytest.mark.asyncio
async def test_work_mode_when_generation_fails_then_trace_is_not_left_running(tmp_path):
    """生成抛错的那次工作必须在轨迹里收尾成未完成，不能永远停在"进行中"。"""
    queue = DelayedResponseQueue()
    _queued_job(queue)
    calls: list = []

    async def chat(request):
        calls.append(request)
        if len(calls) == 1:
            return _round("", _call(ENTER_WORK_MODE, '{"goal": "整理群文件"}', "c-enter"))
        raise RuntimeError("provider down")

    tasks, _, _ = _work_mode_tasks(tmp_path, queue, chat)

    with pytest.raises(RuntimeError):
        await tasks.tick_delayed_queue("group-1", on_partial_reply=AsyncMock())

    session = _session(tmp_path)
    assert session["status"] == "aborted"
    assert session["ended_at"]
    assert session["result"]


def test_work_mode_store_when_runs_were_left_running_then_startup_marks_them_unfinished(tmp_path):
    """上一个进程留下的 running 一定是残留：启动时收尾，别让页面一直挂着"进行中"。"""
    store = WorkModeStore(tmp_path)
    finished = WorkModeRun(group_id="group-1", goal="做完了的", session_id="s-done")
    finished.finish(result="完成")
    store.save_run(finished)
    stale = WorkModeRun(group_id="group-1", goal="断了的", session_id="s-stale")
    store.save_run(stale)
    assert [item["status"] for item in store.load()] == ["completed", "running"]

    changed = store.reconcile_running(reason="进程重启，没有收尾。")

    assert changed == 1
    sessions = {item["session_id"]: item for item in store.load()}
    assert sessions["s-stale"]["status"] == "aborted"
    assert sessions["s-stale"]["result"] == "进程重启，没有收尾。"
    assert sessions["s-stale"]["ended_at"]
    # 已经收过尾的不动。
    assert sessions["s-done"]["status"] == "completed"
    assert sessions["s-done"]["result"] == "完成"
    # 幂等：再跑一次没有可改的。
    assert store.reconcile_running(reason="进程重启，没有收尾。") == 0
