"""业务视角：工具的行为由它自己的 TOOL_META 决定，不由名字决定。

框架里不再有「叫这个名字就特殊处理」的名单。一个工具声明了什么，就得到什么；
但会放宽权限的声明（互动豁免、指令来源绑定、私人记录……）只对包内内置工具生效，
工作区里照抄同名同声明的工具拿不到。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sirius_pulse.core.tool_call_runner import ToolCallRunner
from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.providers.base import ToolCall
from sirius_pulse.tools import ToolExecutor, ToolRegistry
from sirius_pulse.tools.models import ToolInvocationContext, ToolSideEffect

_WORKSPACE_TOOL = """
TOOL_META = {
    "name": "diary_sync",
    "description": "把日记同步到云端",
    "side_effect": "external_write",
    "work_mode_only": True,
    "redact_logs": True,
    "engagement_exempt": True,
    "bookkeeping": True,
    "request_source": True,
    "actions": {
        "peek": {"side_effect": "read_only", "retry_safe": True},
        "push": {"silent": True},
    },
    "parameters": {"action": {"type": "str", "required": True}, "secret": {"type": "str"}},
}


def run(action: str, secret: str = "", **kwargs):
    return {"done": action}
"""


def _workspace_tool(tmp_path: Path) -> Any:
    path = tmp_path / "tools" / "diary_sync.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_WORKSPACE_TOOL, encoding="utf-8")
    return ToolRegistry._load_tool_file(path)


def _builtin(name: str) -> Any:
    return ToolRegistry._load_tool_file(ToolRegistry.builtin_tools_dir() / f"{name}.py")


def test_workspace_tool_gets_what_it_declares_except_privileges(tmp_path: Path) -> None:
    tool = _workspace_tool(tmp_path)

    # 收紧或描述性的声明照常生效。
    assert tool.work_mode_only and tool.redact_logs
    assert tool.side_effect_for({"action": "peek"}) is ToolSideEffect.READ_ONLY
    assert tool.retry_safe_for({"action": "peek"}) is True
    assert tool.side_effect_for({"action": "push"}) is ToolSideEffect.EXTERNAL_WRITE
    assert tool.silent_for({"action": "push"}) is True
    # 放宽权限的声明对工作区工具无效。
    assert tool.trusted is False
    assert tool.engagement_exempt_for({"action": "push"}) is False
    assert tool.is_bookkeeping is False
    assert tool.needs_request_source({"action": "push"}) is False


@pytest.mark.parametrize(
    ("work_mode", "visible"),
    [(False, ["lookup"]), (True, ["diary_sync", "lookup"])],
)
def test_heavy_tools_only_appear_in_work_mode(
    tmp_path: Path, work_mode: bool, visible: list[str]
) -> None:
    registry = ToolRegistry()
    registry.register(_workspace_tool(tmp_path))
    lookup = tmp_path / "tools" / "lookup.py"
    lookup.write_text(
        'TOOL_META = {"name": "lookup", "description": "查资料"}\n'
        "def run(**kwargs):\n    return 'ok'\n",
        encoding="utf-8",
    )
    registry.register(ToolRegistry._load_tool_file(lookup))

    tools = registry.build_tools_list(work_mode=work_mode)

    assert sorted(t["function"]["name"] for t in tools) == visible


def test_redacted_tool_keeps_its_arguments_out_of_telemetry(tmp_path: Path) -> None:
    executor = ToolExecutor(tmp_path)

    executor.execute(_workspace_tool(tmp_path), {"action": "push", "secret": "日记里的秘密"})

    telemetry = (tmp_path / "tool_data" / ".telemetry.jsonl").read_text(encoding="utf-8")
    record = json.loads(telemetry.strip().splitlines()[-1])
    assert record["tool_name"] == "diary_sync" and record["success"] is True
    assert "日记里的秘密" not in telemetry
    assert record["params"] == {"action": "<redacted>", "secret": "<redacted>"}


def test_builtin_declarations_drive_the_privileged_behaviour() -> None:
    notebook = _builtin("persona_notebook")
    person_query = _builtin("person_query")
    image = _builtin("image_generate")

    assert notebook.trusted and notebook.is_bookkeeping and notebook.chat_scoped
    assert notebook.engagement_exempt_for({}) is True
    # 查人要确认是谁问的；按明确 ID 查则不必选来源。
    assert person_query.needs_request_source({"target": {"ref": "requester"}}) is True
    assert person_query.request_source_optional({"target": {"ref": "person_id"}}) is True
    assert person_query.request_source_optional({"target": {"ref": "requester"}}) is False
    # 生图只有画具体人物时才需要来源。
    assert image.needs_request_source({"prompt": "海边"}) is False
    assert image.needs_request_source({"prompt": "我", "subjects": [{"ref": "requester"}]})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "params", "expected"),
    [
        ("organize", {}, [("g1", True)]),  # 明确要求整理
        ("add", {"entry_id": "likes", "text": "喜欢猫"}, []),  # 写进去了，不用整理
    ],
)
async def test_notebook_asks_for_tidying_only_when_it_needs_it(
    tmp_path: Path, action: str, params: dict, expected: list
) -> None:
    scheduled: list[tuple[str, bool]] = []
    engine_context = SimpleNamespace(
        get_work_path=lambda: str(tmp_path),
        get_config_value=lambda key, default=None: default,
        schedule_notebook_maintenance=lambda group_id, *, force=False: scheduled.append(
            (group_id, force)
        ),
    )
    executor = ToolExecutor(tmp_path)
    executor.set_engine_context(engine_context)
    registry = ToolRegistry()
    registry.register(_builtin("persona_notebook"))
    engine = SimpleNamespace(_tool_registry=registry, _tool_executor=executor, config={})
    runner = ToolCallRunner(engine)
    context = ToolInvocationContext(
        caller=UnifiedUser(user_id="u1", name="小明"), group_id="g1", adapter_type="napcat"
    )
    call = ToolCall(
        id="c1",
        function_name="persona_notebook",
        function_arguments=json.dumps({"action": action, **params}),
    )

    outcome = await runner.execute(runner.prepare(call, context), group_id="g1")

    assert outcome.ok, outcome.content
    assert scheduled == expected


def test_every_argument_she_can_fill_in_is_explained() -> None:
    """她看得到的每个内置工具参数都要有说明，不然只能靠猜，猜错就被拒、再重试。"""
    registry = ToolRegistry()
    registry._load_builtin_tools(auto_install_deps=False)
    unexplained = [
        f"{tool.name}.{name}"
        for tool in registry.all_tools()
        if tool.model_visible
        for name, spec in tool.to_tool_schema()["function"]["parameters"]
        .get("properties", {})
        .items()
        if not str(spec.get("description", "")).strip()
    ]

    assert unexplained == []
