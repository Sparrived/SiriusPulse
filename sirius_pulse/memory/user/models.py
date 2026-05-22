"""User memory data models"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sirius_pulse.developer_profiles import metadata_declares_developer
from sirius_pulse.mixins import JsonSerializable


@dataclass(slots=True)
class UserProfile(JsonSerializable):
    """Initial user profile: provided by external system before session starts.
    
    Should not be arbitrarily overwritten by AI during runtime.
    """

    user_id: str
    name: str
    persona: str = ""
    identities: dict[str, str] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    traits: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_developer(self) -> bool:
        return metadata_declares_developer(self.metadata)


@dataclass(slots=True)
class MemoryFact(JsonSerializable):
    """Traceable memory fact record. Supports multi-model collaboration and conflict detection.
    
    Rich context support:
    - observed_at: ISO 8601 timestamp for precise moment
    - observed_time_desc: Human-friendly time description (e.g. "昨天下午", "上周一")
    - context_channel: Where the information came from (e.g. "qq", "wechat", "cli")
    - context_topic: Conversation topic or domain (e.g. "work", "travel", "hobby")
    
    Confidence tiers (dynamic derivation via MemoryPolicy.transient_confidence_threshold):
    - confidence > threshold: High confidence (RESIDENT), persistent storage
    - confidence <= threshold: Low confidence (TRANSIENT), session-only
    """

    fact_type: str
    value: str
    source: str = "unknown"
    confidence: float = 0.5
    observed_at: str = ""
    observed_time_desc: str = ""  # Human-friendly time description
    memory_category: str = "custom"  # identity|preference|emotion|event|custom
    validated: bool = False  # Whether verified by memory_manager
    conflict_with: list[str] = field(default_factory=list)  # List of conflicting memory IDs
    # Rich context fields
    context_channel: str = ""  # Source channel (qq, wechat, cli, etc.)
    context_topic: str = ""  # Conversation topic or domain
    context_metadata: dict[str, str] = field(default_factory=dict)  # Additional context
    # Group isolation
    group_id: str = ""  # Group/chat identifier for memory isolation
    # Activation-based forgetting (paper §4.2.4)
    activation: float = 1.0  # Dynamic activation score (0~1)
    access_count: int = 0  # Retrieval access count for reinforcement
    last_accessed: str = ""  # ISO 8601 timestamp of last retrieval
    # Activity tracking
    mention_count: int = 0  # Number of times this fact has been mentioned/reinforced
    source_event_id: str = ""  # Link back to originating event (if any)

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    def is_transient(self, threshold: float = 0.85) -> bool:
        """Dynamically derive transient status from confidence."""
        return self.confidence <= threshold


@dataclass(slots=True)
class UserRuntimeState:
    """Runtime state: continuously updated by system/AI during session."""

    inferred_persona: str = ""
    inferred_aliases: list[str] = field(default_factory=list)
    inferred_traits: list[str] = field(default_factory=list)
    preference_tags: list[str] = field(default_factory=list)
    recent_messages: list[str] = field(default_factory=list)
    summary_notes: list[str] = field(default_factory=list)
    memory_facts: list[MemoryFact] = field(default_factory=list)
    last_seen_channel: str = ""
    last_seen_uid: str = ""
    # Event observation feature set (for consistency comparison with new events)
    observed_keywords: set[str] = field(default_factory=set)
    observed_roles: set[str] = field(default_factory=set)
    observed_emotions: set[str] = field(default_factory=set)
    observed_entities: set[str] = field(default_factory=set)
    # A1: Time window deduplication - record last event processing time
    last_event_processed_at: datetime | None = None


@dataclass(slots=True)
class UserMemoryEntry:
    """User memory entry combining profile and runtime state."""
    
    profile: UserProfile
    runtime: UserRuntimeState = field(default_factory=UserRuntimeState)

    @property
    def recent_messages(self) -> list[str]:
        """Backward-compatible alias for legacy callers."""
        return self.runtime.recent_messages

    @property
    def summary_notes(self) -> list[str]:
        """Backward-compatible alias for legacy callers."""
        return self.runtime.summary_notes
