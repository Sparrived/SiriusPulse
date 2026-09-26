"""Structured checkpoint memory data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sirius_pulse.mixins import JsonSerializable


@dataclass(slots=True)
class MemoryUnit(JsonSerializable):
    """A compact third-person memory unit distilled from chat history."""

    unit_id: str
    group_id: str
    created_at: str
    unit_type: str = "event"
    scope: str = "group"
    scope_id: str = ""
    summary: str = ""
    participants: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    retrieval_terms: list[str] = field(default_factory=list)
    identity_aliases: list[str] = field(default_factory=list)
    event_time: str = ""
    valid_until: str = ""
    status: str = ""
    salience: float = 0.5
    confidence: float = 0.7
    lifespan: str = "medium"
    should_prompt: bool = True
    source_ids: list[str] = field(default_factory=list)
    embedding: list[float] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryUnitGenerationResult(JsonSerializable):
    """Result of checkpoint memory unit generation."""

    units: list[MemoryUnit] = field(default_factory=list)


def embedding_text(unit: MemoryUnit) -> str:
    """单元被向量化时实际编码的文本。

    它是**唯一**口径：``MemoryUnitIndexer._unit_text`` 与向量的持久化指纹都从这里
    取。任何字段口径的分叉都会让指纹与向量失配，进而把过期向量当成当前向量复用。
    """
    return " ".join(
        [
            unit.summary,
            " ".join(unit.participants),
            " ".join(unit.topics),
            " ".join(unit.keywords),
            " ".join(unit.retrieval_terms),
            " ".join(unit.identity_aliases),
            unit.status,
            unit.event_time,
        ]
    ).strip()
