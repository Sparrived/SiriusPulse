from __future__ import annotations

import pytest

from sirius_pulse.skills.builtin import chat_with_developer


class _FakeNapCatAdapter:
    def __init__(self, root: str = "123456") -> None:
        self.plugin_config = {"root": root}
        self.private_messages: list[tuple[str, str]] = []

    async def send_private_message(self, user_id: str, message: str) -> dict[str, object]:
        self.private_messages.append((user_id, message))
        return {"status": "ok", "message_id": 42}


@pytest.mark.asyncio
async def test_chat_with_developer_when_root_is_configured_then_sends_raw_private_message():
    adapter = _FakeNapCatAdapter(root="10001")

    message = "刚刚发生了一件很有趣的事，想跟你讲一下。"
    result = await chat_with_developer.run(
        message=message,
        bridge=adapter,
        chat_context={
            "chat_type": "group",
            "chat_id": "20002",
            "group_id": "20002",
            "user_id": "30003",
        },
    )

    assert result["success"] is True
    assert adapter.private_messages
    target, body = adapter.private_messages[0]
    assert target == "10001"
    assert body == message
    assert "通知" not in body
    assert "紧急度" not in body


@pytest.mark.asyncio
async def test_chat_with_developer_when_root_is_missing_then_returns_clear_failure():
    adapter = _FakeNapCatAdapter(root="")

    result = await chat_with_developer.run(message="hello", bridge=adapter)

    assert result["success"] is False
    assert "root QQ" in result["error"]
    assert adapter.private_messages == []
