from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.core.sticker_delivery import collect_deferred_stickers_from_tool_calls
from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter
from sirius_pulse.providers.base import ToolCall


def test_collect_deferred_stickers_when_send_sticker_tool_called_then_returns_known_names():
    tool_calls = [
        ToolCall(
            id="call-1",
            function_name="send_sticker",
            function_arguments='{"names": ["开心", "不存在", "开心"]}',
        ),
        ToolCall(
            id="call-2",
            function_name="poke",
            function_arguments='{"user_id": "100"}',
        ),
    ]

    assert collect_deferred_stickers_from_tool_calls(
        tool_calls,
        available_names=["开心"],
    ) == ["开心"]


def test_collect_deferred_stickers_when_interaction_sticker_tool_called_then_returns_known_names():
    tool_calls = [
        ToolCall(
            id="call-1",
            function_name="interaction",
            function_arguments='{"action": "sticker", "names": ["开心"]}',
        )
    ]

    assert collect_deferred_stickers_from_tool_calls(
        tool_calls,
        available_names=["开心"],
    ) == ["开心"]


@pytest.mark.asyncio
async def test_napcat_delayed_delivery_sends_text_before_sticker():
    adapter = NapCatAdapter("ws://example.invalid")
    order: list[str] = []

    async def fake_send_group_msg(group_id, message):
        order.append("text")
        return {"ok": True}

    async def fake_send_stickers(group_id, names):
        order.append("sticker")
        return {"ok": True}

    adapter.send_group_msg = fake_send_group_msg  # type: ignore[method-assign]
    adapter._engine = SimpleNamespace(
        tick_delayed_queue=AsyncMock(
            return_value=[
                {
                    "reply": "先说正文",
                    "reply_references": [],
                    "sticker_names": ["开心"],
                }
            ]
        ),
        _send_stickers_by_names=fake_send_stickers,
    )
    adapter._get_allowed_group_ids = lambda: ["100"]  # type: ignore[method-assign]

    await adapter._handle_event(
        SessionEvent(
            type=SessionEventType.DELAYED_RESPONSE_TRIGGERED,
            data={"group_id": "100"},
        )
    )

    assert order == ["text", "sticker"]
