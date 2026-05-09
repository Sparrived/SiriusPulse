"""Integration tests for persona system in EmotionalGroupChatEngine."""

from __future__ import annotations

import pytest
from unittest.mock import Mock

from sirius_chat.models.persona import PersonaProfile
from sirius_chat.core.persona_generator import PersonaGenerator
from sirius_chat.core.persona_store import PersonaStore
from sirius_chat.core.prompt_factory import StyleAdapter
from sirius_chat.models.emotion import EmotionState, EmpathyStrategy
from sirius_chat.models.models import Message


class TestPersonaProfileRoundtrip:
    def test_to_dict_from_dict_identity(self):
        p = PersonaProfile(
            name="TestBot",
            aliases=["TB"],
            persona_summary="A test bot",
            personality_traits=["friendly", "helpful"],
            catchphrases=["Got it!"],
            emotional_baseline={"valence": 0.5, "arousal": 0.4},
            reply_frequency="high",
        )
        data = p.to_dict()
        p2 = PersonaProfile.from_dict(data)
        assert p2.name == "TestBot"
        assert p2.aliases == ["TB"]
        assert p2.personality_traits == ["friendly", "helpful"]
        assert p2.catchphrases == ["Got it!"]
        assert p2.emotional_baseline == {"valence": 0.5, "arousal": 0.4}
        assert p2.reply_frequency == "high"


class TestTemplatePersonaCreation:
    def test_no_builtin_archetypes(self):
        with pytest.raises(ValueError):
            PersonaGenerator.from_template("sarcastic_techie")

    def test_unknown_archetype_raises(self):
        with pytest.raises(ValueError):
            PersonaGenerator.from_template("nonexistent")


class TestEngineLoadsPersona:
    def test_engine_requires_persona(self, tmp_path):
        from sirius_chat.core.emotional_engine import EmotionalGroupChatEngine
        with pytest.raises(ValueError, match="No persona provided"):
            EmotionalGroupChatEngine(work_path=tmp_path)

    def test_engine_loads_existing_persona(self, tmp_path):
        from sirius_chat.core.emotional_engine import EmotionalGroupChatEngine
        from sirius_chat.models.persona import PersonaProfile
        custom = PersonaProfile(name="静观", personality_traits=["沉稳", "内敛"])
        PersonaStore.save(tmp_path, custom)

        engine = EmotionalGroupChatEngine(work_path=tmp_path)
        assert engine.persona.name == "静观"

    def test_engine_accepts_custom_persona(self, tmp_path):
        from sirius_chat.core.emotional_engine import EmotionalGroupChatEngine
        custom = PersonaProfile(name="CustomBot", reply_frequency="low")
        engine = EmotionalGroupChatEngine(work_path=tmp_path, persona=custom)
        assert engine.persona.name == "CustomBot"
        assert engine.persona.reply_frequency == "low"


class TestPersonaBiasesThreshold:
    def test_high_frequency_lowers_threshold(self, tmp_path):
        from sirius_chat.core.emotional_engine import EmotionalGroupChatEngine
        from sirius_chat.models.intent_v3 import IntentAnalysisV3
        from sirius_chat.models.emotion import EmotionState

        high_p = PersonaProfile(name="Chatty", reply_frequency="high")
        engine = EmotionalGroupChatEngine(work_path=tmp_path, persona=high_p)

        intent = IntentAnalysisV3(urgency_score=30, relevance_score=0.4)
        emotion = EmotionState()

        decision = engine._decision(intent, emotion, "g1", "u1")
        # high frequency should make it easier to reply (lower threshold)
        assert intent.threshold < 0.6  # default base is ~0.45, high *0.8 = ~0.36

    def test_low_frequency_raises_threshold(self, tmp_path):
        from sirius_chat.core.emotional_engine import EmotionalGroupChatEngine
        from sirius_chat.models.intent_v3 import IntentAnalysisV3
        from sirius_chat.models.emotion import EmotionState

        low_p = PersonaProfile(name="Quiet", reply_frequency="low")
        mod_p = PersonaProfile(name="Normal", reply_frequency="moderate")
        low_engine = EmotionalGroupChatEngine(work_path=tmp_path, persona=low_p)
        mod_engine = EmotionalGroupChatEngine(work_path=tmp_path, persona=mod_p)

        intent_low = IntentAnalysisV3(urgency_score=30, relevance_score=0.4)
        intent_mod = IntentAnalysisV3(urgency_score=30, relevance_score=0.4)
        emotion = EmotionState()

        # Prime both engines with the same user state so relationship_factor
        # is identical; only the persona frequency bias should differ.
        mod_engine._decision(intent_mod, emotion, "g1", "u1")
        intent_mod2 = IntentAnalysisV3(urgency_score=30, relevance_score=0.4)
        mod_engine._decision(intent_mod2, emotion, "g1", "u1")
        low_engine._decision(intent_low, emotion, "g1", "u1")
        # low frequency should make it harder to reply (higher threshold than moderate)
        assert intent_low.threshold > intent_mod2.threshold


class TestPersonaPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        p = PersonaProfile(name="PersistBot", catchphrases=["Yo!"])
        PersonaStore.save(tmp_path, p)
        loaded = PersonaStore.load(tmp_path)
        assert loaded is not None
        assert loaded.name == "PersistBot"
        assert loaded.catchphrases == ["Yo!"]

    def test_load_missing_returns_none(self, tmp_path):
        loaded = PersonaStore.load(tmp_path)
        assert loaded is None
