from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

_MAX_PENDING_RECORDS = 20


@dataclass
class ResponseRecord:
    """AI 发言的反馈追踪锚点。

    每次 AI 发送消息后创建一条记录，等待用户反馈（跟进回应）。
    只有 directed_score 达标的后续消息才算 engaged，避免群聊噪音。
    """

    sent_at: str = ""
    target_user_id: str = ""
    topic_hint: str = ""
    response_length: int = 0
    was_engaged: bool = False
    engagement_latency_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sent_at": self.sent_at,
            "target_user_id": self.target_user_id,
            "topic_hint": self.topic_hint,
            "response_length": self.response_length,
            "was_engaged": self.was_engaged,
            "engagement_latency_s": self.engagement_latency_s,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResponseRecord:
        return cls(
            sent_at=data.get("sent_at", ""),
            target_user_id=data.get("target_user_id", ""),
            topic_hint=data.get("topic_hint", ""),
            response_length=data.get("response_length", 0),
            was_engaged=data.get("was_engaged", False),
            engagement_latency_s=data.get("engagement_latency_s", 0.0),
        )


@dataclass
class AtmosphereSnapshot:
    timestamp: str = ""
    group_valence: float = 0.0
    group_arousal: float = 0.0
    active_participants: int = 0


@dataclass
class GroupSemanticProfile:
    group_id: str = ""
    group_name: str = ""
    interest_topics: list[str] = field(default_factory=list)
    atmosphere_history: list[Any] = field(default_factory=list)
    group_norms: dict[str, Any] = field(default_factory=dict)
    taboo_topics: list[str] = field(default_factory=list)
    dominant_topic: str = ""
    pending_ai_responses: list[ResponseRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "group_name": self.group_name,
            "interest_topics": list(self.interest_topics),
            "atmosphere_history": [
                {
                    "timestamp": s.timestamp,
                    "group_valence": s.group_valence,
                    "group_arousal": s.group_arousal,
                    "active_participants": s.active_participants,
                }
                for s in self.atmosphere_history
                if isinstance(s, AtmosphereSnapshot)
            ],
            "group_norms": dict(self.group_norms),
            "taboo_topics": list(self.taboo_topics),
            "dominant_topic": self.dominant_topic,
            "pending_ai_responses": [
                r.to_dict() for r in self.pending_ai_responses
                if isinstance(r, ResponseRecord)
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GroupSemanticProfile:
        raw_history = data.get("atmosphere_history", [])
        history: list[AtmosphereSnapshot] = []
        for item in raw_history:
            if isinstance(item, dict):
                history.append(AtmosphereSnapshot(**item))
            elif isinstance(item, AtmosphereSnapshot):
                history.append(item)
        raw_pending = data.get("pending_ai_responses", [])
        pending: list[ResponseRecord] = []
        for item in raw_pending:
            if isinstance(item, dict):
                pending.append(ResponseRecord.from_dict(item))
            elif isinstance(item, ResponseRecord):
                pending.append(item)
        return cls(
            group_id=data.get("group_id", ""),
            group_name=data.get("group_name", ""),
            interest_topics=list(data.get("interest_topics", [])),
            atmosphere_history=history,
            group_norms=dict(data.get("group_norms", {})),
            taboo_topics=list(data.get("taboo_topics", [])),
            dominant_topic=data.get("dominant_topic", ""),
            pending_ai_responses=pending,
        )


@dataclass
class UserSemanticProfile:
    user_id: str = ""
    name: str = ""

    # 反馈驱动的核心指标
    engagement_rate: float = 0.0
    interaction_count: int = 0
    first_interaction_at: str = ""
    last_interaction_at: str = ""
    pending_responses: list[ResponseRecord] = field(default_factory=list)

    def compute_familiarity(self) -> float:
        """基于真实交互次数的熟悉度（对数曲线，50次≈0.96）。"""
        return round(min(1.0, math.log1p(self.interaction_count) / math.log1p(50)), 4)

    def record_interaction(self, timestamp: str) -> None:
        self.interaction_count += 1
        self.last_interaction_at = timestamp
        if not self.first_interaction_at:
            self.first_interaction_at = timestamp

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "engagement_rate": self.engagement_rate,
            "interaction_count": self.interaction_count,
            "first_interaction_at": self.first_interaction_at,
            "last_interaction_at": self.last_interaction_at,
            "pending_responses": [
                r.to_dict() for r in self.pending_responses
                if isinstance(r, ResponseRecord)
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserSemanticProfile:
        raw_pending = data.get("pending_responses", [])
        pending: list[ResponseRecord] = []
        for item in raw_pending:
            if isinstance(item, dict):
                pending.append(ResponseRecord.from_dict(item))
            elif isinstance(item, ResponseRecord):
                pending.append(item)
        return cls(
            user_id=data.get("user_id", ""),
            name=data.get("name", ""),
            engagement_rate=data.get("engagement_rate", 0.0),
            interaction_count=data.get("interaction_count", 0),
            first_interaction_at=data.get("first_interaction_at", ""),
            last_interaction_at=data.get("last_interaction_at", ""),
            pending_responses=pending,
        )


__all__ = [
    "ResponseRecord",
    "GroupSemanticProfile",
    "UserSemanticProfile",
    "AtmosphereSnapshot",
]
