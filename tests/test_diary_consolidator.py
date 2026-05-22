"""Tests for diary consolidation."""

from __future__ import annotations

from typing import Any

import pytest

from sirius_pulse.memory.diary.consolidator import DiaryConsolidator
from sirius_pulse.memory.diary.models import DiaryEntry


class MockDiaryManager:
    def __init__(self) -> None:
        self._entries: dict[str, list[DiaryEntry]] = {}

    def get_entries_for_group(self, group_id: str) -> list[DiaryEntry]:
        return list(self._entries.get(group_id, []))

    def replace_entries(self, group_id: str, new_entries: list[DiaryEntry]) -> None:
        self._entries[group_id] = list(new_entries)

    def add_entry(self, group_id: str, entry: DiaryEntry) -> None:
        self._entries.setdefault(group_id, []).append(entry)


@pytest.fixture
def manager():
    return MockDiaryManager()


def _entry(
    group_id: str = "g1",
    content: str = "test",
    embedding: list[float] | None = None,
    entry_id: str = "",
    keywords: list[str] | None = None,
) -> DiaryEntry:
    return DiaryEntry(
        entry_id=entry_id or f"e{hash(content) & 0xFFFFFF:06x}",
        group_id=group_id,
        created_at="2026-04-27T10:00:00+00:00",
        source_ids=["s1"],
        content=content,
        keywords=keywords or ["test"],
        summary=content[:20],
        embedding=embedding,
    )


def test_find_clusters_no_entries(manager: MockDiaryManager):
    c = DiaryConsolidator(manager)
    assert c.find_clusters("g1") == []


def test_find_clusters_not_enough_with_embeddings(manager: MockDiaryManager):
    manager.add_entry("g1", _entry(content="a", embedding=[1.0, 0.0, 0.0]))
    c = DiaryConsolidator(manager)
    assert c.find_clusters("g1") == []


def test_find_clusters_similar_pair(manager: MockDiaryManager):
    e1 = _entry(content="今天讨论了项目架构", embedding=[1.0, 0.0, 0.0])
    e2 = _entry(content="项目架构设计讨论", embedding=[0.95, 0.05, 0.0])
    e3 = _entry(content="晚上吃了火锅", embedding=[0.0, 1.0, 0.0])
    manager.add_entry("g1", e1)
    manager.add_entry("g1", e2)
    manager.add_entry("g1", e3)
    c = DiaryConsolidator(manager, {"diary_consolidation_min_entries": 2})
    clusters = c.find_clusters("g1")
    assert len(clusters) == 1
    assert len(clusters[0]) == 2
    contents = {e.content for e in clusters[0]}
    assert contents == {"今天讨论了项目架构", "项目架构设计讨论"}


def test_find_clusters_dissimilar_entries(manager: MockDiaryManager):
    e1 = _entry(content="A", embedding=[1.0, 0.0, 0.0])
    e2 = _entry(content="B", embedding=[0.0, 1.0, 0.0])
    e3 = _entry(content="C", embedding=[0.0, 0.0, 1.0])
    for e in (e1, e2, e3):
        manager.add_entry("g1", e)
    c = DiaryConsolidator(manager, {"diary_consolidation_min_entries": 2})
    assert c.find_clusters("g1") == []


def test_find_clusters_max_size_cap(manager: MockDiaryManager):
    # 6 entries: first 3 similar, last 3 similar, but cross-group dissimilar
    for i in range(3):
        manager.add_entry("g1", _entry(content=f"A{i}", embedding=[1.0, 0.0, 0.0]))
    for i in range(3):
        manager.add_entry("g1", _entry(content=f"B{i}", embedding=[0.0, 1.0, 0.0]))
    c = DiaryConsolidator(manager, {"diary_consolidation_min_entries": 2, "diary_consolidation_max_cluster_size": 2})
    clusters = c.find_clusters("g1")
    assert len(clusters) == 2
    assert all(len(c) == 2 for c in clusters)


def test_build_merge_prompt(manager: MockDiaryManager):
    c = DiaryConsolidator(manager)
    cluster = [
        _entry(content="今天天气很好，去了公园", keywords=["天气", "公园"]),
        _entry(content="明天要下雨，记得带伞", keywords=["下雨", "带伞"]),
    ]
    system, user = c.build_merge_prompt(cluster)
    assert "日记整理助手" in system
    assert "今天天气很好，去了公园" in user
    assert "明天要下雨，记得带伞" in user


def test_parse_merge_result_valid_json(manager: MockDiaryManager):
    c = DiaryConsolidator(manager)
    cluster = [_entry(content="A"), _entry(content="B")]
    raw = '{"content": "合并后内容", "summary": "摘要", "keywords": ["k1", "k2"]}'
    entry = c.parse_merge_result(raw, cluster)
    assert entry is not None
    assert entry.content == "合并后内容"
    assert entry.summary == "摘要"
    assert entry.keywords == ["k1", "k2"]
    assert entry.entry_id.startswith("merged_")
    assert "s1" in entry.source_ids


def test_parse_merge_result_markdown_fenced(manager: MockDiaryManager):
    c = DiaryConsolidator(manager)
    cluster = [_entry(content="A")]
    raw = '```json\n{"content": " fenced ", "summary": "s", "keywords": ["k"]}\n```'
    entry = c.parse_merge_result(raw, cluster)
    assert entry is not None
    assert entry.content == "fenced"


def test_parse_merge_result_regex_fallback(manager: MockDiaryManager):
    c = DiaryConsolidator(manager)
    cluster = [_entry(content="A")]
    raw = 'some text "content": "fallback", "summary": "s"'
    entry = c.parse_merge_result(raw, cluster)
    assert entry is not None
    assert entry.content == "fallback"


def test_parse_merge_result_empty_content(manager: MockDiaryManager):
    c = DiaryConsolidator(manager)
    cluster = [_entry(content="A")]
    raw = '{"summary": "only summary"}'
    assert c.parse_merge_result(raw, cluster) is None


def test_extract_json_plain():
    assert DiaryConsolidator._extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_markdown():
    assert DiaryConsolidator._extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_regex_fields():
    result = DiaryConsolidator._extract_json('"content": "hello", "summary": "hi", "keywords": ["k"]')
    assert result == {"content": "hello", "summary": "hi", "keywords": ["k"]}


def test_rebuild_entries(manager: MockDiaryManager):
    e1 = _entry(content="A", embedding=[1.0, 0.0])
    e2 = _entry(content="B", embedding=[0.95, 0.05])
    e3 = _entry(content="C", embedding=[0.0, 1.0])
    for e in (e1, e2, e3):
        manager.add_entry("g1", e)

    c = DiaryConsolidator(manager)
    clusters = [[e1, e2]]
    merged = [DiaryEntry(
        entry_id="m1",
        group_id="g1",
        created_at="2026-04-27T10:00:00+00:00",
        source_ids=["s1", "s2"],
        content="AB merged",
        keywords=["ab"],
        summary="ab",
    )]
    c.rebuild_entries("g1", clusters, merged)

    entries = manager.get_entries_for_group("g1")
    assert len(entries) == 2
    contents = {e.content for e in entries}
    assert contents == {"C", "AB merged"}
