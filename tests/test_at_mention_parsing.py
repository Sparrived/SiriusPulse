from __future__ import annotations

import pytest

from sirius_pulse.adapters.models import AtSegment, TextSegment
from sirius_pulse.core.qq_mentions import build_qq_mention_section, parse_qq_at_mentions
from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter


def test_parse_qq_at_mentions_when_id_is_known_then_builds_at_segment():
    group = parse_qq_at_mentions("hi @{123} and @{999}", valid_user_ids={"123"})

    assert group is not None
    assert isinstance(group[0], TextSegment)
    assert group[0].text == "hi "
    assert isinstance(group[1], AtSegment)
    assert group[1].user_id == "123"
    assert isinstance(group[2], TextSegment)
    assert group[2].text == " and "
    assert isinstance(group[3], TextSegment)
    assert group[3].text == "@{999}"


def test_parse_qq_at_mentions_accepts_bare_numeric_model_output():
    group = parse_qq_at_mentions("hi @123456 and @qq_789012")

    assert group is not None
    assert isinstance(group[1], AtSegment)
    assert group[1].user_id == "123456"
    assert isinstance(group[3], AtSegment)
    assert group[3].user_id == "789012"


def test_parse_qq_at_mentions_keeps_unknown_bare_numeric_when_ids_are_known():
    group = parse_qq_at_mentions("hi @123456 and @789012", valid_user_ids={"123456"})

    assert group is not None
    assert isinstance(group[1], AtSegment)
    assert group[1].user_id == "123456"
    assert isinstance(group[3], TextSegment)
    assert group[3].text == "@789012"


def test_parse_qq_at_mentions_does_not_split_long_numeric_ids():
    assert parse_qq_at_mentions("hi @1234567890123") is None


def test_build_qq_mention_section_lists_member_ids_and_syntax():
    section = build_qq_mention_section(
        [
            {"user_id": 123, "nickname": "Alice"},
            {"user_id": 456, "card": "BobCard", "role": "admin"},
        ]
    )

    assert "@{QQ号}" in section
    assert "不要编造" in section


@pytest.mark.asyncio
async def test_napcat_group_text_sender_converts_inline_at_marker():
    adapter = NapCatAdapter("ws://example.invalid")
    adapter._group_member_cache["100"] = (
        10**12,
        [{"user_id": 123, "nickname": "Alice"}],
    )
    sent: list[tuple[str, object]] = []

    async def fake_send_group_msg(group_id, message):
        sent.append((str(group_id), message))
        return {"ok": True}

    adapter.send_group_msg = fake_send_group_msg  # type: ignore[method-assign]

    ok = await adapter._send_group_text("100", "hi @{123} @{999}")

    assert ok is True
    assert sent == [
        (
            "100",
            [
                {"type": "text", "data": {"text": "hi "}},
                {"type": "at", "data": {"qq": "123"}},
                {"type": "text", "data": {"text": " "}},
                {"type": "text", "data": {"text": "@{999}"}},
            ],
        )
    ]


@pytest.mark.asyncio
async def test_napcat_group_text_sender_converts_bare_numeric_at_without_member_cache():
    adapter = NapCatAdapter("ws://example.invalid")
    sent: list[tuple[str, object]] = []

    async def fake_send_group_msg(group_id, message):
        sent.append((str(group_id), message))
        return {"ok": True}

    adapter.send_group_msg = fake_send_group_msg  # type: ignore[method-assign]

    ok = await adapter._send_group_text("100", "hi @123456")

    assert ok is True
    assert sent == [
        (
            "100",
            [
                {"type": "text", "data": {"text": "hi "}},
                {"type": "at", "data": {"qq": "123456"}},
            ],
        )
    ]
