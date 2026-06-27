from __future__ import annotations

import pytest

from sirius_pulse.skills.builtin import notify_developer


class _FakeNapCatAdapter:
    def __init__(self, root: str = "123456") -> None:
        self.plugin_config = {"root": root}
        self.private_messages: list[tuple[str, str]] = []

    async def send_private_message(self, user_id: str, message: str) -> dict[str, object]:
        self.private_messages.append((user_id, message))
        return {"status": "ok", "message_id": 42}


@pytest.mark.asyncio
async def test_notify_developer_when_root_is_configured_then_sends_private_message():
    adapter = _FakeNapCatAdapter(root="10001")

    result = await notify_developer.run(
        message="刚刚发生了一件很有趣的事，想告诉你。",
        emotion="开心",
        reason="这件事明显触发了强烈情绪",
        urgency="high",
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
    assert "【人格主动通知】" in body
    assert "情绪：开心" in body
    assert "紧急度：high" in body
    assert "来源：group 20002" in body
    assert "触发用户：30003" in body
    assert "刚刚发生了一件很有趣的事" in body


@pytest.mark.asyncio
async def test_notify_developer_when_root_is_missing_then_returns_clear_failure():
    adapter = _FakeNapCatAdapter(root="")

    result = await notify_developer.run(message="hello", bridge=adapter)

    assert result["success"] is False
    assert "root QQ" in result["error"]
    assert adapter.private_messages == []
