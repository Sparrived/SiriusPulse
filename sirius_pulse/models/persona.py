"""Persona data models: rich character profiles for EmotionalGroupChatEngine.

A persona is the "soul" that shapes perception, cognition, decision, and execution
throughout the emotional engine pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PersonaProfile:
    """Rich character profile influencing the entire emotional engine pipeline."""

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    name: str = "小星"
    aliases: list[str] = field(default_factory=list)
    persona_summary: str = ""
    full_system_prompt: str = ""

    # ------------------------------------------------------------------
    # Personality (deep character)
    # ------------------------------------------------------------------
    personality_traits: list[str] = field(default_factory=list)
    backstory: str = ""
    core_values: list[str] = field(default_factory=list)
    flaws: list[str] = field(default_factory=list)
    motivations: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Expression style
    # ------------------------------------------------------------------
    communication_style: str = ""  # concise/detailed/formal/casual/humorous/...
    speech_rhythm: str = ""  # description of speaking pace/patterns
    emoji_preference: str = ""  # heavy/moderate/light/none
    humor_style: str = ""  # sarcastic/wholesome/dark/dry/witty/none
    typical_greetings: list[str] = field(default_factory=list)
    typical_signoffs: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Emotional baseline
    # ------------------------------------------------------------------
    emotional_baseline: dict[str, float] = field(
        default_factory=lambda: {"valence": 0.2, "arousal": 0.3}
    )
    emotional_range: dict[str, float] = field(
        default_factory=lambda: {"min_valence": -0.5, "max_valence": 0.8}
    )
    empathy_style: str = ""  # warm/practical/distant/playful/mentor
    stress_response: str = ""  # how they react under pressure

    # ------------------------------------------------------------------
    # Behavior boundaries
    # ------------------------------------------------------------------
    boundaries: list[str] = field(default_factory=list)
    taboo_topics: list[str] = field(default_factory=list)
    preferred_topics: list[str] = field(default_factory=list)
    social_role: str = ""  # observer/mediator/leader/jester/caregiver

    # ------------------------------------------------------------------
    # Runtime preferences
    # ------------------------------------------------------------------
    max_tokens_preference: int = 128
    temperature_preference: float = 0.7
    reply_frequency: str = "moderate"  # high/moderate/low/selective

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    version: str = "1.0"
    created_at: str = ""
    source: str = "template"  # template/keyword/interview/manual/roleplay_bridge

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "persona_summary": self.persona_summary,
            "full_system_prompt": self.full_system_prompt,
            "personality_traits": list(self.personality_traits),
            "backstory": self.backstory,
            "core_values": list(self.core_values),
            "flaws": list(self.flaws),
            "motivations": list(self.motivations),
            "communication_style": self.communication_style,
            "speech_rhythm": self.speech_rhythm,
            "emoji_preference": self.emoji_preference,
            "humor_style": self.humor_style,
            "typical_greetings": list(self.typical_greetings),
            "typical_signoffs": list(self.typical_signoffs),
            "emotional_baseline": dict(self.emotional_baseline),
            "emotional_range": dict(self.emotional_range),
            "empathy_style": self.empathy_style,
            "stress_response": self.stress_response,
            "boundaries": list(self.boundaries),
            "taboo_topics": list(self.taboo_topics),
            "preferred_topics": list(self.preferred_topics),
            "social_role": self.social_role,
            "max_tokens_preference": self.max_tokens_preference,
            "temperature_preference": self.temperature_preference,
            "reply_frequency": self.reply_frequency,
            "version": self.version,
            "created_at": self.created_at,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonaProfile":
        return cls(
            name=data.get("name", "小星"),
            aliases=list(data.get("aliases", [])),
            persona_summary=data.get("persona_summary", ""),
            full_system_prompt=data.get("full_system_prompt", ""),
            personality_traits=list(data.get("personality_traits", [])),
            backstory=data.get("backstory", ""),
            core_values=list(data.get("core_values", [])),
            flaws=list(data.get("flaws", [])),
            motivations=list(data.get("motivations", [])),
            communication_style=data.get("communication_style", ""),
            speech_rhythm=data.get("speech_rhythm", ""),
            emoji_preference=data.get("emoji_preference", ""),
            humor_style=data.get("humor_style", ""),
            typical_greetings=list(data.get("typical_greetings", [])),
            typical_signoffs=list(data.get("typical_signoffs", [])),
            emotional_baseline=dict(
                data.get("emotional_baseline", {"valence": 0.2, "arousal": 0.3})
            ),
            emotional_range=dict(
                data.get("emotional_range", {"min_valence": -0.5, "max_valence": 0.8})
            ),
            empathy_style=data.get("empathy_style", ""),
            stress_response=data.get("stress_response", ""),
            boundaries=list(data.get("boundaries", [])),
            taboo_topics=list(data.get("taboo_topics", [])),
            preferred_topics=list(data.get("preferred_topics", [])),
            social_role=data.get("social_role", ""),
            max_tokens_preference=int(data.get("max_tokens_preference", 128)),
            temperature_preference=float(data.get("temperature_preference", 0.7)),
            reply_frequency=data.get("reply_frequency", "moderate"),
            version=data.get("version", "1.0"),
            created_at=data.get("created_at", ""),
            source=data.get("source", "template"),
        )

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    def build_system_prompt(self) -> str:
        """构建发送给 LLM 的角色 prompt。委托 PromptFactory。"""
        from sirius_pulse.core.prompt_factory import PromptFactory

        return PromptFactory.build_persona_prompt(
            name=self.name,
            aliases=self.aliases,
            persona_summary=self.persona_summary,
            backstory=self.backstory,
            personality_traits=self.personality_traits,
            core_values=self.core_values,
            flaws=self.flaws,
            emotional_baseline=self.emotional_baseline,
            stress_response=self.stress_response,
            empathy_style=self.empathy_style,
            social_role=self.social_role,
            boundaries=self.boundaries,
            communication_style=self.communication_style,
            speech_rhythm=self.speech_rhythm,
            humor_style=self.humor_style,
            reply_frequency=self.reply_frequency,
            taboo_topics=self.taboo_topics,
            preferred_topics=self.preferred_topics,
            full_system_prompt=self.full_system_prompt,
        )
