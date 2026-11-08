"""让她不再套模板：雷同时被提醒换个说法、看得见自己的表情和戳一戳、能借用群里刚出现的表情包。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from sirius_pulse.core.borrowed_images import recent_reusable_images, remember
from sirius_pulse.core.emotional_engine import EmotionalGroupChatEngine
from sirius_pulse.core.engine_sticker import EngineSticker
from sirius_pulse.core.style_drift import template_hint
from sirius_pulse.memory.basic import BasicMemoryManager
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision
from sirius_pulse.providers.mock import MockProvider

GROUP = "group-a"

# --- 套模板时被提醒 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("replies", "expected"),
    [
        ([f"第{i}句喵～\n接一句啦～喵" for i in range(8)], ["2行", "「喵」"]),
        (["好呀", "这个我不同意，理由有三条：一来……", "[STICKER:开心]", "哈？", "你先说清楚", "行吧行吧", "噗——", "真的假的？"], []),
        (["嗯喵～\n好喵～"] * 4, []),
    ],
    ids=["two-line-meow-template", "varied", "too-few-to-judge"],
)
def test_template_hint_names_the_repeated_shape_only_when_it_is_real(replies, expected):
    hint = template_hint(replies)

    if not expected:
        assert hint == ""
    else:
        assert hint.startswith("【换个说法】")
        assert all(part in hint for part in expected)


def test_markers_do_not_count_as_her_wording():
    replies = [f"[STICKER:开心] 第{i}个不同的说法" for i in range(8)]

    assert "开头" not in template_hint(replies)


# --- 借用群里刚出现的表情包 -------------------------------------------------------


def _image(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG fake")
    return path


def _group_with_images(tmp_path: Path) -> BasicMemoryManager:
    basic = BasicMemoryManager()
    basic.add_entry(
        GROUP,
        "u1",
        "human",
        "[动画表情：绿发女孩捂耳朵，适合表达嫌吵、别说了。]",
        multimodal_inputs=[{"type": "image", "value": str(_image(tmp_path, "a.jpg"))}],
        speaker_name="小明",
    )
    basic.add_entry(
        GROUP,
        "u2",
        "human",
        "[图片] [图片描述：一张在海边拍的自拍照。]",
        multimodal_inputs=[{"type": "image", "value": str(_image(tmp_path, "b.jpg"))}],
        speaker_name="小红",
    )
    basic.add_entry(
        GROUP,
        "u3",
        "human",
        "[图片] [图片描述：熊猫头表情包，配字“哎呦”。]",
        multimodal_inputs=[{"type": "image", "value": str(_image(tmp_path, "c.jpg"))}],
        speaker_name="小刚",
    )
    return basic


def test_only_stickers_and_meme_images_can_be_borrowed_newest_first(tmp_path):
    images = recent_reusable_images(_group_with_images(tmp_path).get_all(GROUP))

    assert [(i.token, i.path.name, i.speaker) for i in images] == [
        ("刚才1", "c.jpg", "小刚"),
        ("刚才2", "a.jpg", "小明"),
    ], "自拍不是梗图，不会被拿来转发"


class _Adapter:
    def __init__(self):
        self.sent = []

    async def send_group_msg(self, group_id, msg):
        self.sent.append((group_id, msg))
        return {"status": "ok"}


def test_borrowed_sticker_sends_the_original_image_back(tmp_path, monkeypatch):
    monkeypatch.setattr("sirius_pulse.core.engine_sticker.STICKER_MISSEND_PROBABILITY", 1.0)
    basic = _group_with_images(tmp_path)
    engine = SimpleNamespace(work_path=tmp_path, _sticker_names=[], _adapter=None)
    remember(engine, GROUP, recent_reusable_images(basic.get_all(GROUP)))
    adapter = _Adapter()

    result = asyncio.run(
        EngineSticker(engine)._send_stickers_by_names(GROUP, ["刚才2"], adapter=adapter)
    )

    assert result["success"] is True
    image = adapter.sent[0][1][0]["data"]["file"]
    assert image.startswith("base64://"), "原图被原样发出，借来的图不会「手滑发错」"
    assert len(adapter.sent) == 1


# --- 她看得见自己的表情和戳一戳 ---------------------------------------------------


class _Model(MockProvider):
    def __init__(self, reply):
        super().__init__([reply])


@pytest_asyncio.fixture
async def engine(tmp_path):
    engine = EmotionalGroupChatEngine(
        work_path=tmp_path,
        provider_async=_Model("哼，才不理你 [STICKER:开心]"),
        persona=PersonaProfile(name="月白", full_system_prompt="你是月白。"),
        config={
            "pending_message_threshold": 0,
            "partial_reply_lead_seconds": 0,
            "enable_tools": False,
            "task_enabled": {"memory_extract": False, "event_extract": False},
        },
    )
    engine._sticker_names = ["开心"]
    engine.brain.sticker_names = ["开心"]
    try:
        yield engine
    finally:
        engine.stop_background_tasks()
        await engine.event_bus.close()
        engine.cognition_store.close()
        engine.user_manager.close()
        engine._memory_storage.close()


async def _turn(engine, text):
    item = engine.delayed_queue.enqueue(
        GROUP,
        "12345",
        text,
        StrategyDecision(strategy=ResponseStrategy.IMMEDIATE, urgency=50.0, reason="direct"),
        adapter_type="napcat",
        channel_user_id="12345",
    )
    item.enqueue_time = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    return await engine.tick_delayed_queue(GROUP, adapter_type="napcat", item_id=item.item_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "recorded"),
    [
        ("哼，才不理你 [STICKER:开心]", "哼，才不理你\n[STICKER:开心]"),
        ("[POKE:12345]", "[POKE:12345]"),
    ],
    ids=["text-then-sticker", "poke-only"],
)
async def test_her_own_stickers_and_pokes_stay_in_her_history(engine, reply, recorded):
    engine.brain.provider_async = _Model(reply)

    results = await _turn(engine, "月白你在干嘛")

    assert results and (results[0]["reply"] or results[0]["poke_user_ids"])
    mine = [e.content for e in engine.basic_memory.get_all(GROUP) if e.role == "assistant"]
    assert mine == [recorded]
