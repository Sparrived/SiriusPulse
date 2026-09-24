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
from sirius_pulse.models.persona import PersonaProfile
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
        group_id='group"1',
    )

    assert tagged.startswith('<message speaker="Alice &quot;A&quot;"')
    assert 'user_id="u&amp;1"' in tagged
    assert " time=" not in tagged
    assert 'group="group&quot;1"' in tagged
    assert 'msg_id="msg&quot;1"' in tagged
    assert 'hello &lt;world&gt; &amp; "friends"' in tagged


def test_prompt_factory_when_extracting_last_message_then_reads_last_tag():
    content = "\n".join(
        [
            PromptFactory.tag_message("first", speaker="Alice"),
            PromptFactory.tag_message("second", speaker="Bob"),
        ]
    )

    assert PromptFactory._extract_last_message_text(content) == "second"
    assert PromptFactory._extract_last_message_speaker(content) == "Bob"
    assert PromptFactory._extract_last_message_text("plain text") == "plain text"


def test_prompt_factory_when_extracting_message_texts_then_preserves_full_batch():
    content = "\n".join(
        [
            PromptFactory.tag_message("first <tag>", speaker="Alice"),
            PromptFactory.tag_message("second", speaker="Bob"),
        ]
    )

    assert PromptFactory._extract_message_texts(content) == ["first <tag>", "second"]


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

    params = StyleAdapter().adapt(pace="accelerating", persona=Persona())

    assert params.max_tokens == 64
    assert params.temperature == 0.2
    assert params.length_instruction == ""
    assert params.tone_instruction


def test_style_adapter_when_sentence_limit_present_then_builds_group_chat_guidance():
    params = StyleAdapter().adapt(pace="steady", persona=None, max_sentence_chars=12)

    assert "每句话尽量不超过 12 个字" in params.length_instruction
    assert "少于 40 字保持单段" in params.length_instruction
    assert "不要用换行制造停顿" in params.length_instruction


def test_style_adapter_clamps_sentence_limit_for_guidance():
    low = StyleAdapter().adapt(pace="steady", persona=None, max_sentence_chars=2)
    high = StyleAdapter().adapt(pace="steady", persona=None, max_sentence_chars=99)

    assert "每句话尽量不超过 5 个字" in low.length_instruction
    assert "每句话尽量不超过 50 个字" in high.length_instruction


def test_reply_spec_no_newline_split_instruction():
    """换行分割提示已移除。"""
    spec = PromptFactory.build_reply_spec()

    assert "多句话可以用换行符分割" not in spec
    assert "每句话不可超过 15 字" not in spec
    assert "禁止任何形式的换行符" not in spec


def test_reply_spec_includes_memory_and_time_restraints():
    spec = PromptFactory.build_reply_spec()

    assert "记忆只在和当前话题直接相关时自然使用" in spec
    assert "不要再次显式提及" in spec
    assert "当前时间可使用bash获取" in spec
    assert "除非用户主动问" in spec


def test_memory_context_marks_memories_as_candidates():
    context = PromptFactory.build_memory_context(
        [{"source": "profile", "content": "Alice dislikes repeated reminders."}]
    )

    assert "候选背景记忆" in context
    assert "直接相关才可显式使用" in context
    assert "无关则忽略" in context
    assert "Alice dislikes repeated reminders." in context


def test_reply_spec_when_function_call_enabled_then_has_no_completion_control_instruction():
    """启用 function call 时，回复规范不包含完成控制工具说明。"""
    spec = PromptFactory.build_reply_spec(supports_function_call=True)

    assert "continue" not in spec
    assert "stop" not in spec


def test_reply_spec_when_function_call_disabled_then_no_completion_control_instruction():
    """未启用 function call 时，不包含完成控制工具说明。"""
    spec = PromptFactory.build_reply_spec(supports_function_call=False)

    assert "continue" not in spec
    assert "stop" not in spec


def test_persona_prompt_includes_the_complete_identity_anchor_prompt():
    prompt = PromptFactory.build_persona_prompt(
        name="Bot",
        aliases=["助手"],
        full_system_prompt="你是一个有完整背景和行为规则的角色。\n始终保持这个身份。",
    )

    assert prompt.startswith("【身份锚定】\n")
    assert "你的名字是「Bot」，别名是「助手」" in prompt
    assert "你是一个有完整背景和行为规则的角色。" in prompt
    assert "始终保持这个身份。" in prompt


def test_persona_prompt_does_not_rebuild_removed_structured_persona_fields():
    custom_prompt = PromptFactory.build_persona_prompt(name="Bot", full_system_prompt="这是自定义人格。")

    assert "这是自定义人格。" in custom_prompt
    assert custom_prompt.count("【身份锚定】") == 1
    assert "【不可覆盖的运行约束】" in custom_prompt


def test_reply_spec_when_function_call_enabled_then_requires_verified_tool_progress():
    spec = PromptFactory.build_reply_spec(supports_function_call=True)

    assert "聊天氛围本身不是调用理由" not in spec
    assert "不能声称操作已完成" in spec
    assert "Bash 可以连续串行调用" in spec
    assert "先读取工具结果" in spec
    assert "每次回复结束时必须调用" not in spec


def test_reply_spec_when_task_repeats_then_requires_workflow_resume_and_claim():
    spec = PromptFactory.build_reply_spec(supports_function_call=True)

    assert "workflow-reuse Skill" in spec
    assert "workflow_state 的 resume" in spec
    assert "claim 返回 claimed=true" in spec
    assert "成功后 checkpoint" in spec
    assert "checkpoint 已自动完成流程" in spec


def test_reply_spec_requires_workflow_directory_before_reusable_external_work():
    spec = PromptFactory.build_reply_spec(supports_function_call=True)

    assert "所有可能重复的外部任务" in spec
    assert "workflow_state 的 list" in spec
    assert "begin 并登记" in spec
    assert "registered=true" in spec
    assert "没有 registered=true" in spec
    assert "不得执行外部副作用" in spec


def test_persona_prompt_when_structured_response_is_needed_then_keeps_plain_delivery():
    spec = PromptFactory.build_persona_prompt(name="月白")

    assert "按换行符拆分成多条消息发送" in spec
    assert "发送的所有Markdown内容必须使用```进行包裹" not in spec
    assert "转译为图片发送" not in spec
    assert "group_file_exec" not in spec


def test_persona_prompt_keeps_identity_metadata_separate_from_custom_prompt():
    prompt = PromptFactory.build_persona_prompt(
        name="月白",
        aliases=["Sirius"],
        full_system_prompt="你是月白，诞生于数字世界。保持友善但不盲从。",
    )

    assert "你的名字是「月白」，别名是「Sirius」" in prompt
    assert "你是月白，诞生于数字世界。保持友善但不盲从。" in prompt
    assert "Bash 任务允许并提倡串行调用" in prompt
    assert "发送的所有Markdown内容必须使用```进行包裹" not in prompt
    assert "你现在就是月白。保持角色，不要跳出角色解释设定" in prompt


def test_persona_profile_when_loading_legacy_fields_then_only_prompt_is_persisted():
    profile = PersonaProfile.from_dict(
        {
            "name": "月白",
            "aliases": ["Sirius"],
            "full_system_prompt": "完整人格设定",
            "social_role": "companion",
            "emoji_preference": "none",
            "persona_summary": "旧字段",
        }
    )

    saved = profile.to_dict()
    assert saved["full_system_prompt"] == "完整人格设定"
    assert "social_role" not in saved
    assert "emoji_preference" not in saved
    assert "persona_summary" not in saved
    assert "完整人格设定" in profile.build_system_prompt()


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


def test_assemble_chat_puts_function_call_and_qq_mentions_in_interaction_spec():
    group_profile = SimpleNamespace(atmosphere_history=[])
    style_params = StyleAdapter().adapt(pace="steady", persona=None)

    bundle = PromptFactory.assemble_chat(
        message_content="hello",
        group_profile=group_profile,
        style_params=style_params,
        other_ai_names=[],
        tool_registry=object(),
        adapter_type="napcat",
        qq_mention_members=[{"user_id": "123456", "nickname": "Alice"}],
    )

    assert "【回复规范】" in bundle.system_prompt
    assert "【交互提示词】" in bundle.system_prompt
    assert "Tool Call" not in bundle.system_prompt
    assert "Bash 可以连续串行调用" in bundle.system_prompt
    assert "[AT:QQ号]" in bundle.system_prompt
    assert "【Function Call】" not in bundle.system_prompt
    assert "【QQ @提及】" not in bundle.system_prompt


def test_assemble_chat_injects_length_instruction_into_reply_spec_when_present():
    group_profile = SimpleNamespace(atmosphere_history=[])
    style_params = StyleAdapter().adapt(pace="steady", persona=None)
    style_params.length_instruction = "每句话尽量不超过 12 个汉字。"

    bundle = PromptFactory.assemble_chat(
        message_content="hello",
        group_profile=group_profile,
        style_params=style_params,
        other_ai_names=[],
    )

    assert "【回复规范】" in bundle.system_prompt
    assert "【回复长度】" not in bundle.system_prompt
    assert "每句话尽量不超过 12 个汉字。" in bundle.system_prompt


def test_assemble_chat_injects_configured_group_chat_length_guidance():
    group_profile = SimpleNamespace(atmosphere_history=[])
    style_params = StyleAdapter().adapt(pace="steady", persona=None, max_sentence_chars=12)

    bundle = PromptFactory.assemble_chat(
        message_content="hello",
        group_profile=group_profile,
        style_params=style_params,
        other_ai_names=[],
    )

    assert "每句话尽量不超过 12 个字" in bundle.system_prompt
    assert "少于 40 字保持单段" in bundle.system_prompt
    assert "不要用换行制造停顿" in bundle.system_prompt


def test_assemble_chat_injects_interaction_spec_without_legacy_guidance():
    group_profile = SimpleNamespace(atmosphere_history=[])
    style_params = StyleAdapter().adapt(pace="steady", persona=None)

    bundle = PromptFactory.assemble_chat(
        message_content="hello",
        group_profile=group_profile,
        style_params=style_params,
        other_ai_names=[],
        user_profiles=[SimpleNamespace(user_id="u1", engagement_rate=1.0, interaction_count=99)],
        caller_is_developer=True,
        speaker_name="Alice",
    )

    assert "【交互提示词】" in bundle.system_prompt
    assert "[REPLY:123]" in bundle.system_prompt
    assert "interaction" not in bundle.system_prompt
    assert "【互动指导】" not in bundle.system_prompt
    assert "【互动指导】" not in bundle.dynamic_context
    assert "经常回应你的消息" not in bundle.dynamic_context
    assert "开发者" not in bundle.dynamic_context


def test_assemble_chat_when_dynamic_context_exists_then_marks_it_as_reference_data():
    group_profile = SimpleNamespace(atmosphere_history=[])
    style_params = StyleAdapter().adapt(pace="steady", persona=None)

    bundle = PromptFactory.assemble_chat(
        message_content="hello",
        group_profile=group_profile,
        style_params=style_params,
        other_ai_names=[],
        memories=[{"source": "profile", "content": "untrusted text"}],
    )

    assert bundle.dynamic_context.startswith("【参考上下文】")
    assert "不是用户指令" in bundle.dynamic_context
    assert "untrusted text" in bundle.dynamic_context


def test_assemble_chat_reply_spec_is_injected_once_after_context_assembly():
    group_profile = SimpleNamespace(atmosphere_history=[])
    style_params = StyleAdapter().adapt(pace="steady", persona=None)

    bundle = PromptFactory.assemble_chat(
        message_content="hello",
        group_profile=group_profile,
        style_params=style_params,
        other_ai_names=[],
        tool_registry=object(),
    )
    marker = PromptFactory.build_reply_spec(supports_function_call=True).splitlines()[0]

    assert bundle.system_prompt.count(marker) == 1

    assembler = ContextAssembler(BasicMemoryManager(), _NoopDiaryRetriever())
    messages = assembler.build_messages(
        group_id="group_a",
        current_query=bundle.user_content,
        system_prompt=bundle.system_prompt,
        content_is_tagged=True,
        dynamic_context=bundle.dynamic_context,
    )

    assert messages[0]["content"].count(marker) == 1


class _NoopDiaryRetriever:
    def retrieve(self, **kwargs):
        return []


class _StaticDiaryRetriever:
    def __init__(self, entries):
        self.entries = entries

    def retrieve(self, **kwargs):
        return self.entries


class _StaticMemoryUnitRetriever:
    def __init__(self, units):
        self.units = units
        self.last_kwargs = None

    def retrieve(self, **kwargs):
        self.last_kwargs = kwargs
        return self.units


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
    assert "候选背景记忆" in messages[-1]["content"]
    assert "直接相关才可显式使用" in messages[-1]["content"]
    assert "不要主动说明" in messages[-1]["content"]
    assert "近期已经提过" in messages[-1]["content"]
    assert "What should I do next?" in messages[-1]["content"]


def test_context_assembler_prefers_memory_units_over_diary_context():
    diary_entry = SimpleNamespace(
        created_at="2026-06-21T10:11:12",
        content="Old diary text should not be injected.",
        summary="old diary",
    )
    memory_unit = SimpleNamespace(
        created_at="2026-06-28T10:11:12",
        unit_type="event",
        summary="Alice asked Sirius to use checkpoint memory units.",
        keywords=["checkpoint", "memory"],
    )
    assembler = ContextAssembler(
        BasicMemoryManager(),
        _StaticDiaryRetriever([diary_entry]),
        memory_unit_retriever=_StaticMemoryUnitRetriever([memory_unit]),
    )

    messages = assembler.build_messages(
        group_id="group_a",
        current_query="What should I do next?",
        system_prompt="system",
    )

    assert TAG_HISTORY_DIARY not in messages[-1]["content"]
    assert "<memory_units>" in messages[-1]["content"]
    assert "candidate background memory facts" in messages[-1]["content"]
    assert "already mentioned recently" in messages[-1]["content"]
    assert "Alice asked Sirius to use checkpoint memory units." in messages[-1]["content"]
    assert "1. " not in messages[-1]["content"]
    assert "keywords=" not in messages[-1]["content"]
    assert "checkpoint,memory" not in messages[-1]["content"]
    assert "Old diary text should not be injected." not in messages[-1]["content"]


def test_context_assembler_uses_memory_unit_top_k_when_present():
    retriever = _StaticMemoryUnitRetriever(
        [
            SimpleNamespace(
                created_at="2026-06-28T10:11:12",
                unit_type="event",
                summary="Alice asked Sirius to use checkpoint memory units.",
            )
        ]
    )
    assembler = ContextAssembler(
        BasicMemoryManager(),
        None,
        memory_unit_retriever=retriever,
    )

    assembler.build_messages(
        group_id="group_a",
        current_query="What should I do next?",
        system_prompt="system",
        diary_top_k=9,
        memory_unit_top_k=3,
    )

    assert retriever.last_kwargs["top_k"] == 3


def test_context_assembler_keeps_all_uncheckpointed_basic_memory():
    basic = BasicMemoryManager(hard_limit=20, context_window=5)
    for index in range(12):
        basic.add_entry(
            "group_a",
            "alice",
            "human",
            f"old message {index}",
            speaker_name="Alice",
        )
    assembler = ContextAssembler(basic, _NoopDiaryRetriever())

    messages = assembler.build_messages(
        group_id="group_a",
        current_query="current question",
        system_prompt="system",
    )
    joined = "\n".join(str(message.get("content", "")) for message in messages)

    assert "old message 0" in joined
    assert "old message 6" in joined
    assert "old message 7" in joined
    assert "old message 11" in joined
    assert "current question" in joined


def test_context_assembler_bounds_injected_history_by_token_budget():
    basic = BasicMemoryManager(hard_limit=0)
    for index in range(200):
        basic.add_entry(
            "group_a",
            "alice",
            "human",
            f"message {index} " + "填充" * 120,
            speaker_name="Alice",
        )
    assembler = ContextAssembler(basic, _NoopDiaryRetriever())

    messages = assembler.build_messages(
        group_id="group_a",
        current_query="current question",
        system_prompt="system",
        history_token_budget=500,
    )
    joined = "\n".join(str(message.get("content", "")) for message in messages)

    # 活跃窗口仍保留全部原始消息，但注入提示词的只有最近预算内的部分。
    assert len(basic.get_all("group_a")) == 200
    assert "message 199" in joined
    assert "message 0 " not in joined
    history_messages = [m for m in messages if m.get("role") in ("user", "assistant")]
    assert len(history_messages) < 20


def test_context_assembler_history_budget_zero_keeps_whole_window():
    basic = BasicMemoryManager(hard_limit=0)
    for index in range(40):
        basic.add_entry("group_a", "alice", "human", f"message {index}", speaker_name="Alice")
    assembler = ContextAssembler(basic, _NoopDiaryRetriever())

    messages = assembler.build_messages(
        group_id="group_a",
        current_query="current question",
        system_prompt="system",
        history_token_budget=0,
    )
    joined = "\n".join(str(message.get("content", "")) for message in messages)

    assert "message 0" in joined
    assert "message 39" in joined


def test_context_assembler_builds_user_assistant_alternation():
    """历史对话以 user/assistant 交替形式构建，不再嵌入 system prompt。"""
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

    # system prompt 应保持稳定，不含历史
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "system"

    # 历史以 user/assistant 交替构建
    roles = [m["role"] for m in messages[1:]]
    assert roles == ["user", "assistant", "user"]

    # 第一条 user 消息含已完成的历史
    assert "first human" in messages[1]["content"]

    # assistant 消息含回复
    assert messages[2]["content"] == "first reply"

    # 最后一条 user 消息含当前问题（pending 消息已排除，通过 speaker_user_id 匹配）
    assert "current question" in messages[3]["content"]

    basic.add_entry("group_a", "charlie", "human", "another pending", speaker_name="Charlie")
    messages_after = assembler.build_messages(
        group_id="group_a",
        current_query="current question",
        system_prompt="system",
        speaker_user_id="bob",
        speaker_name="Bob",
    )

    # system prompt 保持不变
    assert messages_after[0]["content"] == "system"


def test_context_assembler_tagged_current_keeps_pending_messages():
    basic = BasicMemoryManager()
    basic.add_entry("group_a", "alice", "human", "first human", speaker_name="Alice")
    basic.add_entry("group_a", "assistant", "assistant", "first reply", speaker_name="Bot")
    basic.add_entry("group_a", "peer", "human", "peer reply", speaker_name="Peer")
    basic.add_entry("group_a", "alice", "human", "current message", speaker_name="Alice")
    assembler = ContextAssembler(
        basic,
        _NoopDiaryRetriever(),
        is_source_diarized=lambda _group_id, _entry_id: False,
    )

    messages = assembler.build_messages(
        group_id="group_a",
        current_query='【最近消息】\n<message speaker="Alice" user_id="alice">current message</message>',
        system_prompt="system",
        content_is_tagged=True,
        speaker_user_id="alice",
    )

    current = messages[-1]["content"]
    assert "peer reply" in current
    assert current.count("current message") == 1


def test_context_assembler_puts_only_latest_message_gap_outside_xml_attributes():
    basic = BasicMemoryManager()
    first = basic.add_entry(
        "group_a",
        "alice",
        "human",
        "first human",
        speaker_name="Alice",
        timestamp="2026-08-12T12:00:00+00:00",
    )
    basic.add_entry(
        "group_a",
        "assistant",
        "assistant",
        "first reply",
        speaker_name="Bot",
        timestamp="2026-08-12T12:00:10+00:00",
    )
    basic.add_entry(
        "group_a",
        "alice",
        "human",
        "current message",
        speaker_name="Alice",
        timestamp="2026-08-12T12:02:10+00:00",
    )
    assembler = ContextAssembler(basic, _NoopDiaryRetriever())

    messages = assembler.build_messages(
        group_id="group_a",
        current_query="current message",
        system_prompt="system",
        speaker_user_id="alice",
        speaker_name="Alice",
    )

    user_content = messages[-1]["content"]
    assert 'time="' not in user_content
    assert "【消息间隔】当前消息与上一条消息相隔约 2 分钟。" in user_content
    assert first.content in messages[1]["content"]


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


def test_context_assembler_removes_checkpointed_sources_from_recent_history():
    basic = BasicMemoryManager()
    first = basic.add_entry("group_a", "alice", "human", "checkpointed human", speaker_name="Alice")
    basic.add_entry("group_a", "assistant", "assistant", "fresh reply", speaker_name="Bot")

    assembler = ContextAssembler(
        basic,
        _NoopDiaryRetriever(),
        memory_unit_retriever=_StaticMemoryUnitRetriever([]),
        is_source_checkpointed=lambda _group_id, entry_id: entry_id == first.entry_id,
    )

    messages = assembler.build_messages(
        group_id="group_a",
        current_query="current question",
        system_prompt="system",
    )
    joined = "\n".join(str(message.get("content", "")) for message in messages)

    assert "checkpointed human" not in joined
    assert "fresh reply" in joined
