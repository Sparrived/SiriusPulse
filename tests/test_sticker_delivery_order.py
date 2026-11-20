from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("sticker_accepted", [True, False])
@pytest.mark.parametrize("sticker_names", [["开心"], ["开心", "得意"]])
async def test_napcat_delayed_delivery_sends_text_before_sticker(sticker_accepted, sticker_names):
    adapter = NapCatAdapter("ws://example.invalid")
    order: list[str] = []
    sticker_calls: list[list[str]] = []

    async def fake_send_group_msg(group_id, message):
        order.append("text")
        return {"status": "ok", "retcode": 0}

    async def fake_send_stickers(group_id, names, *, adapter=None):
        assert adapter is not None
        sticker_calls.append(list(names))
        order.append("sticker")
        return {"success": sticker_accepted}

    async def fake_send_poke(user_id, group_id):
        order.append("poke")
        return {"status": "ok", "retcode": 0}

    adapter.send_group_msg = fake_send_group_msg  # type: ignore[method-assign]
    adapter.send_poke = fake_send_poke  # type: ignore[method-assign]
    result = {
        "reply": "先说正文",
        "reply_references": [],
        "sticker_names": sticker_names,
        "poke_user_ids": ["1001"],
    }
    emit = AsyncMock()
    adapter._engine = SimpleNamespace(
        tick_delayed_queue=AsyncMock(return_value=[result]),
        send_stickers_by_names=fake_send_stickers,
        event_bus=SimpleNamespace(emit=emit),
    )
    adapter._get_allowed_group_ids = lambda: ["100"]  # type: ignore[method-assign]

    await adapter._handle_event(
        SessionEvent(
            type=SessionEventType.DELAYED_RESPONSE_TRIGGERED,
            data={"group_id": "100"},
        )
    )

    assert order == (["text", "sticker", "poke"] if sticker_accepted else ["text", "sticker"])
    assert sticker_calls == [list(sticker_names)]
    assert result["delivery_cursor"] == (len(result["delivery_parts"]) if sticker_accepted else 1)
    emit.assert_awaited_once()
    event = emit.call_args.args[0]
    assert event.type == SessionEventType.AGENT_TURN_UPDATED
    assert event.data["delivery_status"] == ("sent" if sticker_accepted else "pending")
    assert event.data["phase"] == ("complete" if sticker_accepted else "verify")


@pytest.mark.asyncio
@pytest.mark.parametrize("group_allowed", [True, False])
async def test_napcat_delayed_delivery_resumes_from_cursor_without_resending(group_allowed):
    adapter = NapCatAdapter("ws://example.invalid")
    adapter.send_group_msg = AsyncMock(return_value={"status": "ok", "retcode": 0})
    adapter.send_poke = AsyncMock(return_value={"status": "ok", "retcode": 0})
    send_stickers = AsyncMock(return_value={"success": True})
    result = {
        "reply": "正文",
        "sticker_names": ["开心", "得意"],
        "delivery_parts": [
            {"kind": "text", "value": "正文"},
            {"kind": "sticker", "value": "开心"},
            {"kind": "sticker", "value": "得意"},
            {"kind": "poke", "value": "1001"},
        ],
        "delivery_cursor": 2,
    }
    adapter._engine = SimpleNamespace(
        tick_delayed_queue=AsyncMock(return_value=[result]),
        send_stickers_by_names=send_stickers,
    )
    adapter.plugin_config["allowed_group_ids"] = ["100"] if group_allowed else ["200"]

    await adapter._handle_event(
        SessionEvent(
            type=SessionEventType.DELAYED_RESPONSE_TRIGGERED,
            data={"group_id": "100"},
        )
    )

    adapter.send_group_msg.assert_not_awaited()
    send_stickers.assert_not_awaited()
    if group_allowed:
        adapter.send_poke.assert_awaited_once_with("1001", "100")
        assert result["delivery_cursor"] == 4
    else:
        adapter.send_poke.assert_not_awaited()
        assert result["delivery_cursor"] < 4


@pytest.mark.asyncio
async def test_napcat_private_qq_sticker_effect_uses_canonical_private_target():
    adapter = NapCatAdapter("ws://example.invalid")
    targets: list[str] = []

    async def fake_send_stickers(group_id, names, *, adapter=None):
        targets.append(str(group_id))
        return {"success": True}

    adapter._engine = SimpleNamespace(send_stickers_by_names=fake_send_stickers)

    accepted = await adapter._deliver_proactive_payload(
        group_id="private_qq_200",
        reply="",
        reply_refs=[],
        image_path="",
        sticker_names=["开心"],
        poke_user_ids=[],
    )

    assert accepted is True
    assert targets == ["private_200"]
