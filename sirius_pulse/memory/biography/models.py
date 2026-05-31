"""传记视图数据模型。

UserBiography 从演化链 active 三元组自动派生，不存储独立数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class UserBiography:
    """用户传记：演化链的投影。

    不存储独立数据，所有信息从 EvolutionChain 的 active 三元组派生。
    当演化链中的三元组被 supersede 时，传记自动更新。
    """

    user_id: str = ""
    name: str = ""

    # ── 从演化链 active 三元组生成 ──
    identity_anchors: list[str] = field(default_factory=list)
    relationships: list[dict[str, str]] = field(default_factory=list)
    short_bio: str = ""

    # ── 从 UnifiedUser 同步 ──
    aliases: list[str] = field(default_factory=list)

    # ── 来源追溯 ──
    source_record_ids: list[str] = field(default_factory=list)

    # ── 统计 ──
    active_fact_count: int = 0
    superseded_fact_count: int = 0
    uncertain_fact_count: int = 0
    generated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "identity_anchors": list(self.identity_anchors),
            "relationships": list(self.relationships),
            "short_bio": self.short_bio,
            "aliases": list(self.aliases),
            "source_record_ids": list(self.source_record_ids),
            "active_fact_count": self.active_fact_count,
            "superseded_fact_count": self.superseded_fact_count,
            "uncertain_fact_count": self.uncertain_fact_count,
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserBiography:
        return cls(
            user_id=data.get("user_id", ""),
            name=data.get("name", ""),
            identity_anchors=list(data.get("identity_anchors", [])),
            relationships=list(data.get("relationships", [])),
            short_bio=data.get("short_bio", ""),
            aliases=list(data.get("aliases", [])),
            source_record_ids=list(data.get("source_record_ids", [])),
            active_fact_count=int(data.get("active_fact_count", 0)),
            superseded_fact_count=int(data.get("superseded_fact_count", 0)),
            uncertain_fact_count=int(data.get("uncertain_fact_count", 0)),
            generated_at=data.get("generated_at", _now_iso()),
        )
