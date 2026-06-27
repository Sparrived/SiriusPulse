from __future__ import annotations

from sirius_pulse.core.cognition import CognitionAnalyzer


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
