"""自主回合与定时任务回合自动走工作模式：重工具可用、过程留轨迹。

这两类回合本来就是"她独自做事"的时刻，以前框架把它们当普通回合发（重工具还被
藏起来了），现在由框架直接替她进入工作模式，并且共享同一份工作模式设置——也就是
在 AMKR 面板里配哪个模型。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sirius_pulse.core.tool_engine_context import ToolEngineContextImpl
from sirius_pulse.core.work_mode import WorkModeStore
from sirius_pulse.providers.base import ToolCall
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
        config={"max_tool_rounds": 2, "tool_execution_timeout": 5},
    )
    context = ToolEngineContextImpl.__new__(ToolEngineContextImpl)
    context._engine = engine
    context.get_tool_descriptions = lambda **_kwargs: "- web_lookup: 查询天气"
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
    context.list_audiences = lambda: []
    context._unaddressed_intentions = lambda: []

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
