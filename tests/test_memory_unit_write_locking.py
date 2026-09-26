"""记忆单元写入互斥。

`add_units()` 是无锁的「读盘 → 追加 → 写盘」读改写，而 checkpoint 的
`reconcile_units()` 会在持锁期间 await 去重判定。两者交错时后写的一方覆盖
前一方，表现为静默丢单元——这里锁定修复后的行为。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sirius_pulse.memory.units import MemoryUnit, MemoryUnitManager


def _unit(unit_id: str, group_id: str, summary: str = "Alice 偏好简短回复。") -> MemoryUnit:
    return MemoryUnit(
        unit_id=unit_id,
        group_id=group_id,
        created_at="2026-07-12T00:00:00+00:00",
        summary=summary,
    )


def test_add_units_persists_every_unit(tmp_path: Path):
    manager = MemoryUnitManager(tmp_path)

    asyncio.run(
        manager.add_units(
            "group-a",
            [_unit("m1", "group-a"), _unit("m2", "group-a", "Alice 负责部署。")],
        )
    )

    assert [unit.unit_id for unit in manager.get_units_for_group("group-a")] == ["m1", "m2"]


def test_add_units_is_idempotent_for_the_same_unit_id(tmp_path: Path):
    manager = MemoryUnitManager(tmp_path)

    async def main() -> None:
        await manager.add_units("group-a", [_unit("m1", "group-a")])
        await manager.add_units("group-a", [_unit("m1", "group-a")])

    asyncio.run(main())

    assert [unit.unit_id for unit in manager.get_units_for_group("group-a")] == ["m1"]


def test_no_unit_is_lost_when_two_writes_interleave(tmp_path: Path):
    """并发追加都必须落盘，不能有一方覆盖另一方。"""
    manager = MemoryUnitManager(tmp_path)

    async def main() -> None:
        await asyncio.gather(
            manager.add_units("group-a", [_unit("m1", "group-a")]),
            manager.add_units("group-a", [_unit("m2", "group-a")]),
            manager.add_units("group-a", [_unit("m3", "group-a")]),
        )

    asyncio.run(main())

    ids = sorted(unit.unit_id for unit in manager.get_units_for_group("group-a"))
    assert ids == ["m1", "m2", "m3"]


def test_checkpoint_reconcile_does_not_drop_a_concurrent_add(tmp_path: Path):
    """checkpoint 去重期间到达的新单元必须留存下来。

    `reconcile_units()` 持锁后会 await 去重判定；`add_units()` 做的是读改写。
    这里用一次真实的让出把窗口撑开：无锁时 checkpoint 的最终写盘会覆盖掉
    期间追加的单元（实测只剩 gen-1），有锁时两者都在。
    """
    from sirius_pulse.memory.units.deduplicator import DedupVerdict

    class _SlowDeduplicator:
        async def decide(self, incoming, existing, indexer, *, brain, model_name):
            await asyncio.sleep(0.05)
            return DedupVerdict(decision="NEW")

    class _Brain:
        async def raw_call(self, request):
            raise AssertionError("本用例不应触发模型调用")

    manager = MemoryUnitManager(tmp_path)
    manager._deduplicator = _SlowDeduplicator()

    async def main() -> None:
        await asyncio.gather(
            manager.reconcile_units(
                "group-a",
                [_unit("gen-1", "group-a", "由 checkpoint 生成的单元。")],
                brain=_Brain(),
                model_name="memory-model",
            ),
            manager.add_units("group-a", [_unit("manual-1", "group-a", "并发追加的单元。")]),
        )

    asyncio.run(main())

    ids = sorted(unit.unit_id for unit in manager.get_units_for_group("group-a"))
    assert ids == ["gen-1", "manual-1"], "并发的 add_units 被 checkpoint 覆盖丢失"
