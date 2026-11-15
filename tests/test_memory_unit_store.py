"""记忆单元存储（persona.db）：读写往返、向量沿用与作废、逐行写入、分组缓存。"""

from __future__ import annotations

import sqlite3

import pytest

from sirius_pulse.memory.units import MemoryUnit, MemoryUnitStore


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


def _rows(tmp_path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(tmp_path / "persona.db")
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_units_round_trip_with_every_field(tmp_path):
    store = MemoryUnitStore(tmp_path)
    original = _unit(
        "mem-1",
        "她养了一只猫",
        participants=["u1"],
        topics=["宠物"],
        retrieval_terms=["猫"],
        identity_aliases=["小雀"],
        event_time="2026-07-11",
        valid_until="2027-01-01",
        status="ongoing",
        lifespan="long",
        should_prompt=False,
        metadata={"retired": "manual", "origin": "webui"},
        embedding=[0.25, 0.5, 0.75],
    )
    store.save("group-a", [original])

    loaded = MemoryUnitStore(tmp_path).load("group-a")

    assert loaded == [original]


def test_list_group_ids_is_cached_and_updated_by_writes(tmp_path, monkeypatch):
    store = MemoryUnitStore(tmp_path)
    store.save("group-a", [_unit("mem-1", "A")])
    store.save("group-b", [_unit("mem-2", "B")])
    assert store.list_group_ids() == ["group-a", "group-b"]

    reads = {"n": 0}
    original = MemoryUnitStore._read

    def counting(self):
        reads["n"] += 1
        return original(self)

    monkeypatch.setattr(MemoryUnitStore, "_read", counting)
    store.save("group-c", [_unit("mem-3", "C")])
    store.save("group-a", [])

    assert store.list_group_ids() == ["group-b", "group-c"]
    assert reads["n"] == 0, "检索路径每条消息都会问分组：写入后应增量更新缓存，而不是重查"


def test_metadata_only_load_leaves_vectors_in_the_database(tmp_path):
    """常驻检索路径按元数据加载，只给真正参与注入的单元补向量。"""
    store = MemoryUnitStore(tmp_path)
    store.save(
        "group-a",
        [_unit("mem-1", "A", embedding=[0.5] * 4), _unit("mem-2", "B", embedding=[0.25] * 4)],
    )

    units = store.load("group-a", with_embeddings=False)
    assert [unit.embedding for unit in units] == [None, None]

    assert store.hydrate_embeddings("group-a", units, unit_ids={"mem-2"}) == 1
    assert units[0].embedding is None
    assert units[1].embedding == [0.25] * 4
    # 向量以 float32 BLOB 存放：4 维 = 16 字节，而不是一段 JSON 文本。
    assert _rows(tmp_path, "SELECT length(vector) FROM memory_unit_vectors") == [(16,), (16,)]


def test_metadata_only_save_preserves_vectors_it_never_loaded(tmp_path):
    """只改元数据的写入不得清空向量——常驻路径正是「不带向量读入」的。"""
    store = MemoryUnitStore(tmp_path)
    store.save("group-a", [_unit("mem-1", "A", embedding=[0.5] * 4)])

    units = store.load("group-a", with_embeddings=False)
    units[0].should_prompt = False
    store.save("group-a", units)

    reloaded = store.load("group-a")
    assert reloaded[0].should_prompt is False
    assert reloaded[0].embedding == [0.5] * 4


def test_save_drops_vector_when_the_embedded_text_changed(tmp_path):
    """文本改过就必须丢掉旧向量，否则过期向量会被当成当前事实参与检索。"""
    store = MemoryUnitStore(tmp_path)
    store.save("group-a", [_unit("mem-1", "旧事实", embedding=[0.5] * 4)])

    units = store.load("group-a", with_embeddings=False)
    units[0].summary = "新事实"
    store.save("group-a", units)

    assert store.load("group-a")[0].embedding is None
    assert _rows(tmp_path, "SELECT COUNT(*) FROM memory_unit_vectors") == [(0,)]


def test_a_commit_only_rewrites_the_units_that_changed(tmp_path):
    """写入是逐行的：改一条不该重写整组（旧格式每次落盘都整组重写几百 MB）。"""
    store = MemoryUnitStore(tmp_path)
    store.save("group-a", [_unit(f"mem-{i}", f"事实 {i}") for i in range(5)])
    before = dict(
        _rows(tmp_path, "SELECT unit_id, updated_at || '/' || revision FROM memory_units")
    )

    units = store.load("group-a", with_embeddings=False)
    units[2].summary = "改过的事实"
    store.save("group-a", units)

    after = dict(_rows(tmp_path, "SELECT unit_id, updated_at || '/' || revision FROM memory_units"))
    changed = {unit_id for unit_id in before if before[unit_id] != after[unit_id]}
    assert changed == {"mem-2"}
    assert [u.summary for u in store.load("group-a")][2] == "改过的事实"


def test_order_within_a_group_is_preserved(tmp_path):
    store = MemoryUnitStore(tmp_path)
    store.save("group-a", [_unit("b", "B"), _unit("a", "A"), _unit("c", "C")])
    assert [u.unit_id for u in store.load("group-a")] == ["b", "a", "c"]

    store.save("group-a", [_unit("c", "C"), _unit("b", "B")])
    assert [u.unit_id for u in store.load("group-a")] == ["c", "b"]


def test_prompt_only_load_skips_retired_units(tmp_path):
    store = MemoryUnitStore(tmp_path)
    store.save(
        "group-a",
        [_unit("live", "活跃"), _unit("retired", "退休", should_prompt=False)],
    )

    assert [u.unit_id for u in store.load("group-a", prompt_only=True)] == ["live"]
    assert store.source_ids("group-a") == {"src-live", "src-retired"}


def test_cold_vectors_come_only_from_overflow_retired_units(tmp_path):
    """冷层只收超额退休的单元；真人手动隐藏、模型判定不注入、已过期的都不收。"""
    store = MemoryUnitStore(tmp_path)
    store.save(
        "group-a",
        [
            _unit("live", "活跃", embedding=[1.0, 0.0]),
            _unit(
                "overflow",
                "超额",
                should_prompt=False,
                metadata={"retired": "overflow"},
                embedding=[0.0, 1.0],
            ),
            _unit(
                "manual",
                "手动",
                should_prompt=False,
                metadata={"retired": "manual"},
                embedding=[0.5, 0.5],
            ),
        ],
    )

    units, matrix = store.read_retired_vectors("group-a")

    assert [u.unit_id for u in units] == ["overflow"]
    assert matrix.tolist() == [[0.0, 1.0]]


def test_reading_a_persona_without_a_database_does_not_create_one(tmp_path):
    store = MemoryUnitStore(tmp_path)

    assert store.list_group_ids() == []
    assert store.load("group-a") == []
    assert store.count() == 0
    assert not (tmp_path / "persona.db").exists()


def test_a_failed_transaction_changes_nothing(tmp_path):
    store = MemoryUnitStore(tmp_path)
    store.save("group-a", [_unit("mem-1", "A")])

    with pytest.raises(RuntimeError):
        with store.transaction() as tx:
            tx.upsert("group-a", _unit("mem-2", "B"))
            tx.delete("group-a", "mem-1")
            raise RuntimeError("boom")

    assert [u.unit_id for u in store.load("group-a")] == ["mem-1"]
