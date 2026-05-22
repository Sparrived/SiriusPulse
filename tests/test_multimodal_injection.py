"""Tests for multimodal message injection helper."""

from __future__ import annotations

import pytest

from sirius_pulse.core.emotional_engine import EmotionalGroupChatEngine


class TestInjectMultimodalIntoUserMessage:
    """Test _inject_multimodal_into_user_message static helper."""

    def test_no_multimodal_inputs_returns_unchanged(self):
        messages = [{"role": "user", "content": "hello"}]
        result = EmotionalGroupChatEngine._inject_multimodal_into_user_message(
            messages, None
        )
        assert result == [{"role": "user", "content": "hello"}]

    def test_empty_messages_returns_unchanged(self):
        result = EmotionalGroupChatEngine._inject_multimodal_into_user_message(
            [], [{"type": "image", "value": "http://a.jpg"}]
        )
        assert result == []

    def test_injects_single_image(self):
        messages = [{"role": "user", "content": "look at this"}]
        result = EmotionalGroupChatEngine._inject_multimodal_into_user_message(
            messages, [{"type": "image", "value": "http://a.jpg"}]
        )
        assert result[0]["role"] == "user"
        content = result[0]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "look at this"}
        assert content[1] == {"type": "image_url", "image_url": {"url": "http://a.jpg"}}

    def test_injects_multiple_images(self):
        messages = [{"role": "user", "content": "pics"}]
        result = EmotionalGroupChatEngine._inject_multimodal_into_user_message(
            messages,
            [
                {"type": "image", "value": "http://a.jpg"},
                {"type": "image", "value": "http://b.jpg"},
            ],
        )
        content = result[0]["content"]
        assert len(content) == 3
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image_url"
        assert content[2]["type"] == "image_url"

    def test_skips_non_image_types(self):
        messages = [{"role": "user", "content": "hello"}]
        result = EmotionalGroupChatEngine._inject_multimodal_into_user_message(
            messages,
            [
                {"type": "audio", "value": "http://a.mp3"},
                {"type": "image", "value": "http://b.jpg"},
            ],
        )
        content = result[0]["content"]
        assert len(content) == 2
        assert content[1]["type"] == "image_url"

    def test_targets_last_user_message(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ]
        result = EmotionalGroupChatEngine._inject_multimodal_into_user_message(
            messages, [{"type": "image", "value": "http://x.jpg"}]
        )
        assert result[1]["content"] == "first"
        assert isinstance(result[3]["content"], list)
        assert result[3]["content"][1] == {
            "type": "image_url",
            "image_url": {"url": "http://x.jpg"},
        }
