"""记忆单元文件存储：分组列表缓存与退避标记的持久化。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

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


def test_list_group_ids_scans_each_file_once_and_updates_incrementally(tmp_path, monkeypatch):
    store = MemoryUnitFileStore(tmp_path)
    store.save("group-a", [_unit("mem-1", "A")])
    store.save("group-b", [_unit("mem-2", "B")])

    calls = {"count": 0}
    original = MemoryUnitFileStore._probe_group_id

    def counting(self, path):
        calls["count"] += 1
        return original(self, path)

    monkeypatch.setattr(MemoryUnitFileStore, "_probe_group_id", counting)

    assert store.list_group_ids() == ["group-a", "group-b"]
    assert calls["count"] == 2

    assert store.list_group_ids() == ["group-a", "group-b"]
    assert calls["count"] == 2, "第二次调用必须命中缓存"

    store.save("group-c", [_unit("mem-3", "C")])
    assert store.list_group_ids() == ["group-a", "group-b", "group-c"]
    assert calls["count"] == 2, "写盘应增量更新缓存，而不是作废后重扫整个目录"


def test_list_group_ids_drops_group_when_its_units_are_emptied(tmp_path):
    """写盘方自己知道改了哪个分组，清空后必须从缓存里移除。"""
    store = MemoryUnitFileStore(tmp_path)
    store.save("group-a", [_unit("mem-1", "A")])
    store.save("group-b", [_unit("mem-2", "B")])
    assert store.list_group_ids() == ["group-a", "group-b"]

    store.save("group-a", [])

    assert store.list_group_ids() == ["group-b"]


def test_group_id_probe_reads_only_the_file_head(tmp_path):
    """分组列表必须靠读文件头拿到，不能为 group_id 解析几百 MB 的向量。"""
    import sirius_pulse.memory.units.store as store_mod

    store = MemoryUnitFileStore(tmp_path)
    store.save("group-big", [_unit("mem-big", "大向量", embedding=[0.1] * 100_000)])
    path = store._path("group-big")
    assert path.stat().st_size > 1_000_000, "样本必须是大文件才有意义"

    read_bytes: list[int] = []
    real_read = Path.read_text

    def counting_read(self, *args, **kwargs):
        read_bytes.append(self.stat().st_size)
        return real_read(self, *args, **kwargs)

    # _probe_group_id 只应走 open().read(N)；一旦出现 read_text 就是全量解析。
    with patch.object(Path, "read_text", counting_read):
        store._group_ids = None
        assert store.list_group_ids() == ["group-big"]

    assert read_bytes == [], "_probe_group_id 不得全量读文件"

    with path.open("r", encoding="utf-8") as handle:
        head = handle.read(store_mod._GROUP_ID_PROBE_BYTES)
    assert len(head) < path.stat().st_size


def test_group_id_probe_falls_back_for_unusual_layout(tmp_path):
    """group_id 不在文件头时不漏分组：退回全量解析。"""
    store = MemoryUnitFileStore(tmp_path)
    base = tmp_path / "memory_units"
    base.mkdir(parents=True, exist_ok=True)
    # 手工构造一个 group_id 出现在 4 KiB 之后的文件
    (base / "weird.json").write_text(
        json.dumps(
            {
                "filler": "x" * 5000,
                "group_id": "group-weird",
                "units": [{"unit_id": "u"}],
            }
        ),
        encoding="utf-8",
    )

    assert store.list_group_ids() == ["group-weird"]


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
