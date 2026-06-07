from __future__ import annotations

from sirius_pulse.core.pinned_message import PinnedMessage
from sirius_pulse.core.prompt_factory import (
    TAG_PINNED_MESSAGES,
    PromptBundle,
    PromptFactory,
    StyleAdapter,
)
from sirius_pulse.memory.basic import BasicMemoryManager
from sirius_pulse.memory.context_assembler import ContextAssembler
from sirius_pulse.token.utils import PromptTokenBreakdown


def test_prompt_bundle_when_breakdown_is_missing_then_creates_default_breakdown():
    bundle = PromptBundle(system_prompt="system", user_content="user")

    assert bundle.system_prompt == "system"
    assert bundle.user_content == "user"
    assert isinstance(bundle.token_breakdown, PromptTokenBreakdown)
    assert bundle.token_breakdown.total == 0


def test_prompt_factory_when_message_is_tagged_then_escapes_content_and_attributes():
    tagged = PromptFactory.tag_message(
        'hello <world> & "friends"',
        speaker='Alice "A"',
        user_id="u&1",
        platform_message_id='msg"1',
        time_str="12:34:56",
        group_id='group"1',
    )

    assert tagged.startswith('<message speaker="Alice &quot;A&quot;"')
    assert 'user_id="u&amp;1"' in tagged
    assert 'time="12:34:56"' in tagged
    assert 'group="group&quot;1"' in tagged
    assert 'msg_id="msg&quot;1"' in tagged
    assert 'hello &lt;world&gt; &amp; "friends"' in tagged


def test_prompt_factory_when_extracting_last_message_then_reads_last_tag():
    content = "\n".join(
        [
            PromptFactory.tag_message("first", speaker="Alice", time_str="00:00:01"),
            PromptFactory.tag_message("second", speaker="Bob", time_str="00:00:02"),
        ]
    )

    assert PromptFactory._extract_last_message_text(content) == "second"
    assert PromptFactory._extract_last_message_speaker(content) == "Bob"
    assert PromptFactory._extract_last_message_text("plain text") == "plain text"


def test_prompt_factory_when_rendering_multimodal_descriptions_then_appends_only_values():
    rendered = PromptFactory.append_multimodal_descriptions(
        "base",
        [
            {"type": "image", "value": "a.png"},
            {"type": "image", "value": ""},
            {"type": "audio", "value": "clip.wav"},
        ],
    )

    assert rendered.startswith("base\n")
    assert "image" in rendered
    assert "a.png" in rendered
    assert "clip.wav" in rendered
    assert PromptFactory.append_multimodal_descriptions("base", []) == "base"


def test_style_adapter_when_persona_preferences_exist_then_applies_overrides():
    class Persona:
        max_tokens_preference = 64
        temperature_preference = 0.2
        communication_style = "formal"
        humor_style = ""
        emoji_preference = "none"

    params = StyleAdapter().adapt(pace="accelerating", persona=Persona())

    assert params.max_tokens == 64
    assert params.temperature == 0.2
    assert params.length_instruction
    assert params.tone_instruction


class _NoopDiaryRetriever:
    def retrieve(self, **kwargs):
        return []


def test_context_assembler_when_pinned_messages_are_provided_then_injects_current_prompt():
    assembler = ContextAssembler(
        BasicMemoryManager(),
        _NoopDiaryRetriever(),
    )
    pinned = PinnedMessage(
        message_id="pin_1",
        content="Remember the deployment window.",
        speaker="Alice",
        group_id="group_a",
        reason="ops",
    )

    messages = assembler.build_messages(
        group_id="group_a",
        current_query="What should I do next?",
        system_prompt="system",
        pinned_messages=[pinned],
    )

    assert TAG_PINNED_MESSAGES in messages[-1]["content"]
    assert "<pinned_message" in messages[-1]["content"]
    assert "Remember the deployment window." in messages[-1]["content"]


def test_output_spec_does_not_tell_model_to_list_pinned_messages():
    spec = PromptFactory.build_output_spec()

    assert "list_pinned_messages" not in spec
