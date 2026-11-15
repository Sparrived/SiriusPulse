"""旧的 memory_units/*.json + 向量 sidecar 一次性迁入 persona.db。

业务视角：升级后她记得的事一条不少、向量不用重算；原文件留作备份不删；迁移被打断
或重复执行都不会让记忆重复、复活或被旧文件覆盖。
"""

from __future__ import annotations

import json
import shutil
import threading
from array import array
from pathlib import Path

from sirius_pulse.memory.units import MemoryUnit, MemoryUnitManager, MemoryUnitStore
from sirius_pulse.memory.units.store import text_fingerprint


def _item(unit_id: str, summary: str, **extra) -> dict:
    return {
        "unit_id": unit_id,
        "group_id": "ignored",  # 旧文件里单元自带的 group_id 以文件为准
        "created_at": "2026-06-28T00:00:00+00:00",
        "summary": summary,
        "keywords": ["猫"],
        **extra,
    }


def _write_v2(base: Path, group_id: str, units: list[tuple[dict, list[float] | None]]) -> Path:
    """写一份 v2 旧格式：元数据 JSON + 打包 float32 的向量 sidecar。"""
    base.mkdir(parents=True, exist_ok=True)
    (base / "vectors").mkdir(exist_ok=True)
    blob = bytearray()
    items = []
    for item, vector in units:
        item = dict(item)
        if vector is not None:
            item["vector_offset"] = len(blob)
            item["vector_dim"] = len(vector)
            item["vector_text"] = text_fingerprint(MemoryUnit.from_dict(item))
            blob.extend(array("f", vector).tobytes())
        items.append(item)
    name = f"{group_id}.abc.bin"
    (base / "vectors" / name).write_bytes(bytes(blob))
    path = base / f"{group_id}.json"
    path.write_text(
        json.dumps(
            {"group_id": group_id, "units_format": 2, "units": items, "vector_file": name},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _backups(work_path: Path) -> Path:
    return work_path / "backups" / "memory_units_json"


def test_sidecar_and_inline_layouts_are_imported_with_their_vectors(tmp_path):
    base = tmp_path / "memory_units"
    _write_v2(
        base,
        "g1",
        [(_item("m1", "她养了一只猫"), [0.5, 0.25]), (_item("m2", "她怕打雷"), None)],
    )
    (base / "g2.json").write_text(
        json.dumps({"group_id": "g2", "units": [_item("m3", "他喜欢下雨天", embedding=[0.75] * 3)]}),
        encoding="utf-8",
    )

    store = MemoryUnitStore(tmp_path)

    g1 = store.load("g1")
    assert [(u.unit_id, u.group_id, u.embedding) for u in g1] == [
        ("m1", "g1", [0.5, 0.25]),
        ("m2", "g1", None),
    ]
    assert [(u.unit_id, u.embedding) for u in store.load("g2")] == [("m3", [0.75] * 3)]
    assert store.list_group_ids() == ["g1", "g2"]


def test_originals_are_moved_to_a_backup_not_deleted(tmp_path):
    base = tmp_path / "memory_units"
    path = _write_v2(base, "g1", [(_item("m1", "她养了一只猫"), [0.5, 0.25])])
    original_json = path.read_bytes()
    original_bin = (base / "vectors" / "g1.abc.bin").read_bytes()

    MemoryUnitStore(tmp_path).load("g1")

    (backup,) = list(_backups(tmp_path).iterdir())
    assert (backup / "g1.json").read_bytes() == original_json
    assert (backup / "vectors" / "g1.abc.bin").read_bytes() == original_bin
    assert not path.exists()
    assert not (base / "vectors").exists()


def test_a_stale_sidecar_vector_is_not_attached_to_edited_text(tmp_path):
    """旧格式里文本改过、向量没重算的单元：导入时丢掉那份过期向量。"""
    base = tmp_path / "memory_units"
    path = _write_v2(base, "g1", [(_item("m1", "她养了一只猫"), [0.5, 0.25])])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["units"][0]["summary"] = "她养了两只猫"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    (unit,) = MemoryUnitStore(tmp_path).load("g1")

    assert unit.summary == "她养了两只猫"
    assert unit.embedding is None


def test_rerunning_with_the_same_files_neither_duplicates_nor_resurrects(tmp_path):
    """迁移被打断后重跑（或有人把备份拷回来）：已导入过的文件不再导入。"""
    base = tmp_path / "memory_units"
    _write_v2(base, "g1", [(_item("m1", "她养了一只猫"), None), (_item("m2", "她怕打雷"), None)])
    store = MemoryUnitStore(tmp_path)
    assert len(store.load("g1")) == 2
    # 之后真人在 WebUI 里删了一条、改了一条。
    with store.transaction() as tx:
        tx.delete("g1", "m2")
        edited = tx.get("g1", "m1")
        edited.summary = "她养了一只橘猫"
        tx.upsert("g1", edited)

    (backup,) = list(_backups(tmp_path).iterdir())
    shutil.copy2(backup / "g1.json", base / "g1.json")  # 同名、同大小、同 mtime

    rerun = MemoryUnitStore(tmp_path)
    assert {u.unit_id: u.summary for u in rerun.load("g1")} == {"m1": "她养了一只橘猫"}
    assert not (base / "g1.json").exists(), "重跑只把它补挪进备份"


def test_legacy_file_never_overwrites_units_already_in_the_database(tmp_path):
    store = MemoryUnitStore(tmp_path)
    store.save("g1", [MemoryUnit(unit_id="m1", group_id="g1", created_at="", summary="库里的版本")])
    base = tmp_path / "memory_units"
    _write_v2(base, "g1", [(_item("m1", "旧文件的版本"), None), (_item("m2", "只在旧文件里"), None)])

    units = MemoryUnitStore(tmp_path).load("g1")

    assert [(u.unit_id, u.summary) for u in units] == [("m1", "库里的版本"), ("m2", "只在旧文件里")]


def test_a_file_that_cannot_be_read_right_now_is_retried_later(tmp_path, monkeypatch):
    """被杀软短暂占用的文件原地不动（连同向量目录），下次启动再导。"""
    base = tmp_path / "memory_units"
    _write_v2(base, "g1", [(_item("m1", "她养了一只猫"), [0.5, 0.25])])
    real_read_text = Path.read_text

    def locked(self, *args, **kwargs):
        if self.name == "g1.json":
            raise PermissionError("locked")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locked)
    assert MemoryUnitStore(tmp_path).load("g1") == []
    assert (base / "g1.json").exists()
    assert (base / "vectors" / "g1.abc.bin").exists()

    monkeypatch.setattr(Path, "read_text", real_read_text)
    (unit,) = MemoryUnitStore(tmp_path).load("g1")
    assert unit.embedding == [0.5, 0.25]


def test_two_processes_migrating_at_once_import_each_file_once(tmp_path):
    base = tmp_path / "memory_units"
    _write_v2(base, "g1", [(_item(f"m{i}", f"事实 {i}"), [float(i), 1.0]) for i in range(50)])
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def migrate() -> None:
        try:
            barrier.wait()
            MemoryUnitStore(tmp_path).list_group_ids()
        except BaseException as exc:  # pragma: no cover - 失败时报告
            errors.append(exc)

    threads = [threading.Thread(target=migrate) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    units = MemoryUnitStore(tmp_path).load("g1")
    assert len(units) == 50
    assert units[7].embedding == [7.0, 1.0]


def test_an_upgraded_persona_recalls_its_old_memories(tmp_path):
    """端到端：旧数据目录直接启动引擎侧管理器，检索得到旧记忆。"""
    _write_v2(
        tmp_path / "memory_units",
        "g1",
        [(_item("m1", "她养了一只猫", salience=0.9, confidence=0.9), None)],
    )

    manager = MemoryUnitManager(tmp_path)

    assert [u.unit_id for u in manager.retrieve("猫", group_id="g1")] == ["m1"]
    assert manager.checkpointed_source_ids("g1") == set()
