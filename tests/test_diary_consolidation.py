from __future__ import annotations

import json

from sirius_pulse.memory.diary.consolidator import DiaryConsolidator
from sirius_pulse.memory.diary.manager import DiaryManager
from sirius_pulse.memory.diary.models import DiaryEntry


def _entry(
    entry_id: str,
    *,
    content: str = "content",
    embedding: list[float] | None = None,
    source_ids: list[str] | None = None,
    source_diary_ids: list[str] | None = None,
    merge_count: int = 0,
) -> DiaryEntry:
    return DiaryEntry(
        entry_id=entry_id,
        group_id="group_a",
        created_at="2026-01-01T00:00:00+00:00",
        source_ids=source_ids or [f"raw_{entry_id}"],
        content=content,
        keywords=[entry_id],
        summary=content,
        embedding=embedding or [1.0, 0.0],
        merge_count=merge_count,
        source_diary_ids=source_diary_ids or [],
    )


def test_diary_consolidation_when_merging_then_preserves_original_entries(tmp_path):
    manager = DiaryManager(tmp_path)
    first = _entry("d1", content="alpha one", embedding=[1.0, 0.0])
    second = _entry("d2", content="alpha two", embedding=[0.99, 0.01])
    manager.add_entry("group_a", first)
    manager.add_entry("group_a", second)

    consolidator = DiaryConsolidator(
        manager,
        {
            "diary_consolidation_min_entries": 2,
            "diary_merge_similarity_threshold": 0.7,
        },
    )
    clusters = consolidator.find_clusters("group_a")
    assert [[entry.entry_id for entry in cluster] for cluster in clusters] == [["d1", "d2"]]

    merged = consolidator.parse_merge_result(
        json.dumps(
            {
                "content": "merged alpha memory",
                "summary": "merged alpha",
                "keywords": ["alpha"],
            }
        ),
        clusters[0],
    )
    assert merged is not None

    consolidator.append_merged_entries("group_a", clusters, [merged])

    entries = manager.get_entries_for_group("group_a")
    entry_ids = {entry.entry_id for entry in entries}
    assert {"d1", "d2", merged.entry_id} <= entry_ids
    assert merged.source_diary_ids == ["d1", "d2"]
    assert merged.source_ids == ["raw_d1", "raw_d2"]
    assert consolidator._cache_key("d1", "d2") in consolidator._sim_cache


def test_diary_consolidation_when_old_entries_are_covered_then_clusters_higher_level_entry(
    tmp_path,
):
    manager = DiaryManager(tmp_path)
    manager.add_entry("group_a", _entry("d1", embedding=[1.0, 0.0]))
    manager.add_entry("group_a", _entry("d2", embedding=[0.99, 0.01]))
    manager.add_entry(
        "group_a",
        _entry(
            "merged_old",
            content="old synthesis",
            embedding=[1.0, 0.0],
            source_ids=["raw_d1", "raw_d2"],
            source_diary_ids=["d1", "d2"],
            merge_count=1,
        ),
    )
    manager.add_entry("group_a", _entry("d3", embedding=[0.98, 0.02]))

    consolidator = DiaryConsolidator(
        manager,
        {
            "diary_consolidation_min_entries": 2,
            "diary_merge_similarity_threshold": 0.7,
        },
    )

    clusters = consolidator.find_clusters("group_a")

    assert [[entry.entry_id for entry in cluster] for cluster in clusters] == [["merged_old", "d3"]]
