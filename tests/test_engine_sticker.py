"""表情包发送在真实聊天中的业务行为测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sirius_pulse.core.engine_sticker import EngineSticker


class DummyPersona:
    name = "测试人格"


class DummyBrain:
    def __init__(self, response: str = '{"pairs": [{"base": "喜欢", "opposites": ["讨厌"]}]}') -> None:
        self.sticker_names: list[str] = []
        self.response = response
        self.requests: list[Any] = []

    async def raw_call(self, request: Any) -> str:
        self.requests.append(request)
        return self.response


class DummyEngine:
    def __init__(
        self,
        work_path: Path,
        adapter: Any,
        llm_response: str = '{"pairs": [{"base": "喜欢", "opposites": ["讨厌"]}]}',
    ) -> None:
        self.work_path = work_path
        self._adapter = adapter
        self._sticker_names: list[str] = []
        self._sticker_oppositions: dict[str, list[str]] = {}
        self.brain = DummyBrain(llm_response)
        self.persona = DummyPersona()
        self._default_model = "test-model"


class RecordingAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list[dict[str, Any]]]] = []
        self.deleted: list[str] = []
        self._next_message_id = 100

    async def send_group_msg(self, group_id: str, message: list[dict[str, Any]]) -> dict[str, Any]:
        self.sent.append(("group", group_id, message))
        self._next_message_id += 1
        return {"status": "ok", "data": {"message_id": self._next_message_id}}

    async def send_private_msg(self, user_id: str, message: list[dict[str, Any]]) -> dict[str, Any]:
        self.sent.append(("private", user_id, message))
        self._next_message_id += 1
        return {"status": "ok", "data": {"message_id": self._next_message_id}}

    async def delete_message(self, message_id: str) -> dict[str, Any]:
        self.deleted.append(message_id)
        return {"status": "ok"}


def _prepare_stickers(tmp_path: Path) -> None:
    stickers_dir = tmp_path / "stickers"
    stickers_dir.mkdir()
    (stickers_dir / "喜欢.png").write_bytes(b"ok")
    (stickers_dir / "讨厌.png").write_bytes(b"bad")


def _force_first_random_choice(monkeypatch) -> None:
    monkeypatch.setattr("sirius_pulse.core.engine_sticker.random.choice", lambda items: items[0])


@pytest.mark.asyncio
async def test_sticker_when_normal_send_succeeds_then_group_receives_selected_image(
    tmp_path: Path,
    monkeypatch,
):
    _prepare_stickers(tmp_path)
    adapter = RecordingAdapter()
    engine = DummyEngine(tmp_path, adapter)
    sticker = EngineSticker(engine)
    sticker._init_sticker_system()
    _force_first_random_choice(monkeypatch)
    monkeypatch.setattr("sirius_pulse.core.engine_sticker.random.random", lambda: 0.99)

    result = await sticker._send_stickers_by_names("group_a", ["喜欢"])

    assert result["success"] is True
    assert adapter.deleted == []
    assert adapter.sent[0][0] == "group"
    assert adapter.sent[0][1] == "group_a"
    assert adapter.sent[0][2][0]["data"]["file"].endswith("喜欢.png")


@pytest.mark.asyncio
async def test_sticker_when_wrong_image_is_missent_then_it_is_recalled_and_correct_one_is_sent(
    tmp_path: Path,
    monkeypatch,
):
    _prepare_stickers(tmp_path)
    adapter = RecordingAdapter()
    engine = DummyEngine(tmp_path, adapter)
    sticker = EngineSticker(engine)
    sticker._init_sticker_system()
    engine._sticker_oppositions = {"喜欢": ["讨厌"]}
    _force_first_random_choice(monkeypatch)
    monkeypatch.setattr("sirius_pulse.core.engine_sticker.random.random", lambda: 0.01)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("sirius_pulse.core.engine_sticker.asyncio.sleep", no_sleep)

    result = await sticker._send_stickers_by_names("group_a", ["喜欢"])

    assert result["success"] is True
    assert result["missent"] is True
    assert result["sticker_name"] == "喜欢"
    assert result["wrong_sticker_name"] == "讨厌"
    assert adapter.deleted == ["101"]
    assert len(adapter.sent) == 2
    assert adapter.sent[0][2][0]["data"]["file"].endswith("讨厌.png")
    assert adapter.sent[1][2][0]["data"]["file"].endswith("喜欢.png")
    assert engine.brain.requests == []


@pytest.mark.asyncio
async def test_sticker_when_private_chat_sends_image_then_private_adapter_api_is_used(
    tmp_path: Path,
    monkeypatch,
):
    _prepare_stickers(tmp_path)
    adapter = RecordingAdapter()
    engine = DummyEngine(tmp_path, adapter)
    sticker = EngineSticker(engine)
    sticker._init_sticker_system()
    _force_first_random_choice(monkeypatch)
    monkeypatch.setattr("sirius_pulse.core.engine_sticker.random.random", lambda: 0.99)

    result = await sticker._send_stickers_by_names("private_10001", ["喜欢"])

    assert result["success"] is True
    assert adapter.sent[0][0] == "private"
    assert adapter.sent[0][1] == "10001"


@pytest.mark.asyncio
async def test_sticker_when_cache_is_warmed_then_later_startup_reuses_saved_oppositions(
    tmp_path: Path,
    monkeypatch,
):
    _prepare_stickers(tmp_path)
    _force_first_random_choice(monkeypatch)
    first_engine = DummyEngine(tmp_path, RecordingAdapter())
    first_sticker = EngineSticker(first_engine)
    first_sticker._init_sticker_system()

    await first_sticker.warmup_opposition_cache()

    second_engine = DummyEngine(tmp_path, RecordingAdapter())
    second_sticker = EngineSticker(second_engine)
    second_sticker._init_sticker_system()
    await second_sticker.warmup_opposition_cache()

    assert len(first_engine.brain.requests) == 1
    assert len(second_engine.brain.requests) == 0
    assert second_engine._sticker_oppositions == {"喜欢": ["讨厌"]}


@pytest.mark.asyncio
async def test_sticker_when_sticker_names_change_then_opposition_cache_is_regenerated(
    tmp_path: Path,
):
    _prepare_stickers(tmp_path)
    engine = DummyEngine(tmp_path, RecordingAdapter())
    sticker = EngineSticker(engine)
    sticker._init_sticker_system()
    await sticker.warmup_opposition_cache()
    assert len(engine.brain.requests) == 1

    (tmp_path / "stickers" / "开心.png").write_bytes(b"happy")
    sticker._init_sticker_system()
    engine.brain.requests.clear()

    await sticker.warmup_opposition_cache()

    assert len(engine.brain.requests) == 1


@pytest.mark.asyncio
async def test_sticker_when_requested_name_has_no_file_then_send_fails_without_adapter_call(
    tmp_path: Path,
):
    _prepare_stickers(tmp_path)
    adapter = RecordingAdapter()
    engine = DummyEngine(tmp_path, adapter)
    sticker = EngineSticker(engine)
    sticker._init_sticker_system()

    result = await sticker._send_stickers_by_names("group_a", ["不存在"])

    assert result["success"] is False
    assert adapter.sent == []
