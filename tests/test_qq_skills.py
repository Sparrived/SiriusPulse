from __future__ import annotations

from typing import Any

import pytest

from sirius_pulse.skills.builtin import (
    get_group_members,
    get_member_info,
    kick_member,
    mute_all,
    mute_member,
    poke,
    recall_message,
    set_group_card,
)


class FakeQQAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def send_poke(self, user_id: str, group_id: str = "") -> dict[str, Any]:
        self.calls.append(("send_poke", (user_id, group_id)))
        return {"ok": True}

    async def delete_message(self, message_id: str) -> dict[str, Any]:
        self.calls.append(("delete_message", (message_id,)))
        return {"ok": True}

    async def get_group_member_list(self, group_id: str) -> list[dict[str, Any]]:
        self.calls.append(("get_group_member_list", (group_id,)))
        return [
            {"user_id": 1001, "nickname": "Alice", "card": "A", "role": "member"},
            {"user_id": 1002, "nickname": "Bob", "role": "admin"},
        ]

    async def get_group_member_info(self, group_id: str, user_id: str) -> dict[str, Any]:
        self.calls.append(("get_group_member_info", (group_id, user_id)))
        return {"user_id": int(user_id), "nickname": "Bob", "card": "B", "role": "admin"}

    async def set_group_kick(
        self, group_id: str, user_id: str, reject_add_request: bool = False
    ) -> dict[str, Any]:
        self.calls.append(("set_group_kick", (group_id, user_id, reject_add_request)))
        return {"ok": True}

    async def set_group_ban(self, group_id: str, user_id: str, duration: int) -> dict[str, Any]:
        self.calls.append(("set_group_ban", (group_id, user_id, duration)))
        return {"ok": True}

    async def set_group_whole_ban(self, group_id: str, enable: bool = True) -> dict[str, Any]:
        self.calls.append(("set_group_whole_ban", (group_id, enable)))
        return {"ok": True}

    async def set_group_card(self, group_id: str, user_id: str, card: str = "") -> dict[str, Any]:
        self.calls.append(("set_group_card", (group_id, user_id, card)))
        return {"ok": True}


@pytest.mark.asyncio
async def test_qq_builtin_skills_call_expected_adapter_methods():
    adapter = FakeQQAdapter()
    ctx = {"chat_type": "group", "chat_id": "9001", "group_id": "9001"}

    assert (await poke.run(1001, bridge=adapter, chat_context=ctx))["success"] is True
    assert (await recall_message.run(42, bridge=adapter))["success"] is True
    members = await get_group_members.run(bridge=adapter, chat_context=ctx)
    member_info = await get_member_info.run(1002, bridge=adapter, chat_context=ctx)
    assert (await kick_member.run(1001, bridge=adapter, chat_context=ctx))["success"] is True
    assert (await mute_member.run(1001, duration=60, bridge=adapter, chat_context=ctx))[
        "success"
    ] is True
    assert (await mute_all.run(False, bridge=adapter, chat_context=ctx))["success"] is True
    assert (await set_group_card.run(1001, "NewCard", bridge=adapter, chat_context=ctx))[
        "success"
    ] is True

    assert "1001" in members["text_blocks"][0]
    assert "Bob" in member_info["text_blocks"][0]
    assert adapter.calls == [
        ("send_poke", ("1001", "9001")),
        ("delete_message", ("42",)),
        ("get_group_member_list", ("9001",)),
        ("get_group_member_info", ("9001", "1002")),
        ("set_group_kick", ("9001", "1001", False)),
        ("set_group_ban", ("9001", "1001", 60)),
        ("set_group_whole_ban", ("9001", False)),
        ("set_group_card", ("9001", "1001", "NewCard")),
    ]


@pytest.mark.asyncio
async def test_qq_group_skills_require_group_context():
    adapter = FakeQQAdapter()
    result = await poke.run(1001, bridge=adapter, chat_context={"chat_type": "private"})

    assert result["success"] is False
    assert adapter.calls == []
