"""Evidence-first memory ledger models.

The provenance layer stores immutable evidence snapshots, extraction runs, and
typed claims. User profiles are projections over active claims, not the source
of truth.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def content_hash(text: str) -> str:
    """Return a stable short hash for evidence content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class ClaimStatus(str, Enum):
    """Lifecycle state for a memory claim."""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    SHADOW = "shadow"
    STALE = "stale"


class ClaimAttribution(str, Enum):
    """How strongly the claim is attributed to the subject."""

    SELF_STATED = "self_stated"
    SECOND_PERSON_CONFIRMED = "second_person_confirmed"
    THIRD_PARTY_CLAIM = "third_party_claim"
    INFERRED = "inferred"
    MANUAL = "manual"
    MIGRATION = "migration"


class ClaimType(str, Enum):
    """Typed memory fact classes used by profile projections."""

    IDENTITY = "identity"
    PREFERENCE = "preference"
    HABIT = "habit"
    RELATIONSHIP = "relationship"
    LONG_STATE = "long_state"
    ALIAS = "alias"
    EVENT = "event"
    OTHER = "other"


@dataclass(slots=True)
class Evidence:
    """Immutable snapshot of the source that supports a claim."""

    evidence_id: str = field(default_factory=lambda: _short_id("ev"))
    source_type: str = "message"
    group_id: str = ""
    message_id: str = ""
    platform_message_id: str = ""
    speaker_user_id: str = ""
    speaker_name: str = ""
    content_quote: str = ""
    content_digest: str = ""
    created_at: str = field(default_factory=_now_iso)
    observed_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.content_digest:
            self.content_digest = content_hash(self.content_quote or self.message_id)
        if not self.observed_at:
            self.observed_at = self.created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_type": self.source_type,
            "group_id": self.group_id,
            "message_id": self.message_id,
            "platform_message_id": self.platform_message_id,
            "speaker_user_id": self.speaker_user_id,
            "speaker_name": self.speaker_name,
            "content_quote": self.content_quote,
            "content_digest": self.content_digest,
            "created_at": self.created_at,
            "observed_at": self.observed_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        return cls(
            evidence_id=data.get("evidence_id", _short_id("ev")),
            source_type=data.get("source_type", "message"),
            group_id=data.get("group_id", ""),
            message_id=data.get("message_id", ""),
            platform_message_id=data.get("platform_message_id", ""),
            speaker_user_id=data.get("speaker_user_id", ""),
            speaker_name=data.get("speaker_name", ""),
            content_quote=data.get("content_quote", ""),
            content_digest=data.get("content_digest", ""),
            created_at=data.get("created_at", _now_iso()),
            observed_at=data.get("observed_at", ""),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(slots=True)
class ExtractionRun:
    """A concrete extraction or migration run that produced claims."""

    run_id: str = field(default_factory=lambda: _short_id("run"))
    task: str = ""
    model: str = ""
    prompt_version: str = ""
    input_evidence_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "input_evidence_ids": list(self.input_evidence_ids),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractionRun":
        return cls(
            run_id=data.get("run_id", _short_id("run")),
            task=data.get("task", ""),
            model=data.get("model", ""),
            prompt_version=data.get("prompt_version", ""),
            input_evidence_ids=list(data.get("input_evidence_ids", [])),
            created_at=data.get("created_at", _now_iso()),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class MemoryClaim:
    """A typed candidate fact with provenance and lifecycle metadata."""

    claim_id: str = field(default_factory=lambda: _short_id("cl"))
    subject_user_id: str = ""
    subject_label: str = ""
    fact_type: str = ClaimType.OTHER
    value: str = ""
    predicate: str = ""
    object_value: str = ""
    status: str = ClaimStatus.CANDIDATE
    attribution: str = ClaimAttribution.INFERRED
    confidence: float = 0.5
    evidence_ids: list[str] = field(default_factory=list)
    extraction_run_id: str = ""
    source: str = ""
    source_record_id: str = ""
    source_situation_id: str = ""
    source_group_id: str = ""
    observed_at: str = field(default_factory=_now_iso)
    expires_at: str = ""
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    corrections: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "subject_user_id": self.subject_user_id,
            "subject_label": self.subject_label,
            "fact_type": self.fact_type,
            "value": self.value,
            "predicate": self.predicate,
            "object_value": self.object_value,
            "status": self.status,
            "attribution": self.attribution,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
            "extraction_run_id": self.extraction_run_id,
            "source": self.source,
            "source_record_id": self.source_record_id,
            "source_situation_id": self.source_situation_id,
            "source_group_id": self.source_group_id,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "supersedes": list(self.supersedes),
            "superseded_by": self.superseded_by,
            "corrections": list(self.corrections),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryClaim":
        return cls(
            claim_id=data.get("claim_id", _short_id("cl")),
            subject_user_id=data.get("subject_user_id", ""),
            subject_label=data.get("subject_label", ""),
            fact_type=data.get("fact_type", ClaimType.OTHER),
            value=data.get("value", ""),
            predicate=data.get("predicate", ""),
            object_value=data.get("object_value", ""),
            status=data.get("status", ClaimStatus.CANDIDATE),
            attribution=data.get("attribution", ClaimAttribution.INFERRED),
            confidence=float(data.get("confidence", 0.5)),
            evidence_ids=list(data.get("evidence_ids", [])),
            extraction_run_id=data.get("extraction_run_id", ""),
            source=data.get("source", ""),
            source_record_id=data.get("source_record_id", ""),
            source_situation_id=data.get("source_situation_id", ""),
            source_group_id=data.get("source_group_id", ""),
            observed_at=data.get("observed_at", _now_iso()),
            expires_at=data.get("expires_at", ""),
            supersedes=list(data.get("supersedes", [])),
            superseded_by=data.get("superseded_by"),
            corrections=list(data.get("corrections", [])),
            metadata=dict(data.get("metadata", {})),
        )

    @property
    def is_active(self) -> bool:
        return self.status == ClaimStatus.ACTIVE

    @property
    def profile_safe(self) -> bool:
        return self.status == ClaimStatus.ACTIVE and self.attribution in {
            ClaimAttribution.SELF_STATED,
            ClaimAttribution.SECOND_PERSON_CONFIRMED,
            ClaimAttribution.MANUAL,
            ClaimAttribution.MIGRATION,
        }


def normalize_claim_value(predicate: str, obj: str) -> str:
    """Render a compact human-readable claim value."""
    predicate = predicate.strip()
    obj = obj.strip()
    if predicate and obj:
        return f"{predicate}{obj}"
    return predicate or obj
