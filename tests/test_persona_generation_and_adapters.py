from __future__ import annotations

import pytest

from sirius_pulse.adapters.models import (
    AtSegment,
    FileSegment,
    ImageSegment,
    MessageGroup,
    ParsedEvent,
    ReplySegment,
    TextSegment,
    VoiceSegment,
    at,
    file as file_segment,
    image,
    reply,
    text,
    voice,
)
from sirius_pulse.persona_generation.builders import (
    _decode_json_string_fragment,
    _extract_json_number_field,
    _extract_json_payload,
    _extract_json_string_field,
    _extract_partial_roleplay_payload,
    _format_dependency_snapshots_for_prompt,
    _load_dependency_file_snapshots,
    _looks_like_roleplay_json_response,
    _prepare_persona_generation_input,
    _strip_wrapped_json_code_fence,
)
from sirius_pulse.persona_generation.templates import (
    PersonaSpec,
    RolePlayAnswer,
    _dict_to_persona_spec,
    _format_answers,
    _normalize_agent_key,
    _normalize_dependency_file_path,
    _normalize_roleplay_question_template,
    _persona_spec_to_dict,
    _resolve_dependency_file_path,
    generate_humanized_roleplay_questions,
    list_roleplay_question_templates,
)
from sirius_pulse.platforms.persona_utils import extract_json


def test_adapter_models_when_message_groups_are_combined_then_segments_keep_order():
    group = MessageGroup.from_str("hello") + MessageGroup(
        [
            at("user-1"),
            image("image.png", url="https://example.test/image.png", sub_type="emoji"),
            voice("voice.amr"),
            file_segment("notes.txt", name="notes"),
            reply("message-1"),
        ]
    )

    assert len(group) == 6
    assert list(group) == [
        TextSegment("hello"),
        AtSegment("user-1"),
        ImageSegment("image.png", url="https://example.test/image.png", sub_type="emoji"),
        VoiceSegment("voice.amr"),
        FileSegment("notes.txt", name="notes"),
        ReplySegment("message-1"),
    ]
    assert group[0] == text("hello")


def test_adapter_models_when_constructed_from_string_then_text_segment_is_created():
    group = MessageGroup("solo")
    event = ParsedEvent(
        group_id="group-1",
        user_id="user-1",
        self_id="bot-1",
        message_type="group",
        prompt="hello",
        nickname="Ada",
        card="Ada Card",
        message_id="msg-1",
        multimodal_inputs=[{"type": "image", "path": "image.png"}],
    )

    assert group.segments == [TextSegment("solo")]
    assert event.group_id == "group-1"
    assert event.multimodal_inputs == [{"type": "image", "path": "image.png"}]


def test_persona_templates_when_template_names_are_normalized_then_aliases_are_supported():
    assert list_roleplay_question_templates() == ["default", "companion", "romance", "group_chat"]
    assert _normalize_roleplay_question_template("standard") == "default"
    assert _normalize_roleplay_question_template("group-chat") == "group_chat"
    assert _normalize_roleplay_question_template("groupchat") == "group_chat"

    questions = generate_humanized_roleplay_questions("group chat")

    assert len(questions) >= 8
    assert all(item.question for item in questions)
    assert {item.perspective for item in questions} <= {"subjective", "objective"}
    with pytest.raises(ValueError):
        generate_humanized_roleplay_questions("missing-template")


def test_persona_templates_when_formatting_and_serializing_specs_then_values_round_trip():
    answer = RolePlayAnswer(question="What matters?", answer="Trust.", perspective="", details="core value")
    spec = PersonaSpec(
        agent_name="Agent",
        agent_alias="Al",
        trait_keywords=["warm"],
        answers=[answer],
        background="Backstory",
        dependency_files=[r".\docs//profile.txt"],
        output_language="en-US",
    )

    formatted = _format_answers(spec.answers)
    merged = spec.merge(agent_alias="Alias", background=None)
    restored = _dict_to_persona_spec(_persona_spec_to_dict(merged))

    assert "1. [subjective] Q: What matters?" in formatted
    assert "details: core value" in formatted
    assert "A: Trust." in formatted
    assert merged is not spec
    assert merged.agent_alias == "Alias"
    assert merged.background == "Backstory"
    assert restored.agent_name == "Agent"
    assert restored.agent_alias == "Alias"
    assert restored.answers == [answer]
    assert restored.dependency_files == ["docs/profile.txt"]
    assert restored.output_language == "en-US"


def test_persona_templates_when_normalizing_keys_and_paths_then_values_are_stable(tmp_path):
    absolute_file = tmp_path / "absolute.txt"

    assert _normalize_agent_key(" A / B ") == "A_B"
    assert _normalize_agent_key(" / ") == "generated_agent"
    assert _normalize_dependency_file_path(r".\docs//profile.txt") == "docs/profile.txt"
    assert _resolve_dependency_file_path(tmp_path, "docs/profile.txt") == tmp_path / "docs/profile.txt"
    assert _resolve_dependency_file_path(tmp_path, str(absolute_file)) == absolute_file


def test_persona_generation_when_json_is_wrapped_or_partial_then_payload_can_be_recovered():
    wrapped = '```json\n{"agent_persona":"p","global_system_prompt":"prompt"}\n```'
    partial = (
        '{"persona":"p","prompt":"long prompt",'
        '"recommended_temperature":0.4,"recommended_max_tokens":900}'
    )

    assert _strip_wrapped_json_code_fence(wrapped) == '{"agent_persona":"p","global_system_prompt":"prompt"}'
    assert _looks_like_roleplay_json_response(wrapped) is True
    assert _extract_json_payload(wrapped) == {"agent_persona": "p", "global_system_prompt": "prompt"}
    assert _decode_json_string_fragment('line\\nquote\\"') == 'line\nquote"'
    assert _extract_json_string_field('{"prompt":"hello', ("prompt",)) == ("hello", False)
    assert _extract_json_number_field('{"temperature":0.25}', ("temperature",)) == 0.25
    assert _extract_partial_roleplay_payload(partial) == (
        {
            "agent_persona": "p",
            "global_system_prompt": "long prompt",
            "temperature": 0.4,
            "max_tokens": 900,
        },
        [],
        [],
    )
    assert _extract_partial_roleplay_payload("plain text") is None
    assert extract_json('```json\n{"x": 1}\n```') == '{"x": 1}'


def test_persona_generation_when_loading_dependency_snapshots_then_missing_and_dirs_are_reported(tmp_path):
    notes = tmp_path / "notes.txt"
    folder = tmp_path / "folder"
    notes.write_text("hello world", encoding="utf-8")
    folder.mkdir()

    snapshots = _load_dependency_file_snapshots(
        dependency_root=tmp_path,
        dependency_files=["notes.txt", "./notes.txt", "missing.txt", "folder"],
    )
    prompt = _format_dependency_snapshots_for_prompt(snapshots, max_chars_per_file=5)

    assert [item.path for item in snapshots] == ["notes.txt", "missing.txt", "folder"]
    assert snapshots[0].exists is True
    assert snapshots[0].content == "hello world"
    assert len(snapshots[0].sha256) == 64
    assert snapshots[1].error == "file_not_found"
    assert snapshots[2].error == "is_directory"
    assert "notes.txt" in prompt
    assert "hello" in prompt
    assert "missing.txt" in prompt
    assert "folder" in prompt


def test_persona_generation_when_preparing_input_then_spec_is_validated_and_dependencies_are_included(tmp_path):
    with pytest.raises(ValueError):
        _prepare_persona_generation_input(
            PersonaSpec(),
            dependency_root=None,
            base_temperature=0.7,
            base_max_tokens=512,
        )
    with pytest.raises(ValueError):
        _prepare_persona_generation_input(
            PersonaSpec(dependency_files=["facts.txt"]),
            dependency_root=None,
            base_temperature=0.7,
            base_max_tokens=512,
        )

    facts = tmp_path / "facts.txt"
    facts.write_text("profile facts", encoding="utf-8")
    prepared = _prepare_persona_generation_input(
        PersonaSpec(
            agent_name="Agent",
            trait_keywords=["warm"],
            answers=[RolePlayAnswer(question="What matters?", answer="Trust.")],
            dependency_files=["./facts.txt"],
            output_language="en-US",
        ),
        dependency_root=tmp_path,
        base_temperature=0.65,
        base_max_tokens=768,
    )

    assert prepared.normalized_spec.dependency_files == ["facts.txt"]
    assert len(prepared.dependency_snapshots) == 1
    assert prepared.dependency_snapshots[0].exists is True
    assert "name=Agent" in prepared.user_prompt
    assert "keywords=warm" in prepared.user_prompt
    assert "Q: What matters?" in prepared.user_prompt
    assert "profile facts" in prepared.user_prompt
    assert prepared.system_prompt
