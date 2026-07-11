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


class _Embedding:
    available = True

    def encode_single(self, text: str) -> list[float]:
        return [1.0, 0.0] if "concise" in text.lower() else [0.0, 1.0]


def test_indexer_returns_only_same_boundary_semantic_candidates():
    from sirius_pulse.memory.units import MemoryUnitIndexer

    indexer = MemoryUnitIndexer(_Embedding())
    match = _unit("mem-match", "Alice prefers concise replies.")
    indexer.add(match)
    indexer.add(_unit("mem-group", match.summary, group_id="group-b"))
    indexer.add(_unit("mem-type", match.summary, unit_type="note"))

    incoming = _unit("mem-new", "Alice likes concise answers.", embedding=None)
    candidates = indexer.semantic_candidates(incoming, top_k=5, min_similarity=0.80)

    assert [(unit.unit_id, score) for unit, score in candidates] == [("mem-match", 1.0)]


def test_indexer_replace_group_removes_stale_units():
    from sirius_pulse.memory.units import MemoryUnitIndexer

    indexer = MemoryUnitIndexer()
    indexer.add(_unit("old", "old"))
    replacement = _unit("new", "new")
    indexer.replace_group("group-a", [replacement])
    assert indexer.list_all() == [replacement]
