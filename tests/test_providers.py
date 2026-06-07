from __future__ import annotations

import base64

import pytest

from sirius_pulse.providers.base import (
    GenerationRequest,
    GenerationResult,
    ToolCall,
    build_chat_completion_payload,
    build_generation_debug_context,
    get_last_generation_usage,
    prepare_openai_compatible_messages,
    resolve_generation_timeout_seconds,
    set_last_generation_usage,
)
from sirius_pulse.providers.response_utils import extract_assistant_text


def test_generation_result_when_tool_calls_are_present_then_reports_tool_call_state():
    result = GenerationResult(
        content="",
        tool_calls=[ToolCall(id="call-1", function_name="lookup", function_arguments='{"q": "x"}')],
    )

    assert result.has_tool_calls is True
    assert result.tool_calls[0].function_name == "lookup"
    assert result.finish_reason == "stop"


def test_generation_usage_when_read_then_clears_thread_local_value():
    set_last_generation_usage({"prompt_tokens": 3})

    assert get_last_generation_usage() == {"prompt_tokens": 3}
    assert get_last_generation_usage() is None


def test_chat_payload_when_provider_disables_thinking_then_includes_provider_defaults():
    request = GenerationRequest(
        model="deepseek-chat",
        system_prompt="system",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=50,
        temperature=0.2,
        tools=[{"type": "function", "function": {"name": "lookup"}}],
        tool_choice="auto",
        response_format={"type": "json_object"},
    )

    payload = build_chat_completion_payload(
        request,
        provider_name="deepseek",
    )

    assert payload["messages"][0] == {"role": "system", "content": "system"}
    assert payload["messages"][1] == {"role": "user", "content": "hello"}
    assert payload["tools"] == request.tools
    assert payload["tool_choice"] == "auto"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}


def test_chat_payload_when_bailian_provider_then_uses_enable_thinking_flag():
    payload = build_chat_completion_payload(
        GenerationRequest(
            model="qwen-plus", system_prompt="", messages=[{"role": "user", "content": "hello"}]
        ),
        provider_name="aliyun-bailian",
    )

    assert payload["enable_thinking"] is False


def test_generation_debug_context_when_multimodal_messages_exist_then_counts_parts():
    request = GenerationRequest(
        model="test-model",
        system_prompt="system",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "one"},
                    {"type": "image_url", "image_url": {"url": "x"}},
                ],
            },
            {"role": "assistant", "content": "two"},
        ],
        tools=[{"type": "function"}],
    )

    context = build_generation_debug_context(request, provider_name="test")

    assert context["input_message_count"] == 2
    assert context["multimodal_part_count"] == 2
    assert context["total_message_count"] == 3


def test_prepare_messages_when_local_image_path_is_used_then_converts_to_data_url(tmp_path):
    image_path = tmp_path / "image.jpg"
    image_path.write_bytes(b"image-bytes")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "see"},
                {"type": "image_url", "image_url": {"url": str(image_path)}},
            ],
        }
    ]

    prepared, stats = prepare_openai_compatible_messages(messages)

    assert stats["local_image_path_conversions"] == 1
    data_url = prepared[0]["content"][1]["image_url"]["url"]
    assert data_url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == b"image-bytes"


def test_prepare_messages_when_invalid_local_image_path_is_used_then_drops_that_part():
    prepared, stats = prepare_openai_compatible_messages(
        [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "missing.png"}}],
            },
        ]
    )

    assert stats["local_image_path_conversions"] == 0
    assert prepared == [{"role": "user", "content": []}]


def test_timeout_when_request_overrides_default_then_uses_request_value():
    request = GenerationRequest(
        model="test-model", system_prompt="", messages=[], timeout_seconds=12
    )

    assert resolve_generation_timeout_seconds(request, 30) == 12


def test_timeout_when_value_is_invalid_then_raises():
    with pytest.raises(ValueError, match="timeout"):
        resolve_generation_timeout_seconds(
            GenerationRequest(model="test-model", system_prompt="", messages=[], timeout_seconds=0),
            30,
        )


def test_extract_assistant_text_when_provider_uses_nested_content_then_returns_first_text():
    assert (
        extract_assistant_text({"content": [{"text": "first"}, {"text": "second"}]})
        == "first\nsecond"
    )
    assert extract_assistant_text({"reasoning_content": {"text": "thought"}}) == "thought"
    assert extract_assistant_text({"refusal": "blocked"}) == "blocked"
