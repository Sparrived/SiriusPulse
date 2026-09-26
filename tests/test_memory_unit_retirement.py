"""记忆单元软退休：模型侧不召回，真人侧仍可追溯。

硬约束：退休只翻转 should_prompt，从不删除单元。单元必须留在磁盘与 WebUI 中，
仍可被真人检索、编辑、手动恢复。
"""

from __future__ import annotations

import asyncio
import json

from sirius_pulse.memory.units import MemoryUnit, MemoryUnitFileStore, MemoryUnitManager

_PAST = "2020-01-01T00:00:00+00:00"


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


def test_retired_units_stay_on_disk_and_loadable(tmp_path, monkeypatch):
    monkeypatch.setattr("sirius_pulse.memory.units.manager.DEFAULT_MEMORY_UNIT_ACTIVE_LIMIT", 2)
    manager = MemoryUnitManager(tmp_path)
    asyncio.run(
        manager.add_units(
            "group-a",
            [
                _unit("mem-1", "第一条部署记录。", keywords=["部署"], salience=0.9, confidence=0.9),
                _unit("mem-2", "第二条部署记录。", keywords=["部署"], salience=0.8, confidence=0.8),
                _unit("mem-3", "第三条部署记录。", keywords=["部署"], salience=0.1, confidence=0.1),
            ],
        )
    )

    manager.retire_overflow("group-a", manager.get_units_for_group("group-a"))
    manager._store.save("group-a", manager.get_units_for_group("group-a"))

    on_disk = MemoryUnitFileStore(tmp_path).load("group-a")
    assert len(on_disk) == 3, "退休不得删除记忆单元"
    retired = [unit for unit in on_disk if not unit.should_prompt]
    assert [unit.unit_id for unit in retired] == ["mem-3"]


def test_expired_units_are_retired_but_retained(tmp_path):
    manager = MemoryUnitManager(tmp_path)
    asyncio.run(
        manager.add_units(
            "group-a",
            [_unit("mem-dead", "下周三开评审会。", valid_until=_PAST, keywords=["评审会"])],
        )
    )

    manager.ensure_group_loaded("group-a")
    assert manager.retrieve("评审会", group_id="group-a", top_k=5) == []

    on_disk = MemoryUnitFileStore(tmp_path).load("group-a")
    assert [unit.unit_id for unit in on_disk] == ["mem-dead"]
    assert on_disk[0].should_prompt is False


def test_add_units_syncs_retirement_into_index(tmp_path):
    """add_units 重新加载过对象，退休标记必须同步进索引，否则仍会被召回。"""
    manager = MemoryUnitManager(tmp_path)
    asyncio.run(
        manager.add_units(
            "group-a",
            [_unit("mem-dead", "下周三开评审会。", valid_until=_PAST, keywords=["评审会"])],
        )
    )

    assert manager.retrieve("评审会", group_id="group-a", top_k=5) == []


def test_webui_edit_does_not_resurrect_retired_unit():
    """真人编辑退休单元时不能被 should_prompt 默认值悄悄复活。"""
    from sirius_pulse.webui.memory_api import _normalize_memory_unit

    normalized = _normalize_memory_unit(
        {"unit_id": "mem-1", "summary": "旧事实", "should_prompt": False}
    )

    assert normalized["should_prompt"] is False


def test_retired_unit_still_counts_as_an_existing_fact(tmp_path):
    """退休只是不再注入，不代表同一条事实可以被重新写一遍。

    常驻加载为了省内存不给退休单元带向量；若去重因此看不到它，重复事实会被当作
    全新单元再写一次，记忆库会缓慢长回 500 条上限之外。
    """

    class _StubEmbedding:
        available = True
        dimension = 4
        model = "stub"

        def encode(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        def encode_single(self, text):
            return [1.0, 0.0, 0.0, 0.0]

    class _DupBrain:
        async def raw_call(self, request):
            return json.dumps({"decision": "DUPLICATE", "target_unit_id": "mem-old"})

    writer = MemoryUnitManager(tmp_path, embedding_client=_StubEmbedding())
    asyncio.run(
        writer.add_units(
            "group-a",
            [_unit("mem-old", "Alice 偏好简洁回复。", keywords=["简洁", "偏好"])],
        )
    )
    loaded = writer.get_units_for_group("group-a")
    for unit in loaded:
        unit.should_prompt = False
    writer._store.save("group-a", loaded)

    # 新进程：常驻加载只带元数据，退休单元没有向量。
    fresh = MemoryUnitManager(tmp_path, embedding_client=_StubEmbedding())
    fresh.ensure_group_loaded("group-a")
    assert {u.unit_id: u.embedding for u in fresh._indexer.list_all()}["mem-old"] is None

    # 语义等价但文字不同的新单元必须仍被认出是「已存在的事实」。
    asyncio.run(
        fresh.reconcile_units(
            "group-a",
            [_unit("mem-new", "Alice 更喜欢简短的回答。", keywords=["简洁", "偏好"])],
            brain=_DupBrain(),
            model_name="stub",
        )
    )

    on_disk = MemoryUnitFileStore(tmp_path).load("group-a")
    assert len(on_disk) == 1, f"退休单元没有挡住重复事实: {[u.summary for u in on_disk]}"


def test_checkpoint_releases_retired_vectors_from_the_index(tmp_path):
    """一次 checkpoint 之后退休单元的向量必须离开内存索引。

    去重路径会把全组向量读进索引才能比对；若写完就留在那里，整组向量又会重新常驻，
    前面省下的内存等于白省。退休单元的向量留在磁盘即可，需要时按需读回。
    """

    class _StubEmbedding:
        available = True
        dimension = 4
        model = "stub"

        def encode(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        def encode_single(self, text):
            return [1.0, 0.0, 0.0, 0.0]

    class _NewBrain:
        async def raw_call(self, request):
            return json.dumps({"decision": "NEW"})

    manager = MemoryUnitManager(tmp_path, embedding_client=_StubEmbedding())
    active = _unit("mem-active", "活跃事实。", keywords=["活跃"], embedding=[1.0, 0, 0, 0])
    retired = _unit(
        "mem-retired",
        "退休事实。",
        keywords=["退休"],
        should_prompt=False,
        embedding=[0.5, 0, 0, 0],
    )
    asyncio.run(manager.add_units("group-a", [active, retired]))

    asyncio.run(
        manager.reconcile_units(
            "group-a",
            [_unit("mem-new", "另一条事实。", keywords=["新"])],
            brain=_NewBrain(),
            model_name="stub",
        )
    )

    indexed = {unit.unit_id: unit for unit in manager._indexer.list_all()}
    assert indexed["mem-retired"].embedding is None, "退休向量必须离开内存索引"
    assert indexed["mem-active"].embedding is not None, "活跃单元仍要能语义检索"

    # 磁盘那一份必须原样保留：真人追溯与后续去重都依赖它。
    on_disk = {unit.unit_id: unit for unit in MemoryUnitFileStore(tmp_path).load("group-a")}
    assert on_disk["mem-retired"].embedding is not None
