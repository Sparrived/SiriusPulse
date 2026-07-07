from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

PROFILE_SECTIONS: tuple[str, ...] = (
    "aliases",
    "identity",
    "interests",
    "preferences",
    "communication_style",
    "relationship",
    "social_relations",
    "boundaries",
    "emotional_pattern",
    "notes",
)

_PROFILE_ITEM_STATUSES = {"active", "uncertain", "rejected", "stale"}
_PROFILE_OPERATIONS = {"upsert", "reject", "stale", "delete"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clamp_confidence(value: Any, default: float = 0.5) -> float:
    try:
        return round(max(0.0, min(1.0, float(value))), 4)
    except (TypeError, ValueError):
        return default


def normalize_key(value: str) -> str:
    return "_".join(str(value or "").strip().lower().split())[:80]


@dataclass(slots=True)
class ProfileItem:
    key: str
    value: str
    confidence: float = 0.5
    source: str = "model"
    evidence: str = ""
    evidence_message_ids: list[str] = field(default_factory=list)
    first_seen_at: str = field(default_factory=now_iso)
    last_seen_at: str = field(default_factory=now_iso)
    update_count: int = 1
    status: str = "active"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "confidence": clamp_confidence(self.confidence),
            "source": self.source,
            "evidence": self.evidence,
            "evidence_message_ids": list(self.evidence_message_ids),
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "update_count": self.update_count,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProfileItem":
        status = str(data.get("status") or "active")
        if status not in _PROFILE_ITEM_STATUSES:
            status = "active"
        return cls(
            key=normalize_key(str(data.get("key") or "")),
            value=str(data.get("value") or "")[:500],
            confidence=clamp_confidence(data.get("confidence", 0.5)),
            source=str(data.get("source") or "model")[:40],
            evidence=str(data.get("evidence") or "")[:500],
            evidence_message_ids=[str(x)[:120] for x in data.get("evidence_message_ids", []) if x],
            first_seen_at=str(data.get("first_seen_at") or now_iso()),
            last_seen_at=str(data.get("last_seen_at") or now_iso()),
            update_count=max(1, int(data.get("update_count") or 1)),
            status=status,
        )


@dataclass(slots=True)
class ProfileSection:
    summary: str = ""
    items: list[ProfileItem] = field(default_factory=list)

    def active_items(self) -> list[ProfileItem]:
        return [item for item in self.items if item.status == "active"]

    def find(self, key: str) -> ProfileItem | None:
        normalized = normalize_key(key)
        for item in self.items:
            if item.key == normalized:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProfileSection":
        return cls(
            summary=str(data.get("summary") or "")[:800],
            items=[ProfileItem.from_dict(x) for x in data.get("items", []) if isinstance(x, dict)],
        )


@dataclass(slots=True)
class ProfileUpdate:
    section: str
    key: str
    value: str = ""
    confidence: float = 0.5
    evidence: str = ""
    evidence_message_ids: list[str] = field(default_factory=list)
    operation: str = "upsert"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProfileUpdate":
        section = str(data.get("section") or "notes").strip().lower()
        if section not in PROFILE_SECTIONS:
            section = "notes"
        operation = str(data.get("operation") or "upsert").strip().lower()
        if operation not in _PROFILE_OPERATIONS:
            operation = "upsert"
        return cls(
            section=section,
            key=normalize_key(str(data.get("key") or "")),
            value=str(data.get("value") or "")[:500],
            confidence=clamp_confidence(data.get("confidence", 0.5)),
            evidence=str(data.get("evidence") or "")[:500],
            evidence_message_ids=[str(x)[:120] for x in data.get("evidence_message_ids", []) if x],
            operation=operation,
        )


@dataclass(slots=True)
class UserPersonaProfile:
    user_id: str
    group_id: str = ""
    display_name: str = ""
    short_impression: str = ""
    aliases: ProfileSection = field(default_factory=ProfileSection)
    identity: ProfileSection = field(default_factory=ProfileSection)
    interests: ProfileSection = field(default_factory=ProfileSection)
    preferences: ProfileSection = field(default_factory=ProfileSection)
    communication_style: ProfileSection = field(default_factory=ProfileSection)
    relationship: ProfileSection = field(default_factory=ProfileSection)
    social_relations: ProfileSection = field(default_factory=ProfileSection)
    boundaries: ProfileSection = field(default_factory=ProfileSection)
    emotional_pattern: ProfileSection = field(default_factory=ProfileSection)
    notes: ProfileSection = field(default_factory=ProfileSection)
    affinity_score: float = 0.0
    familiarity_score: float = 0.0
    last_updated_at: str = field(default_factory=now_iso)
    version: int = 1

    def section(self, name: str) -> ProfileSection:
        section_name = name if name in PROFILE_SECTIONS else "notes"
        return getattr(self, section_name)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "user_id": self.user_id,
            "group_id": self.group_id,
            "display_name": self.display_name,
            "short_impression": self.short_impression,
            "affinity_score": clamp_confidence(self.affinity_score, 0.0),
            "familiarity_score": clamp_confidence(self.familiarity_score, 0.0),
            "last_updated_at": self.last_updated_at,
            "version": self.version,
        }
        for section_name in PROFILE_SECTIONS:
            data[section_name] = self.section(section_name).to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserPersonaProfile":
        kwargs: dict[str, Any] = {
            "user_id": str(data.get("user_id") or ""),
            "group_id": str(data.get("group_id") or ""),
            "display_name": str(data.get("display_name") or "")[:120],
            "short_impression": str(data.get("short_impression") or "")[:800],
            "affinity_score": clamp_confidence(data.get("affinity_score", 0.0), 0.0),
            "familiarity_score": clamp_confidence(data.get("familiarity_score", 0.0), 0.0),
            "last_updated_at": str(data.get("last_updated_at") or now_iso()),
            "version": max(1, int(data.get("version") or 1)),
        }
        for section_name in PROFILE_SECTIONS:
            raw = data.get(section_name, {})
            kwargs[section_name] = ProfileSection.from_dict(raw if isinstance(raw, dict) else {})
        return cls(**kwargs)
