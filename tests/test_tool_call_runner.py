"""业务视角：模型发起的一次工具调用，最终回给模型的是什么、外部被做了几次。

聊天回合与工作模式回合共用同一个执行入口，所以这里的结论对两条路都成立。
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from sirius_pulse.core.tool_call_runner import ToolCallRunner
from sirius_pulse.providers.base import ToolCall
from sirius_pulse.tools import ToolDefinition, ToolExecutor, ToolParameter, ToolRegistry


def _engine(tmp_path: Any, *tools: ToolDefinition, config: dict | None = None) -> Any:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return SimpleNamespace(
        _tool_registry=registry,
        _tool_executor=ToolExecutor(tmp_path),
        config=config or {},
        _bg_tasks_mgr=None,
    )


def _call(name: str, arguments: str = "{}") -> ToolCall:
    return ToolCall(
        id=f"call_{name}", type="function", function_name=name, function_arguments=arguments
    )


async def _run(engine: Any, call: ToolCall):
    runner = ToolCallRunner(engine)
    return await runner.execute(runner.prepare(call, None), group_id="g1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "attempts", "ok"),
    [
        ("message", 1, False),  # 对主人说的话：网络抖一下也不能补发第二遍
        ("status", 2, True),  # 只读查询：抖一下就重试，最终拿到结果
    ],
)
async def test_flaky_network_never_resends_a_message_to_master(
    tmp_path: Any, action: str, attempts: int, ok: bool
) -> None:
    calls: list[str] = []

    def run(action: str = "", **_: Any) -> dict:
        calls.append(action)
        if len(calls) == 1:
            raise ConnectionError("network flapped")
        return {"status": "ok"}

    # 用内置工具自己的声明（按 action 区分能否重试），只把真正的发送换成会抖的假实现。
    builtin = ToolRegistry._load_tool_file(
        ToolRegistry.builtin_tools_dir() / "interaction_with_master.py"
    )
    tool = replace(builtin, _run_func=run, inject_runtime_params=False)
    outcome = await _run(_engine(tmp_path, tool), _call(tool.name, f'{{"action": "{action}"}}'))

    assert len(calls) == attempts
    assert outcome.ok is ok
    assert outcome.tool_message()["tool_call_id"] == "call_interaction_with_master"


@pytest.mark.asyncio
async def test_unknown_tool_is_reported_back_without_running_anything(tmp_path: Any) -> None:
    outcome = await _run(_engine(tmp_path), _call("does_not_exist"))

    assert not outcome.executed
    assert outcome.tool_message() == {
        "role": "tool",
        "tool_call_id": "call_does_not_exist",
        "content": "Tool 'does_not_exist' not found",
    }


@pytest.mark.asyncio
async def test_garbled_arguments_run_the_tool_with_defaults(tmp_path: Any) -> None:
    seen: list[str] = []

    def run(who: str = "", **_: Any) -> dict:
        seen.append(who)
        return {"echo": f"hi {who}"}

    tool = ToolDefinition(
        name="echo",
        description="",
        parameters=[ToolParameter(name="who", type="str", description="", default="大家")],
        _run_func=run,
    )

    outcome = await _run(_engine(tmp_path, tool), _call("echo", "{not json"))

    assert outcome.ok
    assert seen == ["大家"]
    assert "echo: hi 大家" in outcome.content
    assert outcome.content.startswith("[Tool result: success]")


@pytest.mark.asyncio
async def test_tool_images_are_handed_to_the_next_model_round(tmp_path: Any) -> None:
    image = "data:image/png;base64,AAAA"
    tool = ToolDefinition(
        name="look",
        description="",
        _run_func=lambda **_: {
            "text": "看到了",
            "multimodal_blocks": [{"type": "image", "value": image}],
        },
    )

    outcome = await _run(_engine(tmp_path, tool), _call("look"))

    assert outcome.ok and "看到了" in outcome.content
    assert outcome.multimodal == [{"type": "image_url", "image_url": {"url": image}}]


@pytest.mark.asyncio
async def test_slow_tool_is_cut_off_at_the_configured_timeout(tmp_path: Any) -> None:
    import asyncio

    async def run(**_: Any) -> str:
        await asyncio.sleep(5)
        return "too late"

    tool = ToolDefinition(name="slow", description="", _run_func=run)
    engine = _engine(tmp_path, tool, config={"tool_execution_timeout": 0.05})

    outcome = await _run(engine, _call("slow"))

    assert outcome.executed and not outcome.ok
    assert "超时" in outcome.content
