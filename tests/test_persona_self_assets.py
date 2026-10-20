"""人格自身三视图 / 立绘路径的发现与注入。

用户视角：把设定图放进 `{persona}/assets/` 后，人格下一轮对话就能在
system prompt 里看到这些图片的本地路径，并用 read_image / image_generate 使用它们。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sirius_pulse.core.brain import Brain, ChatRequest
from sirius_pulse.providers.base import GenerationResult
from sirius_pulse.utils.persona_assets import (
    build_persona_assets_section,
    collect_persona_assets,
)


class _Provider:
    def __init__(self) -> None:
        self.last_request = None

    async def generate_async(self, request):
        self.last_request = request
        return GenerationResult(content="ok")


def _write(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-image")
    return path


def test_persona_assets_section_lists_three_views_in_reading_order(tmp_path: Path):
    assets = tmp_path / "assets"
    sheet = _write(assets / "yuebai_turnaround_sheet.jpg")
    front = _write(assets / "yuebai_front.png")
    side = _write(assets / "yuebai_side.png")
    back = _write(assets / "yuebai_back.png")
    bust = _write(assets / "yuebai_bust.png")
    _write(assets / "notes.txt")

    section = build_persona_assets_section(assets)

    assert "【你自己的形象参考图】" in section
    for label, path in (
        ("三视图合集", sheet),
        ("正视图", front),
        ("侧视图", side),
        ("后视图", back),
        ("半身/头像", bust),
    ):
        assert label in section
        assert str(path.resolve()) in section
    assert "notes.txt" not in section
    assert section.index(str(sheet.resolve())) < section.index(str(front.resolve()))
    assert section.index(str(front.resolve())) < section.index(str(side.resolve()))


def test_persona_assets_section_is_empty_without_assets_directory(tmp_path: Path):
    assert build_persona_assets_section(tmp_path / "missing-assets") == ""
    assert collect_persona_assets(None) == []


@pytest.mark.asyncio
async def test_brain_chat_injects_own_reference_image_paths_into_system_prompt(tmp_path: Path):
    assets = tmp_path / "persona" / "assets"
    front = _write(assets / "yuebai_front.png")
    provider = _Provider()
    brain = Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="model",
                max_tokens=100,
                temperature=0.1,
                timeout=30,
            )
        ),
        persona=SimpleNamespace(name="月白", build_system_prompt=lambda: "人格基底"),
        persona_assets_dir=assets,
    )

    await brain.chat(
        ChatRequest(
            group_id="group-1",
            user_id="u1",
            system_prompt="对话约束",
            messages=[{"role": "user", "content": "看看你自己的设定图"}],
        )
    )

    prompt = provider.last_request.system_prompt
    assert prompt.startswith("人格基底")
    assert str(front.resolve()) in prompt
    assert "正视图" in prompt
    assert "read_image" in prompt
