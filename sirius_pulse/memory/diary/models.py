"""Diary memory data models."""

from __future__ import annotations

from dataclasses import dataclass, field

from sirius_pulse.mixins import JsonSerializable


@dataclass(slots=True)
class DiaryEntry(JsonSerializable):
    """A diary entry generated from basic memory archive candidates.

    Attributes:
        entry_id: Unique identifier.
        group_id: Group/chat identifier.
        created_at: ISO 8601 timestamp of generation.
        source_ids: List of basic_memory entry_ids that fed this diary.
        content: LLM-generated diary text.
        keywords: Extracted keywords for quick filtering.
        summary: One-line summary (≤50 chars recommended).
        embedding: Semantic vector for RAG retrieval (optional).
        merge_count: How many times this entry has been merged with others.
    """

    entry_id: str
    group_id: str
    created_at: str
    source_ids: list[str] = field(default_factory=list)
    content: str = ""
    keywords: list[str] = field(default_factory=list)
    summary: str = ""
    embedding: list[float] | None = None
    merge_count: int = 0


@dataclass(slots=True)
class DiaryGenerationResult(JsonSerializable):
    """Result of diary generation, including extracted semantic topics."""

    entry: DiaryEntry
    dominant_topic: str = ""
    interest_topics: list[str] = field(default_factory=list)
