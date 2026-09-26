"""记忆单元软退休：模型侧不召回，真人侧仍可追溯。

硬约束：退休只翻转 should_prompt，从不删除单元。单元必须留在磁盘与 WebUI 中，
仍可被真人检索、编辑、手动恢复。
"""

from __future__ import annotations

import asyncio

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
