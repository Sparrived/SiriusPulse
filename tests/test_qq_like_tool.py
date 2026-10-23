"""Tests for the first-class NapCat QQ like tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.tools import ToolExecutor, ToolInvocationContext, ToolRegistry


class _FakeAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def call_api(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, params))
        if self.fail:
            raise RuntimeError("NapCat unavailable")
        return {"status": "ok", "retcode": 0}


def _context(user_id: str = "10001") -> ToolInvocationContext:
    return ToolInvocationContext(caller=UnifiedUser(user_id=user_id, name=user_id))


def _tool(tmp_path: Path):
    registry = ToolRegistry()
    registry.load_from_directory(
        tmp_path / "tools",
        auto_install_deps=False,
        include_builtin=True,
    )
    tool = registry.get("napcat_call")
    assert tool is not None
    return tool


@pytest.mark.asyncio
async def test_qq_like_calls_napcat_and_reuses_last_success(tmp_path: Path):
    adapter = _FakeAdapter()
    executor = ToolExecutor(tmp_path)
    executor.set_bridge("napcat", adapter)
    executor.set_chat_context(group_id="group-1", user_id="10001", adapter_type="napcat")
    tool = _tool(tmp_path)

    first = await executor.execute_async(
        tool,
        {"action": "send_like", "user_id": 1703967297, "times": 2},
        invocation_context=_context(),
    )
    repeated = await executor.execute_async(
        tool,
        {"action": "send_like", "reuse_last": True},
        invocation_context=_context("20002"),
    )

    assert first.success is True
    assert repeated.success is True
    assert adapter.calls == [
        ("send_like", {"user_id": 1703967297, "times": 2}),
        ("send_like", {"user_id": 1703967297, "times": 1}),
    ]
    assert repeated.data["internal_metadata"]["reused_last"] is True


@pytest.mark.asyncio
async def test_qq_like_does_not_persist_failed_target(tmp_path: Path):
    adapter = _FakeAdapter(fail=True)
    executor = ToolExecutor(tmp_path)
    executor.set_bridge("napcat", adapter)
    executor.set_chat_context(group_id="group-1", user_id="10001", adapter_type="napcat")

    result = await executor.execute_async(
        _tool(tmp_path),
        {"action": "send_like", "user_id": 1703967297},
        invocation_context=_context(),
    )

    assert result.success is False
    assert executor.get_data_store("napcat_call").get("last_success:group:group-1") is None


@pytest.mark.asyncio
async def test_qq_like_normalizes_napcat_internal_caller_id(tmp_path: Path):
    adapter = _FakeAdapter()
    executor = ToolExecutor(tmp_path)
    executor.set_bridge("napcat", adapter)
    executor.set_chat_context(group_id="group-1", user_id="qq_10001", adapter_type="napcat")

    result = await executor.execute_async(
        _tool(tmp_path),
        {"action": "send_like"},
        invocation_context=_context("qq_10001"),
    )

    assert result.success is True
    assert adapter.calls == [("send_like", {"user_id": 10001, "times": 1})]
