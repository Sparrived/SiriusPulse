from __future__ import annotations

import pytest

from sirius_pulse.core.cognition import CognitionAnalyzer
from sirius_pulse.providers.mock import MockProvider


def test_cognition_analyzer_when_created_then_exposes_image_caption_cache():
    analyzer = CognitionAnalyzer()

    assert analyzer._image_caption_cache == {}


def test_image_cache_key_when_file_content_matches_then_key_is_stable(tmp_path):
    first = tmp_path / "first.gif"
    second = tmp_path / "second.gif"
    first.write_bytes(b"same-image")
    second.write_bytes(b"same-image")
    analyzer = CognitionAnalyzer()

    assert analyzer._image_cache_key(str(first)) == analyzer._image_cache_key(str(second))


def test_image_cache_key_when_path_is_url_then_key_is_stable():
    analyzer = CognitionAnalyzer()

    assert analyzer._image_cache_key("https://example.test/a.png") == analyzer._image_cache_key(
        "https://example.test/a.png"
    )


@pytest.mark.asyncio
async def test_describe_image_when_images_exist_then_calls_multimodal_provider(tmp_path):
    image_path = tmp_path / "sticker.png"
    image_path.write_bytes(b"image-bytes")
    provider = MockProvider(["一张猫咪表情，显得有点困惑。"])
    analyzer = CognitionAnalyzer(provider_async=provider, model_name="vision-model")

    caption = await analyzer.describe_image(
        [{"type": "image", "value": str(image_path), "sub_type": "1"}],
        is_sticker=True,
    )

    assert caption == "一张猫咪表情，显得有点困惑。"
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.model == "vision-model"
    assert request.purpose == "image_caption"
    content = request.messages[0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1] == {"type": "image_url", "image_url": {"url": str(image_path)}}
    assert analyzer._image_caption_cache[analyzer._image_cache_key(str(image_path))] == caption


@pytest.mark.asyncio
async def test_describe_image_when_caption_is_cached_then_skips_provider(tmp_path):
    image_path = tmp_path / "sticker.png"
    image_path.write_bytes(b"image-bytes")
    provider = MockProvider(["unused"])
    analyzer = CognitionAnalyzer(provider_async=provider)
    analyzer._image_caption_cache[analyzer._image_cache_key(str(image_path))] = "缓存描述"

    caption = await analyzer.describe_image([{"type": "image", "value": str(image_path)}])

    assert caption == "缓存描述"
    assert provider.requests == []
