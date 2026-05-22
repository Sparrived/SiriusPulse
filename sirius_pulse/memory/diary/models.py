"""Diary memory data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class DiaryEntry:
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "group_id": self.group_id,
            "created_at": self.created_at,
            "source_ids": list(self.source_ids),
            "content": self.content,
            "keywords": list(self.keywords),
            "summary": self.summary,
            "embedding": list(self.embedding) if self.embedding else None,
            "merge_count": self.merge_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiaryEntry":
        emb = data.get("embedding")
        return cls(
            entry_id=data.get("entry_id", ""),
            group_id=data.get("group_id", ""),
            created_at=data.get("created_at", ""),
            source_ids=list(data.get("source_ids", [])),
            content=data.get("content", ""),
            keywords=list(data.get("keywords", [])),
            summary=data.get("summary", ""),
            embedding=list(emb) if isinstance(emb, list) else None,
            merge_count=int(data.get("merge_count", 0)),
        )


@dataclass(slots=True)
class DiaryGenerationResult:
    """Result of diary generation, including extracted semantic topics."""

    entry: DiaryEntry
    dominant_topic: str = ""
    interest_topics: list[str] = field(default_factory=list)
