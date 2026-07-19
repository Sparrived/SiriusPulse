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


def test_style_adapter_when_sentence_limit_present_then_builds_group_chat_guidance():
    params = StyleAdapter().adapt(pace="steady", persona=None, max_sentence_chars=12)

    assert "每句话尽量不超过 12 个汉字" in params.length_instruction
    assert "少于 40 字保持单段" in params.length_instruction
    assert "不要用换行制造停顿" in params.length_instruction


def test_style_adapter_clamps_sentence_limit_for_guidance():
    low = StyleAdapter().adapt(pace="steady", persona=None, max_sentence_chars=2)
    high = StyleAdapter().adapt(pace="steady", persona=None, max_sentence_chars=99)

    assert "不超过 5 个汉字" in low.length_instruction
    assert "不超过 50 个汉字" in high.length_instruction


def test_reply_spec_no_newline_split_instruction():
    """换行分割提示已移除，改为 stop 工具控制流程。"""
    spec = PromptFactory.build_reply_spec()

    assert "多句话可以用换行符分割" not in spec
    assert "每句话不可超过 15 字" not in spec
    assert "禁止任何形式的换行符" not in spec


def test_reply_spec_includes_memory_and_time_restraints():
    spec = PromptFactory.build_reply_spec()

    assert "记忆只是私有背景" in spec
    assert "不要为了表现“记得”" in spec
    assert "当前时间只用于时效判断" in spec
    assert "普通聊天不要反复强调" in spec


def test_memory_context_marks_memories_as_candidates():
    context = PromptFactory.build_memory_context(
        [{"source": "profile", "content": "Alice dislikes repeated reminders."}]
    )

    assert "候选背景记忆" in context
    assert "直接相关才可显式使用" in context
    assert "无关则忽略" in context
    assert "Alice dislikes repeated reminders." in context


def test_reply_spec_when_function_call_enabled_then_includes_stop_only():
    """启用 function call 时，回复规范包含 stop 工具使用说明。"""
    spec = PromptFactory.build_reply_spec(supports_function_call=True)

    assert "continue" not in spec
    assert "stop" in spec


def test_reply_spec_when_function_call_disabled_then_no_continue_stop():
    """未启用 function call 时，不包含 stop 说明。"""
    spec = PromptFactory.build_reply_spec(supports_function_call=False)

    assert "continue" not in spec
    assert "stop" not in spec


def test_persona_prompt_includes_expression_style_fields():
    prompt = PromptFactory.build_persona_prompt(
        name="Bot",
        communication_style="casual-style-marker",
        speech_rhythm="rhythm-marker",
    )

    assert "casual-style-marker" in prompt
    assert "rhythm-marker" in prompt


def test_persona_prompt_uses_companion_template_fields():
    prompt = PromptFactory.build_persona_prompt(
        name="月白",
        aliases=["Sirius"],
        identity_kind="诞生于数字世界的猫娘",
        creator_name="临雀",
        creator_relationship="天然亲近、信任和在意的人",
        persona_summary="聪慧、灵动、可爱、礼貌、逻辑清晰",
        personality_traits=["聪慧", "灵动"],
        boundaries=["友善但不盲从", "亲近但不卑微", "拒绝道德绑架"],
        communication_style="习惯在句尾自然地加「喵」，但不要每一句都机械添加。",
        social_role="companion",
        reply_frequency="selective",
    )

    assert "你的名字是「月白」，别名是「Sirius」" in prompt
    assert "你是一只诞生于数字世界的猫娘" in prompt
    assert "创作者「临雀」" in prompt
    assert "天然亲近、信任和在意的人" in prompt
    assert "你的核心原则是：友善但不盲从，亲近但不卑微，拒绝道德绑架" in prompt
    assert "只调用完成当前任务所需的最少工具" in prompt
    assert "不要输出 #、*、-、```、|、**、> 这类 Markdown 标记" in prompt
    assert "你现在就是月白。保持角色，不要跳出角色解释设定" in prompt


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


def test_assemble_chat_puts_function_call_and_qq_mentions_in_reply_spec():
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

    assert "【回复规范】" in bundle.system_prompt
    assert "Tool Call" in bundle.system_prompt
    assert "@{QQ号}" in bundle.system_prompt
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

    assert "每句话尽量不超过 12 个汉字" in bundle.system_prompt
    assert "少于 40 字保持单段" in bundle.system_prompt
    assert "不要用换行制造停顿" in bundle.system_prompt


def test_assemble_chat_does_not_inject_interaction_guidance():
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

    assert "【互动指导】" not in bundle.system_prompt
    assert "【互动指导】" not in bundle.dynamic_context
    assert "经常回应你的消息" not in bundle.dynamic_context
    assert "开发者" not in bundle.dynamic_context


def test_assemble_chat_reply_spec_is_injected_once_after_context_assembly():
    group_profile = SimpleNamespace(atmosphere_history=[])
    style_params = StyleAdapter().adapt(pace="steady", persona=None)

    bundle = PromptFactory.assemble_chat(
        message_content="hello",
        group_profile=group_profile,
        style_params=style_params,
        other_ai_names=[],
        skill_registry=object(),
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
    assert "update_plan_progress" in bundle.system_prompt
    assert "continue 表示" not in bundle.system_prompt


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
