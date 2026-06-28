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
