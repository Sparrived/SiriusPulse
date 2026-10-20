"""read_image 内置工具：人格能真正看到本地图片 / 图片 URL。

用户视角：人格拿到自己的三视图路径或聊天图片缓存路径后，调用 read_image
就能把图片读进视觉通道，而不是只看得到一行路径文本。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from sirius_pulse.tools.builtin import read_image
from sirius_pulse.tools.data_store import ToolDataStore

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)


def _store(persona_dir: Path) -> ToolDataStore:
    return ToolDataStore(persona_dir / "tool_data" / "read_image.json")


def _write_png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_PNG_1X1)
    return path


@pytest.mark.asyncio
async def test_read_image_returns_persona_asset_as_vision_input(tmp_path: Path):
    persona_dir = tmp_path / "sirius"
    front = _write_png(persona_dir / "assets" / "yuebai_front.png")

    result = await read_image.run(
        paths=[str(front)],
        focus="发色和服装细节",
        data_store=_store(persona_dir),
    )

    assert result["success"] is True
    blocks = result["multimodal_blocks"]
    assert [Path(block["value"]) for block in blocks] == [front.resolve()]
    assert blocks[0]["mime_type"] == "image/png"
    assert "已读取 1 张图片" in result["text_blocks"][0]["value"]
    assert any("发色和服装细节" in block["value"] for block in result["text_blocks"])


@pytest.mark.asyncio
async def test_read_image_expands_tilde_against_persona_home(tmp_path: Path):
    persona_dir = tmp_path / "sirius"
    cached = _write_png(persona_dir / "home" / "image_cache" / "chat_001.png")

    result = await read_image.run(
        paths=["~/image_cache/chat_001.png"],
        data_store=_store(persona_dir),
    )

    assert result["success"] is True
    assert Path(result["multimodal_blocks"][0]["value"]) == cached.resolve()


@pytest.mark.asyncio
async def test_read_image_reports_missing_non_image_and_too_many(tmp_path: Path):
    persona_dir = tmp_path / "sirius"
    store = _store(persona_dir)

    missing = await read_image.run(
        paths=[str(persona_dir / "assets" / "nope.png")], data_store=store
    )
    assert missing["success"] is False
    assert "不存在" in missing["error"]

    notes = persona_dir / "assets" / "notes.txt"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text("不是图片", encoding="utf-8")
    not_image = await read_image.run(paths=[str(notes)], data_store=store)
    assert not_image["success"] is False
    assert "不是可识别的图片" in not_image["error"]

    front = _write_png(persona_dir / "assets" / "yuebai_front.png")
    too_many = await read_image.run(paths=[str(front)] * 5, data_store=store)
    assert too_many["success"] is False
    assert "最多读 4 张" in too_many["error"]
