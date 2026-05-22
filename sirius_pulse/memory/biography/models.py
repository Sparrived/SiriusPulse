"""人物传记数据模型 — 全局跨群人物认知锚点。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RelationshipAnchor:
    """人物关系锚点 — 记录此人与其他人的关系。"""

    target_name: str = ""
    target_user_id: str = ""
    relation: str = ""
    fact_hint: str = ""
    mentioned_count: int = 1
    last_mentioned_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_name": self.target_name,
            "target_user_id": self.target_user_id,
            "relation": self.relation,
            "fact_hint": self.fact_hint,
            "mentioned_count": self.mentioned_count,
            "last_mentioned_at": self.last_mentioned_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RelationshipAnchor:
        return cls(
            target_name=data.get("target_name", ""),
            target_user_id=data.get("target_user_id", ""),
            relation=data.get("relation", ""),
            fact_hint=data.get("fact_hint", ""),
            mentioned_count=data.get("mentioned_count", 1),
            last_mentioned_at=data.get("last_mentioned_at", ""),
        )


@dataclass(slots=True)
class AliasEntry:
    """别名条目 — 一对多结构，支持同名消歧。

    置信度由 mentioned_count + source 通过 compute_confidence 计算，
    再经时间衰减（apply_time_decay）得到最终值。
    """

    user_id: str = ""
    user_name: str = ""
    weight: float = 1.0
    groups: list[str] = field(default_factory=list)
    mentioned_count: int = 1
    confidence: float = -1.0
    first_seen_at: str = ""
    last_seen_at: str = ""
    source: str = "napcat"

    @staticmethod
    def compute_confidence(mentioned_count: int, source: str = "llm_discovery") -> float:
        """根据提及次数和来源计算基础置信度（对数增长）。

        - napcat（适配器直接注册）：首次 0.50，稳定较快
        - llm_discovery（蒸馏发现）：首次 0.30，需要更多验证
        """
        if mentioned_count <= 0:
            return 0.0
        initial = 0.50 if source == "napcat" else 0.30
        base = min(0.95, initial + 0.20 * math.log2(mentioned_count))
        return round(base, 4)

    @staticmethod
    def apply_time_decay(confidence: float, days_since_last_seen: float) -> float:
        """时间衰减：每过去一天，置信度衰减 5%（保留最近活跃条目的优势）。"""
        if days_since_last_seen <= 0:
            return confidence
        return round(max(0.0, confidence * (0.95 ** days_since_last_seen)), 4)

    DECAY_THRESHOLD: float = 0.10

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "user_name": self.user_name,
            "weight": self.weight,
            "groups": list(self.groups),
            "mentioned_count": self.mentioned_count,
            "confidence": self.confidence,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AliasEntry:
        entry = cls(
            user_id=data.get("user_id", ""),
            user_name=data.get("user_name", ""),
            weight=float(data.get("weight", 1.0)),
            groups=list(data.get("groups", [])),
            mentioned_count=max(1, int(data.get("mentioned_count", 1))),
            confidence=float(data.get("confidence", -1.0)),
            first_seen_at=data.get("first_seen_at", ""),
            last_seen_at=data.get("last_seen_at", ""),
            source=data.get("source", "napcat"),
        )
        # 向后兼容：旧数据没有 confidence 字段，从 mentioned_count 计算
        if entry.confidence <= 0.0:
            entry.confidence = cls.compute_confidence(entry.mentioned_count, entry.source)
        return entry


@dataclass
class UserPersonaCard:
    """用户传记卡 — 全局唯一，跨群收敛。不追加，只重写。

    每人一张卡。不同群的观察都累积到同一张卡中，
    由 LLM 在 token 预算内合并重写。
    """

    user_id: str = ""
    name: str = ""

    # ── 注入层 ──
    aliases: list[str] = field(default_factory=list)
    identity_anchors: list[str] = field(default_factory=list)
    relationships: list[RelationshipAnchor] = field(default_factory=list)
    short_bio: str = ""

    # ── 层1：原始消息攒批（等待蒸馏）──
    pending_messages: list[str] = field(default_factory=list)
    pending_message_count: int = 0

    # ── 层2：蒸馏后的要点（等待传记更新）──
    distilled_points: list[str] = field(default_factory=list)
    last_distill_at: str = ""

    # ── 内部追踪 ──
    last_updated_at: str = ""
    bio_token_estimate: int = 0
    bio_token_budget: int = 500

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "aliases": list(self.aliases),
            "identity_anchors": list(self.identity_anchors),
            "relationships": [r.to_dict() for r in self.relationships],
            "short_bio": self.short_bio,
            "pending_messages": list(self.pending_messages),
            "pending_message_count": self.pending_message_count,
            "distilled_points": list(self.distilled_points),
            "last_distill_at": self.last_distill_at,
            "last_updated_at": self.last_updated_at,
            "bio_token_estimate": self.bio_token_estimate,
            "bio_token_budget": self.bio_token_budget,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserPersonaCard:
        rels = []
        for r in data.get("relationships", []):
            if isinstance(r, dict):
                rels.append(RelationshipAnchor.from_dict(r))
            elif isinstance(r, RelationshipAnchor):
                rels.append(r)
        return cls(
            user_id=data.get("user_id", ""),
            name=data.get("name", ""),
            aliases=list(data.get("aliases", [])),
            identity_anchors=list(data.get("identity_anchors", [])),
            relationships=rels,
            short_bio=data.get("short_bio", ""),
            pending_messages=list(data.get("pending_messages", [])),
            pending_message_count=int(data.get("pending_message_count", 0)),
            distilled_points=list(data.get("distilled_points", [])),
            last_distill_at=data.get("last_distill_at", ""),
            last_updated_at=data.get("last_updated_at", ""),
            bio_token_estimate=int(data.get("bio_token_estimate", 0)),
            bio_token_budget=int(data.get("bio_token_budget", 500)),
        )


__all__ = [
    "UserPersonaCard",
    "RelationshipAnchor",
    "AliasEntry",
]
