"""交互标记解析与主动消息出口的回归测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase
from sirius_pulse.core.helpers import Helpers
from sirius_pulse.core.interaction_markers import parse_interaction_markers
from sirius_pulse.memory.basic import BasicMemoryManager


class _RecordingBrain:
    def __init__(self) -> None:
        self.post_hooks: list[tuple[object, int, set[str] | None]] = []

    def register_post_hook(
        self,
        hook: object,
        priority: int = 100,
        task_filter: set[str] | None = None,
    ) -> None:
        self.post_hooks.append((hook, priority, task_filter))


def _interaction_marker_hook() -> tuple[object, object]:
    engine = SimpleNamespace(
        brain=_RecordingBrain(),
        basic_memory=BasicMemoryManager(),
        _sticker_names=["开心", "疑惑"],
        get_qq_group_members_for_prompt=lambda _group_id: [{"user_id": "1001"}],
    )
    _EmotionalGroupChatEngineBase._register_engine_hooks(engine)
    for hook, priority, _task_filter in engine.brain.post_hooks:
        if priority == 20:
            return hook, engine
    raise AssertionError("interaction marker hook was not registered")


@pytest.mark.parametrize(
    "marker",
    [
        "[STICKER:得意]",
        "[STICKER：得意]",
        "【STICKER:得意】",
        "［STICKER：得意］",
        "[STICKERS:得意]",
        "[sticker: '得意']",
    ],
)
def test_sticker_marker_variants_are_all_recognized(marker: str) -> None:
    markers = parse_interaction_markers(f"{marker} 画好啦", available_stickers=["得意", "开心"])

    assert markers.text == "画好啦"
    assert markers.sticker_names == ["得意"]


def test_sticker_marker_keeps_only_names_in_vocabulary() -> None:
    markers = parse_interaction_markers("[STICKER:得意、不存在、开心] 正文", available_stickers=["得意", "开心"])

    assert markers.text == "正文"
    assert markers.sticker_names == ["得意", "开心"]


def test_sticker_marker_is_stripped_when_name_is_unknown() -> None:
    markers = parse_interaction_markers("[STICKER:得意] 正文", available_stickers=[])

    assert markers.text == "正文"
    assert markers.sticker_names == []


def test_poke_marker_only_keeps_allowed_ids() -> None:
    markers = parse_interaction_markers("[POKE:1001][POKE:2002] 正文", allowed_poke_ids={"1001"})

    assert markers.text == "正文"
    assert markers.poke_user_ids == ["1001"]


class _RecordingEventBus:
    """只记录事件的极简事件总线。"""

    closed = False
    subscriber_count = 1
    supports_delivery_ack = False

    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, event: object) -> bool:
        self.events.append(event)
        return True


@pytest.mark.asyncio
async def test_proactive_text_sends_sticker_instead_of_literal_marker() -> None:
    bus = _RecordingEventBus()
    engine = SimpleNamespace(
        _sticker_names=["得意", "开心"],
        _current_adapter_type="",
        event_bus=bus,
        basic_memory=BasicMemoryManager(),
        get_qq_group_members_for_prompt=lambda _group_id: [],
    )

    accepted = await Helpers(engine).dispatch_proactive_message(
        group_id="100",
        text="[STICKER：得意] 画好啦",
        event_id="evt-1",
    )

    assert accepted is True
    event = bus.events[0]
    assert event.data["reply"] == "画好啦"
    assert event.data["sticker_names"] == ["得意"]


@pytest.mark.parametrize(
    "marker",
    [
        "[STICKER：开心]",
        "【STICKER:开心】",
        "［STICKER：开心］",
        "[STICKERS:开心]",
    ],
)
def test_chat_reply_hook_sends_sticker_for_fullwidth_variants(marker: str) -> None:
    hook, _engine = _interaction_marker_hook()
    request = SimpleNamespace(group_id="group_a", user_id="caller")
    result = SimpleNamespace(
        raw_text=f"{marker} 正文",
        clean_text=f"{marker} 正文",
        sticker_names=[],
        poke_user_ids=[],
    )

    hook(None, request, result, {})

    assert result.clean_text == "正文"
    assert result.raw_text == "正文"
    assert result.sticker_names == ["开心"]


def test_chat_reply_hook_parses_clean_text_when_raw_text_is_empty() -> None:
    hook, _engine = _interaction_marker_hook()
    result = SimpleNamespace(
        raw_text="",
        clean_text="[STICKER：开心] 正文",
        sticker_names=[],
        poke_user_ids=[],
    )

    hook(None, SimpleNamespace(group_id="group_a", user_id="caller"), result, {})

    assert result.clean_text == "正文"
    assert result.sticker_names == ["开心"]
