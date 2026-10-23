"""群成员查询、群管理与点赞统一收敛到 ``napcat_call`` 后的业务行为测试。"""

from __future__ import annotations

from typing import Any

import pytest

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.tools.builtin import napcat_call
from sirius_pulse.tools.models import ToolInvocationContext

GROUP_CONTEXT = {"chat_type": "group", "chat_id": "9001", "group_id": "9001"}


class FakeNapcatAdapter:
    """只实现 ``call_api`` 的最小适配器，记录每次 OneBot action 与参数。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, action: str, params: dict[str, Any]) -> Any:
        self.calls.append((action, dict(params)))
        if action == "get_group_member_list":
            return [
                {"user_id": 1001, "nickname": "Alice", "card": "A", "role": "member"},
                {"user_id": 1002, "nickname": "Bob", "role": "admin"},
            ]
        if action == "get_group_member_info":
            return {"user_id": 1002, "nickname": "Bob", "card": "B", "role": "admin"}
        return {"status": "ok", "retcode": 0}


def _developer_context() -> ToolInvocationContext:
    return ToolInvocationContext(
        caller=UnifiedUser(user_id="dev", name="开发者", metadata={"is_developer": True})
    )


def _member_context() -> ToolInvocationContext:
    return ToolInvocationContext(caller=UnifiedUser(user_id="u1", name="普通群友"))


@pytest.mark.asyncio
async def test_qq_member_queries_use_current_group_and_return_model_readable_text():
    adapter = FakeNapcatAdapter()

    members = await napcat_call.run(
        action="list_members", bridge=adapter, chat_context=GROUP_CONTEXT
    )
    member_info = await napcat_call.run(
        action="get_member",
        params={"user_id": 1002},
        bridge=adapter,
        chat_context=GROUP_CONTEXT,
    )

    assert members["success"] is True
    assert "1001" in members["text_blocks"][0]
    assert "Alice" in members["text_blocks"][0]
    assert member_info["success"] is True
    assert "Bob" in member_info["text_blocks"][0]
    assert adapter.calls == [
        ("get_group_member_list", {"group_id": 9001}),
        ("get_group_member_info", {"group_id": 9001, "user_id": 1002}),
    ]


@pytest.mark.asyncio
async def test_qq_admin_actions_from_developer_hit_expected_onebot_api():
    adapter = FakeNapcatAdapter()
    developer = _developer_context()

    kick = await napcat_call.run(
        action="kick",
        params={"user_id": 1001},
        bridge=adapter,
        chat_context=GROUP_CONTEXT,
        invocation_context=developer,
    )
    mute = await napcat_call.run(
        action="mute_member",
        params={"user_id": 1001, "duration": 60},
        bridge=adapter,
        chat_context=GROUP_CONTEXT,
        invocation_context=developer,
    )
    mute_all = await napcat_call.run(
        action="mute_all",
        params={"enable": False},
        bridge=adapter,
        chat_context=GROUP_CONTEXT,
        invocation_context=developer,
    )
    card = await napcat_call.run(
        action="set_group_card",
        params={"user_id": 1001, "card": "NewCard"},
        bridge=adapter,
        chat_context=GROUP_CONTEXT,
        invocation_context=developer,
    )

    assert [kick["success"], mute["success"], mute_all["success"], card["success"]] == [
        True,
        True,
        True,
        True,
    ]
    assert adapter.calls == [
        ("set_group_kick", {"user_id": 1001, "group_id": 9001}),
        ("set_group_ban", {"user_id": 1001, "duration": 60, "group_id": 9001}),
        ("set_group_whole_ban", {"enable": False, "group_id": 9001}),
        ("set_group_card", {"user_id": 1001, "card": "NewCard", "group_id": 9001}),
    ]


@pytest.mark.asyncio
async def test_qq_admin_action_is_refused_for_non_developer_caller():
    adapter = FakeNapcatAdapter()

    result = await napcat_call.run(
        action="kick",
        params={"user_id": 1001},
        bridge=adapter,
        chat_context=GROUP_CONTEXT,
        invocation_context=_member_context(),
    )

    assert result["success"] is False
    assert "权限不足" in result["error"]
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_qq_send_like_reaches_napcat_send_like_api():
    adapter = FakeNapcatAdapter()

    result = await napcat_call.run(
        action="send_like",
        params={"user_id": 1703967297, "times": 3},
        bridge=adapter,
        chat_context=GROUP_CONTEXT,
    )

    assert result["success"] is True
    assert adapter.calls == [("send_like", {"user_id": 1703967297, "times": 3})]
