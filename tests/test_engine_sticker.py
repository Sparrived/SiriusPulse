"""Tests for sticker missend behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sirius_pulse.core.engine_sticker import EngineSticker


class DummyPersona:
    """测试用人格对象。"""

    name = "测试人格"


class DummyBrain:
    """测试用 LLM 调用记录器。"""

    def __init__(self, response: str = '{"pairs": [{"base": "喜欢", "opposites": ["讨厌"]}]}') -> None:
        self.sticker_names: list[str] = []
        self.response = response
        self.requests: list[Any] = []

    async def raw_call(self, request: Any) -> str:
        self.requests.append(request)
        return self.response


class DummyEngine:
    """测试用最小引擎对象。"""

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


class DummyAdapter:
    """记录消息发送与撤回调用的适配器。"""

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


@pytest.mark.asyncio
async def test_sticker_missend_recalls_wrong_and_resends_correct(tmp_path, monkeypatch):
    stickers_dir = tmp_path / "stickers"
    stickers_dir.mkdir()
    (stickers_dir / "喜欢.png").write_bytes(b"ok")
    (stickers_dir / "讨厌.png").write_bytes(b"bad")

    adapter = DummyAdapter()
    engine = DummyEngine(tmp_path, adapter)
    sticker = EngineSticker(engine)
    sticker._init_sticker_system()
    engine._sticker_oppositions = {"喜欢": ["讨厌"]}

    monkeypatch.setattr("sirius_pulse.core.engine_sticker.random.random", lambda: 0.01)
    monkeypatch.setattr("sirius_pulse.core.engine_sticker.random.choice", lambda items: items[0])

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("sirius_pulse.core.engine_sticker.asyncio.sleep", no_sleep)

    result = await sticker._send_stickers_by_names("123", ["喜欢"])

    assert result["success"] is True
    assert result["missent"] is True
    assert result["sticker_name"] == "喜欢"
    assert result["wrong_sticker_name"] == "讨厌"
    assert len(engine.brain.requests) == 0
    assert adapter.deleted == ["101"]
    assert len(adapter.sent) == 2
    assert adapter.sent[0][2][0]["data"]["file"].endswith("讨厌.png")
    assert adapter.sent[1][2][0]["type"] == "text"
    assert adapter.sent[1][2][1]["data"]["file"].endswith("喜欢.png")


@pytest.mark.asyncio
async def test_sticker_warmup_builds_and_reuses_cached_oppositions(tmp_path, monkeypatch):
    stickers_dir = tmp_path / "stickers"
    stickers_dir.mkdir()
    (stickers_dir / "喜欢.png").write_bytes(b"ok")
    (stickers_dir / "讨厌.png").write_bytes(b"bad")

    adapter = DummyAdapter()
    engine = DummyEngine(tmp_path, adapter)
    sticker = EngineSticker(engine)
    sticker._init_sticker_system()

    monkeypatch.setattr("sirius_pulse.core.engine_sticker.random.choice", lambda items: items[-1])

    await sticker.warmup_opposition_cache()

    assert len(engine.brain.requests) == 1
    assert engine._sticker_oppositions == {"喜欢": ["讨厌"]}

    engine.brain.requests.clear()
    await sticker.warmup_opposition_cache()

    assert len(engine.brain.requests) == 0
    assert engine._sticker_oppositions == {"喜欢": ["讨厌"]}


@pytest.mark.asyncio
async def test_sticker_warmup_regenerates_when_names_changed(tmp_path):
    stickers_dir = tmp_path / "stickers"
    stickers_dir.mkdir()
    (stickers_dir / "喜欢.png").write_bytes(b"ok")
    (stickers_dir / "讨厌.png").write_bytes(b"bad")

    adapter = DummyAdapter()
    engine = DummyEngine(tmp_path, adapter)
    sticker = EngineSticker(engine)
    sticker._init_sticker_system()

    await sticker.warmup_opposition_cache()
    assert len(engine.brain.requests) == 1

    (stickers_dir / "开心.png").write_bytes(b"happy")
    sticker._init_sticker_system()
    engine.brain.requests.clear()

    await sticker.warmup_opposition_cache()

    assert len(engine.brain.requests) == 1


@pytest.mark.asyncio
async def test_sticker_missend_uses_cached_opposition_without_llm_call(tmp_path, monkeypatch):
    stickers_dir = tmp_path / "stickers"
    stickers_dir.mkdir()
    (stickers_dir / "喜欢.png").write_bytes(b"ok")
    (stickers_dir / "讨厌.png").write_bytes(b"bad")

    adapter = DummyAdapter()
    engine = DummyEngine(tmp_path, adapter)
    sticker = EngineSticker(engine)
    sticker._init_sticker_system()
    engine._sticker_oppositions = {"喜欢": ["讨厌"]}

    monkeypatch.setattr("sirius_pulse.core.engine_sticker.random.random", lambda: 0.01)
    monkeypatch.setattr("sirius_pulse.core.engine_sticker.random.choice", lambda items: items[0])

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("sirius_pulse.core.engine_sticker.asyncio.sleep", no_sleep)

    result = await sticker._send_stickers_by_names("123", ["喜欢"])

    assert result["success"] is True
    assert result["missent"] is True
    assert len(engine.brain.requests) == 0
    assert adapter.deleted == ["101"]
    assert len(adapter.sent) == 2
