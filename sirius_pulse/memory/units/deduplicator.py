"""Deterministic rules for reconciling memory units."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sirius_pulse.memory.units.models import MemoryUnit

_END_PUNCTUATION = "。！？.!?"
_LIFESPAN_RANK = {"short": 0, "medium": 1, "long": 2}


@dataclass(slots=True, frozen=True)
class DedupVerdict:
    """The decision for one incoming memory unit."""

    decision: str
    target_unit_id: str = ""
    merged_summary: str = ""
    reason: str = ""


def _clone(unit: MemoryUnit) -> MemoryUnit:
    return MemoryUnit.from_dict(unit.to_dict())


def _union(left: list[str], right: list[str], limit: int | None = None) -> list[str]:
    result: list[str] = []
    for value in [*left, *right]:
        if value and value not in result:
            result.append(value)
        if limit is not None and len(result) >= limit:
            break
    return result


def normalize_summary(summary: str) -> str:
    """Normalize a summary for deterministic exact-match deduplication."""
    normalized = unicodedata.normalize("NFKC", summary).casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.rstrip(_END_PUNCTUATION).rstrip()


def same_boundary(left: MemoryUnit, right: MemoryUnit) -> bool:
    """Return whether two units are allowed to influence each other."""
    return (
        left.group_id,
        left.scope,
        left.scope_id,
        left.unit_type,
    ) == (
        right.group_id,
        right.scope,
        right.scope_id,
        right.unit_type,
    )


def _index_text_fields(unit: MemoryUnit) -> tuple[object, ...]:
    return (unit.summary, unit.participants, unit.topics, unit.keywords)


def merge_memory_units(
    canonical: MemoryUnit,
    incoming: MemoryUnit,
    verdict: DedupVerdict,
    *,
    now_iso: str,
) -> MemoryUnit:
    """Merge a duplicate into its canonical memory unit."""
    if verdict.decision not in {"DUPLICATE", "MERGE"}:
        raise ValueError("merge requires DUPLICATE or MERGE")
    if not same_boundary(canonical, incoming):
        raise ValueError("cannot merge memory units across boundaries")
    merged = _clone(canonical)
    before = _index_text_fields(merged)
    if verdict.decision == "MERGE":
        summary = verdict.merged_summary.strip()
        if not summary or len(summary) > 180:
            raise ValueError("invalid merged summary")
        merged.summary = summary
    merged.created_at = min(canonical.created_at, incoming.created_at)
    merged.source_ids = _union(canonical.source_ids, incoming.source_ids)
    merged.participants = _union(canonical.participants, incoming.participants, 8)
    merged.topics = _union(canonical.topics, incoming.topics, 8)
    merged.keywords = _union(canonical.keywords, incoming.keywords, 12)
    merged.salience = max(canonical.salience, incoming.salience)
    merged.confidence = max(canonical.confidence, incoming.confidence)
    merged.lifespan = max(
        (canonical.lifespan, incoming.lifespan),
        key=lambda value: _LIFESPAN_RANK.get(value, 1),
    )
    merged.should_prompt = canonical.should_prompt or incoming.should_prompt
    metadata = dict(canonical.metadata)
    metadata["revision_count"] = int(metadata.get("revision_count", 0)) + 1
    metadata["merged_unit_ids"] = _union(
        list(metadata.get("merged_unit_ids") or []), [incoming.unit_id]
    )
    metadata["last_merged_at"] = now_iso
    metadata["decision"] = verdict.decision.lower()
    merged.metadata = metadata
    if _index_text_fields(merged) != before:
        merged.embedding = None
    return merged


def link_conflict(
    canonical: MemoryUnit,
    incoming: MemoryUnit,
    reason: str,
) -> tuple[MemoryUnit, MemoryUnit]:
    """Keep conflicting facts while linking both units."""
    left, right = _clone(canonical), _clone(incoming)
    left.metadata = dict(left.metadata)
    right.metadata = dict(right.metadata)
    left.metadata["conflicts_with"] = _union(
        list(left.metadata.get("conflicts_with") or []), [right.unit_id]
    )
    right.metadata["conflicts_with"] = _union(
        list(right.metadata.get("conflicts_with") or []), [left.unit_id]
    )
    left.metadata["conflict_reason"] = reason
    right.metadata["conflict_reason"] = reason
    return left, right


def apply_verdict(
    units: list[MemoryUnit],
    incoming: MemoryUnit,
    verdict: DedupVerdict,
    *,
    now_iso: str,
) -> tuple[list[MemoryUnit], MemoryUnit]:
    """Apply a deduplication decision without mutating the input list."""
    working = [_clone(unit) for unit in units]
    if verdict.decision == "NEW":
        accepted = _clone(incoming)
        working.append(accepted)
        return working, accepted
    target_index = next(
        (index for index, unit in enumerate(working) if unit.unit_id == verdict.target_unit_id),
        -1,
    )
    if target_index < 0:
        accepted = _clone(incoming)
        working.append(accepted)
        return working, accepted
    if verdict.decision == "CONFLICT":
        linked_target, accepted = link_conflict(working[target_index], incoming, verdict.reason)
        working[target_index] = linked_target
        working.append(accepted)
        return working, accepted
    accepted = merge_memory_units(working[target_index], incoming, verdict, now_iso=now_iso)
    working[target_index] = accepted
    return working, accepted
