"""统一用户数据模型。

包含：
- 基础身份信息
- 传记画像信息
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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


@dataclass
class UnifiedUser:
    """统一用户模型。

    包含基础身份、传记画像等用户相关信息。
    """

    # ── 基础身份 ──
    user_id: str = ""
    name: str = ""
    persona: str = ""
    identities: dict[str, str] = field(default_factory=dict)  # platform → uid
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
            traits=list(data.get("traits", [])),
            group_memberships=dict(data.get("group_memberships", {})),
            metadata=dict(data.get("metadata", {})),
            identity_anchors=list(data.get("identity_anchors", [])),
            relationships=rels,
        )


__all__ = [
    "UnifiedUser",
    "RelationshipAnchor",
]
