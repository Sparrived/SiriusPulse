from __future__ import annotations

from types import SimpleNamespace

from sirius_pulse.core.prompt_factory import (
    TAG_HISTORY_DIARY,
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
        emoji_preference = "none"

    params = StyleAdapter().adapt(pace="accelerating", persona=Persona())

    assert params.max_tokens == 64
    assert params.temperature == 0.2
    assert params.length_instruction == ""
    assert params.tone_instruction


def test_output_spec_no_newline_split_instruction():
    """换行分割提示已移除，改为 continue/stop 工具控制流程。"""
    spec = PromptFactory.build_output_spec()

    assert "多句话可以用换行符分割" not in spec
    assert "每句话不可超过 15 字" not in spec
    assert "禁止任何形式的换行符" not in spec


def test_output_spec_when_function_call_enabled_then_includes_continue_stop():
    """启用 function call 时，输出规范包含 continue/stop 工具使用说明。"""
    spec = PromptFactory.build_output_spec(supports_function_call=True)

    assert "continue" in spec
    assert "stop" in spec
    assert "每次回复结束时必须调用工具" in spec


def test_output_spec_when_function_call_disabled_then_no_continue_stop():
    """未启用 function call 时，不包含 continue/stop 说明。"""
    spec = PromptFactory.build_output_spec(supports_function_call=False)

    assert "continue" not in spec
    assert "stop" not in spec


def test_persona_prompt_drops_length_biased_speech_fields():
    prompt = PromptFactory.build_persona_prompt(
        name="Bot",
        communication_style="简短随意",
        speech_rhythm="短句慢慢说",
    )

    assert "简短" not in prompt
    assert "短句" not in prompt
    assert "说话随意" not in prompt


def test_assemble_chat_does_not_inject_group_style_length_learning():
    group_profile = SimpleNamespace(
        atmosphere_history=[],
        group_norms={
            "avg_message_length": 8,
            "length_distribution": {"short": 10},
            "message_count": 10,
        },
    )
    style_params = StyleAdapter().adapt(pace="silent", persona=None)

    bundle = PromptFactory.assemble_chat(
        message_content="hello",
        group_profile=group_profile,
        style_params=style_params,
        other_ai_names=[],
    )

    assert "【群体风格】" not in bundle.system_prompt
    assert "【回复风格】" not in bundle.system_prompt
    assert "平均8字" not in bundle.system_prompt
    assert "尽量简短" not in bundle.system_prompt
    assert "控制在 30 字" not in bundle.system_prompt


def test_assemble_chat_when_atmosphere_history_exists_then_does_not_inject_trend():
    group_profile = SimpleNamespace(
        atmosphere_history=[
            SimpleNamespace(group_valence=-0.4),
            SimpleNamespace(group_valence=0.0),
            SimpleNamespace(group_valence=0.5),
            SimpleNamespace(group_valence=0.7),
        ],
    )
    style_params = StyleAdapter().adapt(pace="steady", persona=None)

    bundle = PromptFactory.assemble_chat(
        message_content="hello",
        group_profile=group_profile,
        style_params=style_params,
        other_ai_names=[],
    )

    assert "【氛围趋势】" not in bundle.system_prompt
    assert "群聊氛围正在" not in bundle.system_prompt


def test_assemble_chat_puts_function_call_and_qq_mentions_in_output_spec():
    group_profile = SimpleNamespace(atmosphere_history=[])
    style_params = StyleAdapter().adapt(pace="steady", persona=None)

    bundle = PromptFactory.assemble_chat(
        message_content="hello",
        group_profile=group_profile,
        style_params=style_params,
        other_ai_names=[],
        skill_registry=object(),
        adapter_type="napcat",
        qq_mention_members=[{"user_id": "123456", "nickname": "Alice"}],
    )

    assert "【输出规范】" in bundle.system_prompt
    assert "Function Call" in bundle.system_prompt
    assert "@{QQ号}" in bundle.system_prompt
    assert "【Function Call】" not in bundle.system_prompt
    assert "【QQ @提及】" not in bundle.system_prompt


def test_assemble_chat_when_plan_flow_then_uses_plan_finish_tools():
    group_profile = SimpleNamespace(atmosphere_history=[])
    style_params = StyleAdapter().adapt(pace="steady", persona=None)

    bundle = PromptFactory.assemble_chat(
        message_content="hello",
        group_profile=group_profile,
        style_params=style_params,
        other_ai_names=[],
        skill_registry=object(),
        tool_flow_mode="plan",
    )

    assert "exit_plan" in bundle.system_prompt
    assert "abort_plan" in bundle.system_prompt
    assert "continue 表示" not in bundle.system_prompt


class _NoopDiaryRetriever:
    def retrieve(self, **kwargs):
        return []


class _StaticDiaryRetriever:
    def __init__(self, entries):
        self.entries = entries

    def retrieve(self, **kwargs):
        return self.entries


def test_context_assembler_when_diary_exists_then_injects_user_message_not_system():
    diary_entry = SimpleNamespace(
        created_at="2026-06-21T10:11:12",
        content="Alice promised to deploy after lunch.",
        summary="deployment promise",
    )
    assembler = ContextAssembler(
        BasicMemoryManager(),
        _StaticDiaryRetriever([diary_entry]),
    )

    messages = assembler.build_messages(
        group_id="group_a",
        current_query="What should I do next?",
        system_prompt="system",
    )

    assert TAG_HISTORY_DIARY not in messages[0]["content"]
    assert TAG_HISTORY_DIARY in messages[-1]["content"]
    assert "Alice promised to deploy after lunch." in messages[-1]["content"]
    assert "What should I do next?" in messages[-1]["content"]


def test_context_assembler_keeps_completed_history_in_system_prefix_until_diary_promotion():
    basic = BasicMemoryManager()
    basic.add_entry("group_a", "alice", "human", "first human", speaker_name="Alice")
    basic.add_entry("group_a", "assistant", "assistant", "first reply", speaker_name="Bot")
    basic.add_entry("group_a", "bob", "human", "pending human", speaker_name="Bob")
    assembler = ContextAssembler(
        basic,
        _NoopDiaryRetriever(),
        is_source_diarized=lambda _group_id, _entry_id: False,
    )

    messages = assembler.build_messages(
        group_id="group_a",
        current_query="current question",
        system_prompt="system",
        speaker_user_id="bob",
        speaker_name="Bob",
    )
    system_before = messages[0]["content"]

    assert system_before.startswith("【历史聊天信息】")
    assert "first human" in system_before
    assert "first reply" in system_before
    assert "pending human" not in system_before
    assert "尚未被日记记忆系统收录的近期原始消息" not in system_before
    assert "<conversation_history>" not in system_before
    assert "</conversation_history>" not in system_before
    assert '<message speaker="Alice"' in system_before
    assert [message["role"] for message in messages[1:]] == ["user"]

    basic.add_entry("group_a", "charlie", "human", "another pending", speaker_name="Charlie")
    messages_after_pending = assembler.build_messages(
        group_id="group_a",
        current_query="current question",
        system_prompt="system",
        speaker_user_id="bob",
        speaker_name="Bob",
    )

    assert messages_after_pending[0]["content"] == system_before


def test_context_assembler_removes_diarized_sources_from_system_prefix():
    basic = BasicMemoryManager()
    first = basic.add_entry("group_a", "alice", "human", "first human", speaker_name="Alice")
    second = basic.add_entry("group_a", "assistant", "assistant", "first reply", speaker_name="Bot")
    diarized = {first.entry_id, second.entry_id}
    assembler = ContextAssembler(
        basic,
        _NoopDiaryRetriever(),
        is_source_diarized=lambda _group_id, entry_id: entry_id in diarized,
    )

    messages = assembler.build_messages(
        group_id="group_a",
        current_query="current question",
        system_prompt="system",
    )

    assert "【历史聊天信息】" not in messages[0]["content"]
    assert "first human" not in messages[0]["content"]
    assert "first reply" not in messages[0]["content"]
