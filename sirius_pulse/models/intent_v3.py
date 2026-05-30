"""Intent analysis models v3: purpose-driven classification aligned with paper."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SocialIntent(Enum):
    """Purpose-driven intent taxonomy"""

    HELP_SEEKING = "help_seeking"
    EMOTIONAL = "emotional"
    SOCIAL = "social"
    SILENT = "silent"
    PLUGIN_COMMAND = "plugin_command"  # Plugin 命令意图（v1.2+）


class HelpSubtype(Enum):
    TECH_HELP = "tech_help"
    INFO_QUERY = "info_query"
    DECISION_HELP = "decision_help"


class EmotionalSubtype(Enum):
    VENTING = "venting"
    SEEKING_EMPATHY = "seeking_empathy"
    COMPANIONSHIP = "companionship"
    CELEBRATION = "celebration"


class SocialSubtype(Enum):
    TOPIC_DISCUSSION = "topic_discussion"
    RELATIONSHIP_MAINTENANCE = "relationship_maintenance"
    HUMOR = "humor"


class SilentSubtype(Enum):
    PRIVATE_CHAT = "private_chat"
    FILLER = "filler"
    IRRELEVANT = "irrelevant"


INTENT_SUBTYPE_MAP: dict[SocialIntent, type[Enum]] = {
    SocialIntent.HELP_SEEKING: HelpSubtype,
    SocialIntent.EMOTIONAL: EmotionalSubtype,
    SocialIntent.SOCIAL: SocialSubtype,
    SocialIntent.SILENT: SilentSubtype,
}


@dataclass(slots=True)
class IntentAnalysisV3:
    """Extended intent analysis result compatible with v2 + v3 fields."""

    # === v2 compatible fields ===
    intent_type: str = "chat"
    target: str = "unknown"
    target_scope: str = "unknown"
    directed_at_current_ai: bool = False
    importance: float = 0.5

    # === v3 purpose-driven fields ===
    social_intent: SocialIntent = field(default_factory=lambda: SocialIntent.SOCIAL)
    intent_subtype: str = ""
    urgency_score: float = 0.0  # 0-100
    relevance_score: float = 0.5  # 0-1
    confidence: float = 0.8
    response_priority: int = 5  # 1-10
    estimated_response_time: float = 0.0  # seconds; 0 = immediate
    search_query: str = ""  # LLM-generated query for memory retrieval

    # === multi-factor decision support ===
    activity_factor: float = 1.0
    engagement_factor: float = 1.0
    time_factor: float = 1.0
    threshold: float = 0.5

    # === directedness multi-dimensional scoring (0.0 ~ 1.0) ===
    # Layer 1: Structural (platform metadata)
    mention_score: float = 0.0        # @mention exact match
    reference_score: float = 0.0      # reply_to / quote reference
    at_all_score: float = 0.0         # @all / @everyone

    # Layer 2: Linguistic (surface text features)
    name_match_score: float = 0.0     # nickname/name match
    second_person_score: float = 0.0  # density of "你/您"
    question_score: float = 0.0       # interrogative patterns
    imperative_score: float = 0.0     # imperative/request patterns

    # Layer 3: Semantic (content understanding)
    topic_relevance_score: float = 0.0      # topic vs AI persona overlap
    emotional_disclosure_score: float = 0.0 # emotional expression seeking support
    attention_seeking_score: float = 0.0    # attention-seeking phrases

    # Layer 4: Contextual (conversation dynamics)
    recency_score: float = 0.0        # recent interaction with AI
    turn_taking_score: float = 0.0    # turn-alternation pattern

    # === synthesized directed score ===
    directed_score: float = 0.0  # 0.0 ~ 1.0, synthesized from 12-dim + LLM

    # === social signal decoding ===
    sarcasm_score: float = 0.0  # 0.0 ~ 1.0, irony / sarcasm detection
    entitlement_score: float = 0.5  # 0.0 ~ 1.0, how qualified AI is to reply

    # === image understanding ===
    image_caption: str = ""  # 图片描述文本，由多模态意图分析生成
    sticker_caption: str = ""  # 动画表情描述文本（缓存命中时使用）

    # === Plugin 意图识别字段（v1.2+）===
    plugin_intent: str | None = None       # 匹配到的插件意图ID，如 "weather"
    plugin_confidence: float = 0.0         # 插件匹配置信度
    plugin_slots: dict[str, Any] = field(default_factory=dict)  # 提取的参数槽位
    plugin_render_mode: str = "direct"     # direct | llm | silent

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_type": self.intent_type,
            "target": self.target,
            "target_scope": self.target_scope,
            "directed_at_current_ai": self.directed_at_current_ai,
            "importance": self.importance,
            "social_intent": self.social_intent.value,
            "intent_subtype": self.intent_subtype,
            "urgency_score": self.urgency_score,
            "relevance_score": self.relevance_score,
            "confidence": self.confidence,
            "response_priority": self.response_priority,
            "estimated_response_time": self.estimated_response_time,
            "search_query": self.search_query,
            "activity_factor": self.activity_factor,
            "engagement_factor": self.engagement_factor,
            "time_factor": self.time_factor,
            "threshold": self.threshold,
            # directedness dimensions
            "mention_score": self.mention_score,
            "reference_score": self.reference_score,
            "at_all_score": self.at_all_score,
            "name_match_score": self.name_match_score,
            "second_person_score": self.second_person_score,
            "question_score": self.question_score,
            "imperative_score": self.imperative_score,
            "topic_relevance_score": self.topic_relevance_score,
            "emotional_disclosure_score": self.emotional_disclosure_score,
            "attention_seeking_score": self.attention_seeking_score,
            "recency_score": self.recency_score,
            "turn_taking_score": self.turn_taking_score,
            "directed_score": self.directed_score,
            "sarcasm_score": self.sarcasm_score,
            "entitlement_score": self.entitlement_score,
            "image_caption": self.image_caption,
            "sticker_caption": self.sticker_caption,
            # plugin fields
            "plugin_intent": self.plugin_intent,
            "plugin_confidence": self.plugin_confidence,
            "plugin_slots": self.plugin_slots,
            "plugin_render_mode": self.plugin_render_mode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IntentAnalysisV3":
        si_raw = data.get("social_intent", "social")
        try:
            social_intent = SocialIntent(si_raw)
        except ValueError:
            social_intent = SocialIntent.SOCIAL
        return cls(
            intent_type=data.get("intent_type", "chat"),
            target=data.get("target", "unknown"),
            target_scope=data.get("target_scope", "unknown"),
            directed_at_current_ai=data.get("directed_at_current_ai", False),
            importance=data.get("importance", 0.5),
            social_intent=social_intent,
            intent_subtype=data.get("intent_subtype", ""),
            urgency_score=data.get("urgency_score", 0.0),
            relevance_score=data.get("relevance_score", 0.5),
            confidence=data.get("confidence", 0.8),
            response_priority=data.get("response_priority", 5),
            estimated_response_time=data.get("estimated_response_time", 0.0),
            search_query=data.get("search_query", ""),
            activity_factor=data.get("activity_factor", 1.0),
            engagement_factor=data.get("engagement_factor", 1.0),
            time_factor=data.get("time_factor", 1.0),
            threshold=data.get("threshold", 0.5),
            # directedness dimensions
            mention_score=data.get("mention_score", 0.0),
            reference_score=data.get("reference_score", 0.0),
            at_all_score=data.get("at_all_score", 0.0),
            name_match_score=data.get("name_match_score", 0.0),
            second_person_score=data.get("second_person_score", 0.0),
            question_score=data.get("question_score", 0.0),
            imperative_score=data.get("imperative_score", 0.0),
            topic_relevance_score=data.get("topic_relevance_score", 0.0),
            emotional_disclosure_score=data.get("emotional_disclosure_score", 0.0),
            attention_seeking_score=data.get("attention_seeking_score", 0.0),
            recency_score=data.get("recency_score", 0.0),
            turn_taking_score=data.get("turn_taking_score", 0.0),
            directed_score=data.get("directed_score", 0.0),
            sarcasm_score=data.get("sarcasm_score", 0.0),
            entitlement_score=data.get("entitlement_score", 0.5),
            image_caption=data.get("image_caption", ""),
            sticker_caption=data.get("sticker_caption", ""),
            # plugin fields
            plugin_intent=data.get("plugin_intent"),
            plugin_confidence=data.get("plugin_confidence", 0.0),
            plugin_slots=data.get("plugin_slots", {}),
            plugin_render_mode=data.get("plugin_render_mode", "direct"),
        )
