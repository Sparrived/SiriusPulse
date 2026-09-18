"""引用消息的图片应作为真实视觉输入进入模型。"""

from __future__ import annotations

import pytest

from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter


def _group_event(message, *, group_id: str = "100") -> dict:
    return {
        "time": 1720000000,
        "post_type": "message",
        "message_type": "group",
        "group_id": group_id,
        "user_id": "300",
        "self_id": "100",
        "message_id": "message-1",
        "message": message,
        "sender": {"nickname": "Alice", "card": ""},
    }


def _adapter(monkeypatch: pytest.MonkeyPatch, quoted_message: list[dict] | None = None):
    adapter = NapCatAdapter(
        "ws://example.invalid",
        config={"allowed_group_ids": ["100"], "persona_name": "alpha", "qq_number": "100"},
    )

    async def fake_call_api(action: str, params: dict) -> dict:
        assert action == "get_msg"
        return {"data": {"message": quoted_message or [], "sender": {"nickname": "Bob"}}}

    monkeypatch.setattr(adapter, "call_api", fake_call_api)
    return adapter


@pytest.mark.asyncio
async def test_reply_quote_when_quoted_message_has_image_then_image_reaches_vision_input(
    monkeypatch: pytest.MonkeyPatch,
):
    """回复引用了一条图片消息时，模型应能真正看到那张图，而不只是文本标签。"""
    adapter = _adapter(
        monkeypatch,
        quoted_message=[
            {"type": "image", "data": {"url": "quoted.png"}},
            {"type": "text", "data": {"text": "这是上周的截图"}},
        ],
    )

    parsed = await adapter.parse_event(
        _group_event(
            [
                {"type": "reply", "data": {"id": "777"}},
                {"type": "text", "data": {"text": "这个怎么看"}},
            ]
        )
    )

    assert parsed is not None
    assert "这是上周的截图" in parsed.prompt
    assert parsed.multimodal_inputs == [
        {"type": "image", "value": "quoted.png", "file_path": "quoted.png"}
    ]


@pytest.mark.asyncio
async def test_reply_quote_when_quoted_message_is_image_only_then_keeps_quote_and_image(
    monkeypatch: pytest.MonkeyPatch,
):
    """被引用消息只有图片、没有文字时，也不应丢掉引用信息与图片。"""
    adapter = _adapter(
        monkeypatch,
        quoted_message=[{"type": "image", "data": {"url": "only.png"}}],
    )

    parsed = await adapter.parse_event(_group_event([{"type": "reply", "data": {"id": "778"}}]))

    assert parsed is not None
    assert 'msg_id="778"' in parsed.prompt
    assert "图片" in parsed.prompt
    assert [item["value"] for item in parsed.multimodal_inputs] == ["only.png"]


@pytest.mark.asyncio
async def test_message_with_text_and_image_then_both_are_marked_as_vision_input(
    monkeypatch: pytest.MonkeyPatch,
):
    """图文同条发送时，图片应进入视觉输入。"""
    adapter = _adapter(monkeypatch)

    parsed = await adapter.parse_event(
        _group_event(
            [
                {"type": "text", "data": {"text": "看这个"}},
                {"type": "image", "data": {"url": "attached.png"}},
            ]
        )
    )

    assert parsed is not None
    assert [item["value"] for item in parsed.multimodal_inputs] == ["attached.png"]
