from sirius_pulse.memory.units import MemoryUnit
from sirius_pulse.memory.units.deduplicator import (
    DedupVerdict,
    link_conflict,
    merge_memory_units,
    normalize_summary,
    same_boundary,
)


def _unit(unit_id: str, summary: str, **changes) -> MemoryUnit:
    values = {
        "unit_id": unit_id,
        "group_id": "group-a",
        "created_at": "2026-07-12T00:00:00+00:00",
        "unit_type": "preference",
        "scope": "user",
        "scope_id": "alice",
        "summary": summary,
        "participants": ["alice"],
        "topics": ["reply-style"],
        "keywords": ["concise"],
        "salience": 0.6,
        "confidence": 0.7,
        "lifespan": "medium",
        "source_ids": ["src-1"],
        "embedding": [1.0, 0.0],
    }
    values.update(changes)
    return MemoryUnit(**values)


def test_normalized_equal_summary_is_duplicate_only_inside_same_boundary():
    old = _unit("mem-old", "Alice prefers concise replies。")
    same = _unit("mem-new", "  alice prefers concise replies!  ")
    other_group = _unit("mem-other", same.summary, group_id="group-b")

    assert normalize_summary(old.summary) == normalize_summary(same.summary)
    assert same_boundary(old, same) is True
    assert same_boundary(old, other_group) is False


def test_merge_keeps_canonical_identity_and_all_sources():
    old = _unit("mem-old", "Alice prefers concise replies.")
    new = _unit(
        "mem-new",
        "Alice prefers concise replies with examples.",
        created_at="2026-07-12T01:00:00+00:00",
        participants=["alice", "sirius"],
        topics=["reply-style", "examples"],
        keywords=["examples"],
        salience=0.9,
        confidence=0.8,
        lifespan="long",
        source_ids=["src-2"],
    )
    merged = merge_memory_units(
        old,
        new,
        DedupVerdict("MERGE", "mem-old", "Alice prefers concise replies with examples.", "补充"),
        now_iso="2026-07-12T02:00:00+00:00",
    )

    assert merged.unit_id == "mem-old"
    assert merged.created_at == old.created_at
    assert merged.source_ids == ["src-1", "src-2"]
    assert merged.participants == ["alice", "sirius"]
    assert merged.topics == ["reply-style", "examples"]
    assert merged.keywords == ["concise", "examples"]
    assert merged.salience == 0.9
    assert merged.confidence == 0.8
    assert merged.lifespan == "long"
    assert merged.embedding is None
    assert merged.metadata["merged_unit_ids"] == ["mem-new"]
    assert merged.metadata["revision_count"] == 1


def test_conflict_keeps_both_units_and_links_them():
    old = _unit("mem-old", "Alice prefers concise replies.")
    new = _unit("mem-new", "Alice now prefers detailed explanations.")

    linked_old, linked_new = link_conflict(old, new, "偏好发生变化")

    assert linked_old.metadata["conflicts_with"] == ["mem-new"]
    assert linked_new.metadata["conflicts_with"] == ["mem-old"]
    assert linked_old.metadata["conflict_reason"] == "偏好发生变化"
