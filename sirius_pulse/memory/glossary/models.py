"""Glossary data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sirius_pulse.mixins import JsonSerializable


@dataclass(slots=True)
class GlossaryTerm(JsonSerializable):
    """A term/noun definition learned by the AI from conversations.

    Attributes:
        term: The word or phrase.
        definition: Current best definition.
        source: How the term was learned (conversation | user_explained | inferred).
        first_seen_at: ISO 8601 timestamp of first encounter.
        last_updated_at: ISO 8601 timestamp of last update.
        confidence: How confident the AI is in the definition.
        usage_count: How many times the term appeared in conversations.
        context_examples: Short example sentences showing usage.
        related_terms: Links to related glossary terms.
        domain: Subject area (tech | daily | culture | game | custom).
    """

    term: str = ""
    definition: str = ""
    source: str = "inferred"
    first_seen_at: str = ""
    last_updated_at: str = ""
    confidence: float = 0.5
    usage_count: int = 1
    context_examples: list[str] = field(default_factory=list)
    related_terms: list[str] = field(default_factory=list)
    domain: str = "custom"

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        now = datetime.now(timezone.utc).isoformat()
        if not self.first_seen_at:
            self.first_seen_at = now
        if not self.last_updated_at:
            self.last_updated_at = now
