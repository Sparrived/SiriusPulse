from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("sticker_accepted", [True, False])
async def test_napcat_delayed_delivery_sends_text_before_sticker(sticker_accepted):
    adapter = NapCatAdapter("ws://example.invalid")
    order: list[str] = []

    async def fake_send_group_msg(group_id, message):
        order.append("text")
        return {"status": "ok", "retcode": 0}

    async def fake_send_stickers(group_id, names, *, adapter=None):
        assert adapter is not None
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
        "sticker_names": ["开心"],
        "poke_user_ids": ["1001"],
    }
    emit = AsyncMock()
    adapter._engine = SimpleNamespace(
        tick_delayed_queue=AsyncMock(return_value=[result]),
        _send_stickers_by_names=fake_send_stickers,
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
    assert result["delivery_cursor"] == (3 if sticker_accepted else 1)
    emit.assert_awaited_once()
    event = emit.call_args.args[0]
    assert event.type == SessionEventType.AGENT_TURN_UPDATED
    assert event.data["delivery_status"] == ("sent" if sticker_accepted else "pending")
    assert event.data["phase"] == ("complete" if sticker_accepted else "verify")


@pytest.mark.asyncio
async def test_napcat_private_qq_sticker_effect_uses_canonical_private_target():
    adapter = NapCatAdapter("ws://example.invalid")
    targets: list[str] = []

    async def fake_send_stickers(group_id, names, *, adapter=None):
        targets.append(str(group_id))
        return {"success": True}

    adapter._engine = SimpleNamespace(_send_stickers_by_names=fake_send_stickers)

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
