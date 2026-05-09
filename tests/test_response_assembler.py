"""Tests for PromptFactory, StyleAdapter (prompt construction and style adaptation)."""

from __future__ import annotations

import pytest

from sirius_chat.core.prompt_factory import PromptFactory, StyleAdapter, StyleParams, PromptBundle
from sirius_chat.models.emotion import EmotionState
from sirius_chat.models.intent_v3 import IntentAnalysisV3, SocialIntent
from sirius_chat.models.models import Message
from sirius_chat.memory.semantic.models import (
    GroupSemanticProfile,
    UserSemanticProfile,
    AtmosphereSnapshot,
)


class TestStyleAdapter:
    def test_pace_accelerating_suggests_brief(self):
        """加速节奏应给出简短提示，不截断 max_tokens。"""
        adapter = StyleAdapter()
        style = adapter.adapt(pace="accelerating")
        assert style.max_tokens == 4096
        assert style.temperature == 0.7
        assert "简短" in style.length_instruction

    def test_pace_decelerating_allows_expansion(self):
        """放缓节奏应给出展开提示，不截断 max_tokens。"""
        adapter = StyleAdapter()
        style = adapter.adapt(pace="decelerating")
        assert style.max_tokens == 4096
        assert "展开" in style.length_instruction

    def test_pace_silent_suggests_more(self):
        """安静节奏应给出多说提示，不截断 max_tokens。"""
        adapter = StyleAdapter()
        style = adapter.adapt(pace="silent")
        assert style.max_tokens == 4096
        assert "多说" in style.length_instruction


class TestPromptFactoryAssemble:
    def test_assemble_includes_all_sections(self):
        from sirius_chat.models.persona import PersonaProfile
        persona = PersonaProfile(name="TestBot")
        msg = Message(role="human", content="我今天心情不好", speaker="u1")
        emotion = EmotionState(valence=-0.6, arousal=0.5, intensity=0.7)
        adapter = StyleAdapter()
        style = adapter.adapt(pace="steady")

        bundle = PromptFactory.assemble_chat(
            persona_prompt=persona.build_system_prompt(),
            message_content=msg.content,
            speaker_name=msg.speaker,
            emotion=emotion,
            memories=[{"source": "working_memory", "content": "用户上周说工作压力大"}],
            group_profile=None,
            style_params=style,
            other_ai_names=[],
        )

        assert "你在一个多人聊天场景里" in bundle.system_prompt
        assert "发言者情绪" in bundle.system_prompt
        assert "相关记忆" in bundle.system_prompt
        assert "工作压力大" in bundle.system_prompt
        assert "我今天心情不好" in bundle.user_content

    def test_assemble_with_group_profile(self):
        from sirius_chat.models.persona import PersonaProfile
        persona = PersonaProfile(name="TestBot")
        msg = Message(role="human", content="hello", speaker="u1")
        emotion = EmotionState(valence=0.2, arousal=0.3, intensity=0.5)
        adapter = StyleAdapter()
        style = adapter.adapt(pace="steady")

        group = GroupSemanticProfile(group_id="g1", typical_interaction_style="humorous")
        group.atmosphere_history.append(AtmosphereSnapshot(
            timestamp="2026-04-17T10:00:00", group_valence=0.3, group_arousal=0.4
        ))

        bundle = PromptFactory.assemble_chat(
            persona_prompt=persona.build_system_prompt(),
            message_content=msg.content,
            speaker_name=msg.speaker,
            emotion=emotion,
            group_profile=group,
            style_params=style,
            other_ai_names=[],
        )

        assert "群体风格" in bundle.system_prompt
        assert "轻松幽默" in bundle.system_prompt
        assert "群里氛围" in bundle.system_prompt

    def test_assemble_with_user_profile(self):
        from sirius_chat.models.persona import PersonaProfile
        persona = PersonaProfile(name="TestBot")
        msg = Message(role="human", content="test", speaker="u1")
        emotion = EmotionState(valence=0.0, arousal=0.0, intensity=0.0)
        adapter = StyleAdapter()
        style = adapter.adapt(pace="steady")

        user = UserSemanticProfile(user_id="u1")
        group = GroupSemanticProfile(group_id="g1")

        bundle = PromptFactory.assemble_chat(
            persona_prompt=persona.build_system_prompt(),
            message_content=msg.content,
            speaker_name=msg.speaker,
            emotion=emotion,
            group_profile=group,
            user_profiles=[user],
            style_params=style,
            other_ai_names=[],
        )

        assert bundle.system_prompt
        assert "test" in bundle.user_content

    def test_assemble_delayed_uses_scene_description(self):
        from sirius_chat.models.persona import PersonaProfile
        persona = PersonaProfile(name="TestBot")
        adapter = StyleAdapter()
        style = adapter.adapt(pace="decelerating")

        bundle = PromptFactory.assemble_chat(
            persona_prompt=persona.build_system_prompt(),
            message_content="刚才的话题很有趣",
            group_profile=GroupSemanticProfile(group_id="g1", typical_interaction_style="humorous"),
            style_params=style,
            other_ai_names=[],
            scene_description="群里的话题有了自然间隙，你决定插一句。",
        )
        assert "话题有了自然间隙" in bundle.system_prompt
        assert "刚才的话题很有趣" in bundle.user_content
        assert "轻松幽默" in bundle.system_prompt

    def test_assemble_proactive(self):
        from sirius_chat.models.persona import PersonaProfile
        persona = PersonaProfile(name="TestBot")

        bundle = PromptFactory.assemble_proactive(
            persona_prompt=persona.build_system_prompt(),
            trigger_reason="silence_30min",
            group_profile=GroupSemanticProfile(group_id="g1", interest_topics=["photography", "travel"]),
            suggested_tone="casual",
            other_ai_names=[],
        )
        assert "silence_30min" in bundle.system_prompt
        assert "casual" in bundle.system_prompt
        assert "photography" in bundle.system_prompt


class TestPromptFactoryOutputSpec:
    def test_prompt_contains_output_spec(self):
        from sirius_chat.models.persona import PersonaProfile
        persona = PersonaProfile(name="TestBot")
        msg = Message(role="human", content="你好")
        emotion = EmotionState()
        adapter = StyleAdapter()
        style = adapter.adapt(pace="steady")

        bundle = PromptFactory.assemble_chat(
            persona_prompt=persona.build_system_prompt(),
            message_content=msg.content,
            speaker_name=msg.speaker,
            emotion=emotion,
            group_profile=None,
            style_params=style,
            other_ai_names=[],
        )
        assert "输出规范" in bundle.system_prompt
        assert "直接输出" in bundle.system_prompt
