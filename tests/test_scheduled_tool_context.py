"""自主/定时与内部结果回调共用 Agent 工具循环；内部正文不自动对外发送。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.core.emotional_engine import EmotionalGroupChatEngine
from sirius_pulse.core.tool_engine_context import ToolEngineContextImpl
from sirius_pulse.core.work_mode import WorkModeStore
from sirius_pulse.providers.base import ImageGenerationResult, ToolCall
from sirius_pulse.tools.models import ToolResult


class _Executor:
    def __init__(self):
        self.context = None
        self.calls = []

    def set_chat_context(self, **kwargs):
        self.context = kwargs

    async def execute_async(self, tool, params, **kwargs):
        self.calls.append((tool, params, kwargs))
        return ToolResult.from_raw_result({"success": True, "text": "tool output"})


class _Registry:
    def get(self, name):
        return SimpleNamespace(name=name, retry_safe=False)


class _Brain:
    def __init__(self, rounds):
        self.requests = []
        self._rounds = list(rounds)

    async def chat(self, request):
        self.requests.append(request)
        return self._rounds.pop(0)


def _tool_round(*, tool="web_lookup", arguments='{"query":"weather"}'):
    return SimpleNamespace(
        raw_text="",
        clean_text="",
        tool_calls=[ToolCall(id="call-1", function_name=tool, function_arguments=arguments)],
    )


def _final_round(text):
    return SimpleNamespace(
        raw_text=text,
        clean_text=text,
        tool_calls=[],
        reply_references=[],
        sticker_names=[],
        poke_user_ids=[],
    )


def _context(tmp_path, brain, executor=None):
    engine = SimpleNamespace(
        persona=None,
        work_path=tmp_path,
        _tool_executor=executor or _Executor(),
        _tool_registry=_Registry(),
        brain=brain,
        delayed_queue=DelayedResponseQueue(),
        config={"max_tool_rounds": 2, "tool_execution_timeout": 5},
    )
    engine.get_agent_turn_lock = lambda group_id: EmotionalGroupChatEngine.get_agent_turn_lock(
        engine, group_id
    )
    context = ToolEngineContextImpl.__new__(ToolEngineContextImpl)
    context._engine = engine
    context.get_tool_descriptions = lambda **_kwargs: "- web_lookup: 查询天气"
    context.list_audiences = lambda: []
    context._unaddressed_intentions = lambda: []
    return context


def _sessions(tmp_path):
    return WorkModeStore(tmp_path).load()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_result", "expected"),
    [
        (True, True),
        (False, False),
        ("accepted", False),
        (1, False),
        ({"accepted": True}, False),
        (None, False),
    ],
)
async def test_scheduled_dispatch_only_accepts_explicit_true(handler_result, expected):
    async def dispatch_proactive_message(**_kwargs):
        return handler_result

    context = ToolEngineContextImpl.__new__(ToolEngineContextImpl)
    context._engine = SimpleNamespace(
        dispatch_proactive_message=dispatch_proactive_message,
    )

    accepted = await context.dispatch_proactive_message(
        group_id="group-1",
        text="scheduled update",
    )

    assert accepted is expected


@pytest.mark.asyncio
async def test_legacy_host_without_receipt_dispatcher_cannot_claim_delivery(tmp_path):
    context = _context(tmp_path, _Brain([]))
    emitted = []

    async def emit_event(*args, **kwargs):
        emitted.append((args, kwargs))
        return True

    context.emit_event = emit_event
    accepted = await context.dispatch_proactive_message(
        group_id="group-1",
        text="通知正文",
        event_id="image_generate:legacy:text",
        record_delivery=True,
    )

    assert accepted is False
    assert emitted == [], "入队不是平台回执，不得从旧event接口伪装成功"


@pytest.mark.asyncio
async def test_scheduled_generation_runs_tool_calls_before_final_text(tmp_path):
    executor = _Executor()
    brain = _Brain([_tool_round(), _final_round("天气不错。")])
    context = _context(tmp_path, brain, executor)

    result = await context.generate_scheduled_message(
        job={"expression": "*/5 * * * *", "command": "echo hello"},
        command_output="hello",
        group_id="group-1",
        user_id="u1",
        user_name="Alice",
        adapter_type="napcat",
    )

    assert result["text"] == "天气不错。"
    assert len(executor.calls) == 1
    assert executor.calls[0][1] == {"query": "weather"}
    assert executor.context == {
        "group_id": "group-1",
        "user_id": "u1",
        "adapter_type": "napcat",
    }
    assert len(brain.requests) == 2
    assert any(message["role"] == "tool" for message in brain.requests[1].messages)


@pytest.mark.asyncio
async def test_scheduled_turn_then_runs_in_work_mode_and_leaves_a_trace(tmp_path):
    """定时任务回合不需要模型自己喊 enter_work_mode，也不该在轨迹里缺席。"""
    brain = _Brain([_tool_round(), _final_round("天气不错。")])
    context = _context(tmp_path, brain)

    await context.generate_scheduled_message(
        job={"expression": "*/5 * * * *", "command": "echo hello"},
        command_output="hello",
        group_id="group-1",
        user_id="u1",
        user_name="Alice",
        adapter_type="napcat",
    )

    assert [request.work_mode for request in brain.requests] == [True, True]
    session = _sessions(tmp_path)[0]
    assert session["source"] == "scheduled"
    assert session["goal"] == "定时任务：echo hello"
    assert session["status"] == "completed"
    assert session["result"] == "天气不错。"
    assert session["steps"][0]["tools"][0]["name"] == "web_lookup"
    assert "tool output" in session["steps"][0]["results"][0]["output"]


@pytest.mark.asyncio
async def test_autonomous_turn_then_runs_in_work_mode_and_leaves_a_trace(tmp_path):
    brain = _Brain([_final_round("读完了那篇文章。")])
    context = _context(tmp_path, brain)

    result = await context.run_autonomous_turn(
        kind="reading",
        seed="把那篇文章读完",
        group_id="group-1",
        why="想知道结论",
    )

    assert result["text"] == "读完了那篇文章。"
    assert brain.requests[0].work_mode is True
    session = _sessions(tmp_path)[0]
    assert session["source"] == "autonomy"
    assert session["goal"] == "自主回合（reading）：想知道结论"
    assert session["result"] == "读完了那篇文章。"


@pytest.mark.asyncio
async def test_work_mode_task_setting_then_overrides_the_turn_task_name(tmp_path):
    """设置里的任务名就是 AMKR 的模型入口：设了它，整段工作都走那个模型。"""
    WorkModeStore(tmp_path).save_settings(task_name="work_mode_generate")
    brain = _Brain([_final_round("做完了。")])
    context = _context(tmp_path, brain)

    await context.run_autonomous_turn(kind="reading", seed="", group_id="group-1")

    assert brain.requests[0].task_name == "work_mode_generate"
    assert _sessions(tmp_path)[0]["task_name"] == "work_mode_generate"


@pytest.mark.asyncio
async def test_work_mode_task_setting_unset_then_keeps_the_native_task_name(tmp_path):
    brain = _Brain([_final_round("做完了。")])
    context = _context(tmp_path, brain)

    await context.run_autonomous_turn(kind="reading", seed="", group_id="group-1")

    assert brain.requests[0].task_name == "autonomy_generate"
    # 轨迹是给页面读的，必须能原样序列化。
    assert json.loads(json.dumps(_sessions(tmp_path)))[0]["task_name"] == "autonomy_generate"


@pytest.mark.asyncio
async def test_autonomous_turn_when_generation_raises_then_trace_is_unfinished(tmp_path):
    """生成抛错（比如网络超时）时，轨迹要收尾成未完成，不能永远停在"进行中"。"""

    class _RaisingBrain(_Brain):
        async def chat(self, request):
            self.requests.append(request)
            raise RuntimeError("provider down")

    context = _context(tmp_path, _RaisingBrain([]))
    context.list_audiences = lambda: []
    context._unaddressed_intentions = lambda: []

    with pytest.raises(RuntimeError):
        await context.run_autonomous_turn(kind="reading", seed="", group_id="group-1")

    session = _sessions(tmp_path)[0]
    assert session["status"] == "aborted"
    assert session["ended_at"]
    assert session["result"]


@pytest.mark.asyncio
async def test_a_turn_that_burns_every_tool_round_still_leaves_written_words(tmp_path):
    """轮次用完时必须再要一轮正文，否则她读了一堆东西却一个字都没留下。

    这正是自主行为"调用了但没产出"的原因：模型把全部轮次花在侦察上，最后一轮
    仍是工具调用，循环带回来的正文是空的，整次工作就此消失。收尾轮不带工具，
    所以正文不会再被一次工具调用顶掉。
    """
    brain = _Brain([_tool_round(), _tool_round(), _final_round("读完了两回，接着往下写。")])
    context = _context(tmp_path, brain)

    result = await context.run_autonomous_turn(kind="reading", seed="读一下连载写到哪了", group_id="group-1")

    assert result["text"] == "读完了两回，接着往下写。"
    # 前两轮带工具，收尾轮必须把工具摘掉。
    assert [request.enable_tools for request in brain.requests] == [True, True, False]
    session = _sessions(tmp_path)[0]
    assert session["result"] == "读完了两回，接着往下写。"
    # 撞到上限仍是"未完成"：收尾只是把已有结果写下来，不代表事情做完了。
    assert session["status"] == "aborted"


@pytest.mark.asyncio
async def test_a_turn_that_finishes_on_its_own_does_not_pay_for_a_wrap_up(tmp_path):
    """只有真撞到上限才补收尾轮；正常收尾的回合不该多花一次调用。"""
    brain = _Brain([_tool_round(), _final_round("天气不错。")])
    context = _context(tmp_path, brain)

    result = await context.run_autonomous_turn(kind="reading", seed="", group_id="group-1")

    assert result["text"] == "天气不错。"
    assert len(brain.requests) == 2
    assert _sessions(tmp_path)[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_the_wrap_up_keeps_the_earlier_text_when_it_fails(tmp_path):
    """收尾轮失败不该把整次工作变成异常：退回最后一轮已有的文字。"""

    class _BrainThatFailsOnWrapUp(_Brain):
        async def chat(self, request):
            if not request.enable_tools:
                raise RuntimeError("provider down")
            return await super().chat(request)

    brain = _BrainThatFailsOnWrapUp([_tool_round(), _tool_round()])
    context = _context(tmp_path, brain)

    result = await context.run_autonomous_turn(kind="reading", seed="", group_id="group-1")

    # 两轮工具都没写正文，所以结果仍是空的——但这一次是"如实为空"，不是崩溃。
    assert result["text"] == ""
    assert _sessions(tmp_path)[0]["status"] == "aborted"


async def _callback(context, **kwargs):
    return await context.handle_background_tool_result(
        callback_id="image_generate:img_callback",
        tool_name="image_generate",
        outcome="success",
        artifact_path="/data/images/cat.png",
        group_id="group-1",
        user_id="u1",
        user_name="Alice",
        adapter_type="napcat",
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "三个草稿先保留，不对外发。"])
async def test_background_result_allows_silent_completion_and_internal_summary(tmp_path, text):
    executor = _Executor()
    WorkModeStore(tmp_path).save_settings(task_name="work_mode_generate")
    brain = _Brain([_final_round(text)])
    context = _context(tmp_path, brain, executor)
    dispatched = []

    async def dispatch(**kwargs):
        dispatched.append(kwargs)
        return True

    context._engine.dispatch_proactive_message = dispatch
    payload = await _callback(context, request_summary="画一只猫")
    assert payload["status"] == "completed"
    assert payload["text"] == text
    assert len(brain.requests) == 1
    request = brain.requests[0]
    assert request.enable_tools is True
    assert request.post_process is False
    assert request.work_mode is True
    assert request.task_name == "work_mode_generate"
    assert "cat.png" in request.system_prompt
    assert executor.calls == dispatched == []
    assert len(_sessions(tmp_path)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("self_initiated", [False, True])
async def test_callback_tool_loop_fences_every_action_and_keeps_origin_permissions(
    tmp_path,
    self_initiated,
):
    events = []

    class CheckingExecutor(_Executor):
        async def execute_async(self, tool, params, **kwargs):
            assert events[-1] == "fence", "工具执行之前必须同步持久化unsafe"
            events.append("execute")
            invocation = kwargs["invocation_context"]
            assert invocation.background_event_id == "image_generate:img_callback"
            assert invocation.self_initiated is self_initiated
            assert invocation.caller_is_developer is False
            assert invocation.group_id == "group-1"
            assert invocation.caller_user_id == "u1"
            return await super().execute_async(tool, params, **kwargs)

    brain = _Brain(
        [
            _tool_round(tool="image_generate", arguments='{"prompt":"改成水彩"}'),
            _tool_round(
                tool="group_file_exec", arguments='{"action":"image","image_path":"cat.png"}'
            ),
            _final_round("内部总结，不自动外发"),
        ]
    )
    executor = CheckingExecutor()
    context = _context(tmp_path, brain, executor)
    context._engine.config["max_tool_rounds"] = 3
    payload = await _callback(
        context,
        self_initiated=self_initiated,
        before_tool=lambda: events.append("fence"),
    )
    assert payload["status"] == "completed"
    assert events == ["fence", "execute", "fence", "execute"]
    assert [call[0].name for call in executor.calls] == ["image_generate", "group_file_exec"]
    assert all(request.work_mode and not request.post_process for request in brain.requests)
    assert all(request.enable_tools for request in brain.requests)
    assert not context._engine.get_agent_turn_lock("group-1").locked()


@pytest.mark.asyncio
async def test_busy_group_defers_callback_without_model_or_tool_activity(tmp_path):
    brain = _Brain([_final_round("")])
    context = _context(tmp_path, brain)
    lock = context._engine.get_agent_turn_lock("group-1")
    await lock.acquire()
    try:
        payload = await asyncio.wait_for(_callback(context), timeout=0.5)
        assert payload["status"] == "deferred"
        assert brain.requests == []
        assert context._engine._tool_executor.calls == []
        assert _sessions(tmp_path) == []
    finally:
        lock.release()
    assert (await _callback(context))["status"] == "completed"


@pytest.mark.asyncio
async def test_callback_fence_error_stops_executor_and_releases_group_lock(tmp_path):
    brain = _Brain([_tool_round(tool="group_file_exec")])
    context = _context(tmp_path, brain)

    def failed_fence():
        raise OSError("cannot persist callback fence")

    with pytest.raises(OSError, match="callback fence"):
        await _callback(context, before_tool=failed_fence)
    assert context._engine._tool_executor.calls == []
    assert not context._engine.get_agent_turn_lock("group-1").locked()


@pytest.mark.asyncio
async def test_generate_image_sends_the_registered_task_name_and_reference_bytes(tmp_path):
    """生图不掺本地模型选择：发出的是已登记的任务名，参考图按原始字节交给 provider。"""

    class _ImageBrain:
        def __init__(self):
            self.requests = []

        async def generate_image_async(self, request):
            self.requests.append(request)
            return ImageGenerationResult(image_bytes=b"png-bytes", mime_type="image/png")

    brain = _ImageBrain()
    context = _context(tmp_path, brain)

    result = await context.generate_image(
        prompt="画一只猫",
        reference_images=[("ref.png", b"raw")],
        timeout_seconds=120.0,
    )

    assert result == {"image_bytes": b"png-bytes", "mime_type": "image/png"}
    request = brain.requests[0]
    assert request.model == "image_generate"
    assert request.prompt == "画一只猫"
    assert request.reference_images == [("ref.png", b"raw")]
    assert request.timeout_seconds == 120.0
