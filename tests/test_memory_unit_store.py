"""记忆单元文件存储：分组列表缓存与退避标记的持久化。"""

from __future__ import annotations

import json

from sirius_pulse.memory.units import MemoryUnit, MemoryUnitFileStore


def _unit(unit_id: str, summary: str, **changes) -> MemoryUnit:
    values = {
        "unit_id": unit_id,
        "group_id": "group-a",
        "created_at": "2026-07-12T00:00:00+00:00",
        "unit_type": "event",
        "scope": "group",
        "scope_id": "",
        "summary": summary,
        "keywords": ["部署"],
        "salience": 0.6,
        "confidence": 0.7,
        "source_ids": [f"src-{unit_id}"],
    }
    values.update(changes)
    return MemoryUnit(**values)


def test_list_group_ids_reads_each_file_once_and_invalidates_on_save(tmp_path, monkeypatch):
    store = MemoryUnitFileStore(tmp_path)
    store.save("group-a", [_unit("mem-1", "A")])
    store.save("group-b", [_unit("mem-2", "B")])

    calls = {"count": 0}
    original = MemoryUnitFileStore._read_payload

    def counting(path):
        calls["count"] += 1
        return original(path)

    monkeypatch.setattr(MemoryUnitFileStore, "_read_payload", staticmethod(counting))

    assert store.list_group_ids() == ["group-a", "group-b"]
    assert calls["count"] == 2

    assert store.list_group_ids() == ["group-a", "group-b"]
    assert calls["count"] == 2, "第二次调用必须命中缓存"

    store.save("group-c", [_unit("mem-3", "C")])
    assert store.list_group_ids() == ["group-a", "group-b", "group-c"]


def test_list_group_ids_skips_files_without_units(tmp_path):
    store = MemoryUnitFileStore(tmp_path)
    store.save("group-a", [_unit("mem-1", "A")])
    store.save("group-empty", [])

    assert store.list_group_ids() == ["group-a"]


def test_list_group_ids_survives_corrupt_file(tmp_path):
    store = MemoryUnitFileStore(tmp_path)
    store.save("group-a", [_unit("mem-1", "A")])
    (tmp_path / "memory_units" / "broken.json").write_text("{not json", encoding="utf-8")

    assert store.list_group_ids() == ["group-a"]


def test_stored_payload_keeps_should_prompt_flag(tmp_path):
    """落盘格式必须保住 should_prompt，否则重启后退休状态丢失。"""
    store = MemoryUnitFileStore(tmp_path)
    store.save("group-a", [_unit("mem-1", "A", should_prompt=False)])

    payload = json.loads((tmp_path / "memory_units" / "group-a.json").read_text(encoding="utf-8"))
    assert payload["units"][0]["should_prompt"] is False
    assert store.load("group-a")[0].should_prompt is False
