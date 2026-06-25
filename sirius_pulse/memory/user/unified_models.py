"""统一用户数据模型。

包含：
- 基础身份信息
- 传记画像信息
- 别名索引支持
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, ClassVar

from sirius_pulse.developer_profiles import metadata_declares_developer


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
    """别名条目 — 一个别名只能指向一个用户。

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
    def compute_confidence(mentioned_count: int, source: str = "model_skill") -> float:
        """根据提及次数和来源计算基础置信度（对数增长）。"""
        if mentioned_count <= 0:
            return 0.0
        initial = 0.50 if source == "napcat" else 0.30
        base = min(0.95, initial + 0.20 * math.log2(mentioned_count))
        return round(base, 4)

    @staticmethod
    def apply_time_decay(confidence: float, days_since_last_seen: float) -> float:
        """时间衰减：每过去一天，置信度衰减 5%。"""
        if days_since_last_seen <= 0:
            return confidence
        return round(max(0.0, confidence * (0.95**days_since_last_seen)), 4)

    DECAY_THRESHOLD: ClassVar[float] = 0.10

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
        if entry.confidence <= 0.0:
            entry.confidence = cls.compute_confidence(entry.mentioned_count, entry.source)
        return entry


@dataclass
class UnifiedUser:
    """统一用户模型。

    包含基础身份、传记画像、别名索引等所有用户相关信息。
    """

    # ── 基础身份 ──
    user_id: str = ""
    name: str = ""
    persona: str = ""
    identities: dict[str, str] = field(default_factory=dict)  # platform → uid
    aliases: list[str] = field(default_factory=list)
    traits: list[str] = field(default_factory=list)
    group_memberships: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    # ── 传记画像 ──
    identity_anchors: list[str] = field(default_factory=list)
    relationships: list[RelationshipAnchor] = field(default_factory=list)

    @property
    def is_developer(self) -> bool:
        return metadata_declares_developer(self.metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "persona": self.persona,
            "identities": dict(self.identities),
            "aliases": list(self.aliases),
            "traits": list(self.traits),
            "group_memberships": dict(self.group_memberships),
            "metadata": dict(self.metadata),
            "identity_anchors": list(self.identity_anchors),
            "relationships": [r.to_dict() for r in self.relationships],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UnifiedUser:
        rels = []
        for r in data.get("relationships", []):
            if isinstance(r, dict):
                rels.append(RelationshipAnchor.from_dict(r))
            elif isinstance(r, RelationshipAnchor):
                rels.append(r)
        return cls(
            user_id=data.get("user_id", ""),
            name=data.get("name", ""),
            persona=data.get("persona", ""),
            identities=dict(data.get("identities", {})),
            aliases=list(data.get("aliases", [])),
            traits=list(data.get("traits", [])),
            group_memberships=dict(data.get("group_memberships", {})),
            metadata=dict(data.get("metadata", {})),
            identity_anchors=list(data.get("identity_anchors", [])),
            relationships=rels,
        )


__all__ = [
    "UnifiedUser",
    "RelationshipAnchor",
    "AliasEntry",
]
