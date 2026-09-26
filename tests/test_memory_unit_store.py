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

    # 旧格式（向量内联）的文件才会这么大；必须保证探测它时不全量读。
    base = tmp_path / "memory_units"
    base.mkdir(parents=True, exist_ok=True)
    legacy = base / "group-big.json"
    legacy.write_text(
        json.dumps(
            {
                "group_id": "group-big",
                "units": [
                    {
                        "unit_id": "mem-big",
                        "group_id": "group-big",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "summary": "大向量",
                        "embedding": [0.1234567890123456789] * 100_000,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert legacy.stat().st_size > 1_000_000, "样本必须是大文件才有意义"

    read_bytes: list[int] = []
    real_read = Path.read_text

    def counting_read(self, *args, **kwargs):
        read_bytes.append(self.stat().st_size)
        return real_read(self, *args, **kwargs)

    # _probe_group_id 只应走 open().read(N)；一旦出现 read_text 就是全量解析。
    with patch.object(Path, "read_text", counting_read):
        store = MemoryUnitFileStore(tmp_path)
        assert store.list_group_ids() == ["group-big"]

    assert read_bytes == [], "_probe_group_id 不得全量读文件"

    with legacy.open("r", encoding="utf-8") as handle:
        head = handle.read(store_mod._GROUP_ID_PROBE_BYTES)
    assert len(head) < legacy.stat().st_size


def test_save_keeps_vectors_out_of_the_metadata_json(tmp_path):
    """向量必须落在 sidecar 里：内联会让「取一条摘要」变成解析几百 MB 的向量。"""
    store = MemoryUnitFileStore(tmp_path)
    store.save("group-big", [_unit("mem-big", "大向量", embedding=[0.1] * 100_000)])

    path = store._path("group-big")
    assert path.stat().st_size < 10_000, "元数据文件不该再包含向量"

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "embedding" not in raw["units"][0]
    assert raw["vector_file"]


def test_load_round_trips_vectors_through_the_sidecar(tmp_path):
    store = MemoryUnitFileStore(tmp_path)
    store.save("group-a", [_unit("mem-1", "A", embedding=[0.25, 0.5, 0.75])])

    loaded = store.load("group-a")

    assert [round(value, 4) for value in loaded[0].embedding] == [0.25, 0.5, 0.75]


def test_metadata_only_load_does_not_read_vectors(tmp_path):
    """常驻检索路径按元数据加载，不能为了摘要付出向量解析成本。"""
    store = MemoryUnitFileStore(tmp_path)
    store.save("group-a", [_unit("mem-1", "A", embedding=[0.5] * 1024)])

    units = store.load("group-a", with_embeddings=False)

    assert units[0].embedding is None
    assert store.hydrate_embeddings("group-a", units) == 1
    assert len(units[0].embedding or []) == 1024


def test_hydrate_only_touches_requested_units(tmp_path):
    store = MemoryUnitFileStore(tmp_path)
    store.save(
        "group-a",
        [
            _unit("mem-1", "A", embedding=[0.5] * 4),
            _unit("mem-2", "B", embedding=[0.25] * 4),
        ],
    )

    units = store.load("group-a", with_embeddings=False)

    assert store.hydrate_embeddings("group-a", units, unit_ids={"mem-2"}) == 1
    assert units[0].embedding is None
    assert units[1].embedding is not None


def test_metadata_only_save_preserves_vectors_it_never_loaded(tmp_path):
    """只改元数据的写入不得清空向量——常驻路径正是「不带向量读入」的。"""
    store = MemoryUnitFileStore(tmp_path)
    store.save("group-a", [_unit("mem-1", "A", embedding=[0.5] * 4)])

    units = store.load("group-a", with_embeddings=False)
    units[0].should_prompt = False
    store.save("group-a", units)

    reloaded = store.load("group-a")
    assert reloaded[0].should_prompt is False
    assert len(reloaded[0].embedding or []) == 4


def test_save_drops_vector_when_the_embedded_text_changed(tmp_path):
    """文本改过就必须丢掉旧向量，否则过期向量会被当成当前事实参与检索。"""
    store = MemoryUnitFileStore(tmp_path)
    store.save("group-a", [_unit("mem-1", "旧事实", embedding=[0.5] * 4)])

    units = store.load("group-a", with_embeddings=False)
    units[0].summary = "新事实"
    store.save("group-a", units)

    assert store.load("group-a")[0].embedding is None


def test_legacy_inline_vectors_are_migrated_and_preserved(tmp_path):
    """存量文件是向量内联的：首次落盘必须搬进 sidecar，且一条都不能丢。"""
    base = tmp_path / "memory_units"
    base.mkdir(parents=True, exist_ok=True)
    (base / "group-a.json").write_text(
        json.dumps(
            {
                "group_id": "group-a",
                "units": [
                    {
                        "unit_id": "mem-legacy",
                        "group_id": "group-a",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "summary": "旧格式单元",
                        "embedding": [0.75] * 8,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = MemoryUnitFileStore(tmp_path)
    assert store.is_legacy_layout("group-a") is True

    units = store.load("group-a", with_embeddings=False)
    assert units[0].embedding is None, "元数据加载不该解析内联向量"
    assert store.hydrate_embeddings("group-a", units) == 1, "旧格式仍要能补出向量"

    units[0].summary = "旧格式单元（已迁移）"
    store.save("group-a", units)

    raw = json.loads((base / "group-a.json").read_text(encoding="utf-8"))
    assert store.is_legacy_layout("group-a") is False
    assert "embedding" not in raw["units"][0]
    assert store.load("group-a")[0].embedding == [0.75] * 8


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
