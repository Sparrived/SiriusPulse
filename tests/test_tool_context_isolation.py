"""跨群并发下的调用上下文隔离。

框架同时服务多个群，工具执行器却曾把「当前聊天上下文」存成一份共享可变
dict。并发时后到达的群会覆盖先到达的群，于是工具看到的位置、以及管理类
工具的管理员判定都可能落在**错误的群**上。这里锁定修复后的业务行为：
每次调用用自己的身份，共享 setter 只作为兜底。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from sirius_pulse.tools import (
    ToolDefinition,
    ToolExecutor,
    ToolInvocationContext,
    ToolParameter,
)
from sirius_pulse.tools.models import build_chat_context


class _AdminEngine:
    """假引擎：只有 group-ok 是 Bot 担任管理员的群。"""

    def __init__(self) -> None:
        self.checked: list[str] = []

    def is_qq_bot_group_admin(self, group_id: str) -> bool:
        self.checked.append(group_id)
        return group_id == "group-ok"


def _tool(name: str, run_func, *, admin_required: bool = False) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="测试工具",
        parameters=[],
        admin_required=admin_required,
        inject_runtime_params=True,
        _run_func=run_func,
    )


def _executor(tmp_path: Path) -> ToolExecutor:
    executor = ToolExecutor(work_path=tmp_path)
    executor.set_engine_context(_AdminEngine())
    return executor


def _ctx(group_id: str, *, adapter_type: str = "napcat") -> ToolInvocationContext:
    return ToolInvocationContext(group_id=group_id, adapter_type=adapter_type)


# ── build_chat_context：单一推导来源 ─────────────────────────────────


def test_build_chat_context_derives_group_and_private_shapes():
    group = build_chat_context(group_id="12345", user_id="u1", adapter_type="napcat")
    assert group == {
        "group_id": "12345",
        "user_id": "u1",
        "chat_type": "group",
        "chat_id": "12345",
        "is_private": False,
        "adapter_type": "napcat",
    }

    private = build_chat_context(group_id="private_qq_678", user_id="u2")
    assert private["chat_type"] == "private"
    assert private["chat_id"] == "678"
    assert private["is_private"] is True


def test_invocation_context_chat_context_matches_explicit_derivation():
    ctx = _ctx("98765")
    assert ctx.chat_context == build_chat_context(group_id="98765", adapter_type="napcat")


# ── 工具实际看到的位置 ──────────────────────────────────────────────


def test_tool_sees_its_own_group_not_the_shared_setter_value(tmp_path: Path):
    seen: list[dict[str, Any]] = []

    def run(chat_context: dict[str, Any] | None = None) -> dict[str, Any]:
        seen.append(dict(chat_context or {}))
        return {"success": True, "text": "ok"}

    executor = _executor(tmp_path)
    # 共享兜底被另一个群覆盖——正是并发下的真实情形。
    executor.set_chat_context(group_id="group-stale", user_id="other", adapter_type="napcat")

    result = executor.execute(_tool("where", run), {}, invocation_context=_ctx("group-real"))

    assert result.success is True
    assert seen == [build_chat_context(group_id="group-real", adapter_type="napcat")]


def test_tool_falls_back_to_shared_context_when_invocation_has_no_group(tmp_path: Path):
    seen: list[dict[str, Any]] = []

    def run(chat_context: dict[str, Any] | None = None) -> dict[str, Any]:
        seen.append(dict(chat_context or {}))
        return {"success": True, "text": "ok"}

    executor = _executor(tmp_path)
    executor.set_chat_context(group_id="group-fallback", user_id="u9", adapter_type="napcat")

    executor.execute(_tool("where", run), {}, invocation_context=ToolInvocationContext())

    assert seen == [
        build_chat_context(group_id="group-fallback", user_id="u9", adapter_type="napcat")
    ]


# ── 管理类工具的鉴权归属 ────────────────────────────────────────────


def test_admin_tool_is_authorized_against_its_own_group(tmp_path: Path):
    def run(**_: Any) -> dict[str, Any]:
        return {"success": True, "text": "已执行"}

    executor = _executor(tmp_path)
    tool = _tool("group_management", run, admin_required=True)
    # 共享上下文指向「Bot 是管理员」的群，本次调用却来自另一个群。
    executor.set_chat_context(group_id="group-ok", adapter_type="napcat")

    result = executor.execute(tool, {}, invocation_context=_ctx("group-other"))

    assert result.success is False
    assert "管理员" in (result.error or "")
    assert executor._engine_context.checked == ["group-other"]


def test_admin_tool_runs_when_its_own_group_grants_admin(tmp_path: Path):
    def run(**_: Any) -> dict[str, Any]:
        return {"success": True, "text": "已执行"}

    executor = _executor(tmp_path)
    tool = _tool("group_management", run, admin_required=True)
    executor.set_chat_context(group_id="group-other", adapter_type="napcat")

    result = executor.execute(tool, {}, invocation_context=_ctx("group-ok"))

    assert result.success is True
    assert executor._engine_context.checked == ["group-ok"]


def test_admin_tool_is_refused_in_private_chat(tmp_path: Path):
    def run(**_: Any) -> dict[str, Any]:
        return {"success": True, "text": "已执行"}

    executor = _executor(tmp_path)
    tool = _tool("group_management", run, admin_required=True)
    executor.set_chat_context(group_id="group-ok", adapter_type="napcat")

    result = executor.execute(tool, {}, invocation_context=_ctx("private_qq_1"))

    assert result.success is False
    assert "群聊" in (result.error or "")


def test_concurrent_calls_for_two_groups_do_not_cross_contaminate(tmp_path: Path):
    """两个群的调用交错时，各自都必须看到自己的群。"""
    seen: list[tuple[str, str]] = []

    async def run(label: str, chat_context: dict[str, Any] | None = None) -> dict[str, Any]:
        # 在工具内部让出事件循环，制造与另一群交错的机会。
        await asyncio.sleep(0)
        seen.append((label, str((chat_context or {}).get("group_id", ""))))
        return {"success": True, "text": label}

    executor = _executor(tmp_path)
    tool = _tool("where", run)
    tool.parameters = [ToolParameter(name="label", type="str", description="标签", required=True)]

    async def main() -> None:
        await asyncio.gather(
            executor.execute_async(tool, {"label": "a"}, invocation_context=_ctx("group-a")),
            executor.execute_async(tool, {"label": "b"}, invocation_context=_ctx("group-b")),
        )

    asyncio.run(main())

    assert sorted(seen) == [("a", "group-a"), ("b", "group-b")]


# ── 参数注入清单仍然完整 ────────────────────────────────────────────


def test_chat_context_is_only_injected_when_tool_accepts_it(tmp_path: Path):
    captured: list[dict[str, Any]] = []

    def run(x: str = "") -> dict[str, Any]:
        captured.append({"x": x})
        return {"success": True, "text": "ok"}

    executor = _executor(tmp_path)
    tool = _tool("no_ctx", run)
    tool.parameters = [ToolParameter(name="x", type="str", description="x", required=False)]

    executor.execute(tool, {"x": "1"}, invocation_context=_ctx("group-a"))

    assert captured == [{"x": "1"}]
    assert "chat_context" not in captured[0]
