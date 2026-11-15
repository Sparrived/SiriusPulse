"""引擎后台整理与 WebUI 人工编辑同时改同一个群：谁的改动都不能丢。

引擎去重要 await 好几次模型才写库；这期间真人可能在 WebUI 里新建、改、删、换群。
规则：不同单元互不影响；同一条单元两边都改时保留真人的版本。
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from sirius_pulse.memory.units import MemoryUnit, MemoryUnitManager, MemoryUnitStore
from sirius_pulse.memory.units.deduplicator import DedupVerdict
from sirius_pulse.webui.memory_api import (
    _create_memory_unit,
    _update_memory_unit,
    api_persona_memory_unit_put,
    api_persona_memory_units_post,
)


def _unit(unit_id: str, summary: str, group_id: str = "g1", **extra) -> MemoryUnit:
    return MemoryUnit(
        unit_id=unit_id,
        group_id=group_id,
        created_at="2026-07-12T00:00:00+00:00",
        summary=summary,
        **extra,
    )


def _request(body: dict | None = None, *, match: dict | None = None):
    async def json_body():
        return body or {}

    return SimpleNamespace(json=json_body, query={}, match_info=match or {})


def _on_disk(work_path: Path, group_id: str = "g1") -> dict[str, MemoryUnit]:
    return {u.unit_id: u for u in MemoryUnitStore(work_path).load(group_id)}


class _PausingDeduplicator:
    """去重判定停在「等模型」那一刻，让测试在这个窗口里做 WebUI 编辑。"""

    def __init__(self, verdict: DedupVerdict) -> None:
        self.verdict = verdict
        self.deciding = asyncio.Event()
        self.resume = asyncio.Event()

    async def decide(self, incoming, existing, indexer, *, brain, model_name):
        self.deciding.set()
        await self.resume.wait()
        return self.verdict


async def _checkpoint_with_pause(manager, verdict, incoming, edit):
    dedupe = _PausingDeduplicator(verdict)
    manager._deduplicator = dedupe
    task = asyncio.create_task(
        manager.reconcile_units("g1", [incoming], brain=object(), model_name="memory")
    )
    await dedupe.deciding.wait()
    await edit()
    dedupe.resume.set()
    await task


@pytest.mark.asyncio
async def test_webui_create_move_and_hide_during_a_checkpoint_all_survive(tmp_path: Path):
    manager = MemoryUnitManager(tmp_path)
    await manager.add_units(
        "g1", [_unit("move", "她在北京上班"), _unit("hide", "她讨厌香菜"), _unit("keep", "她养猫")]
    )

    async def edit():
        created = await api_persona_memory_units_post(
            _request({"group_id": "g1", "unit_id": "made", "summary": "她周末去爬山"}), tmp_path
        )
        moved = await api_persona_memory_unit_put(
            _request({"group_id": "g2"}, match={"unit_id": "move"}), tmp_path
        )
        hidden = await api_persona_memory_unit_put(
            _request({"should_prompt": False}, match={"unit_id": "hide"}), tmp_path
        )
        assert (created.status, moved.status, hidden.status) == (201, 200, 200)

    await _checkpoint_with_pause(manager, DedupVerdict(decision="NEW"), _unit("new", "她下周搬家"), edit)

    g1 = _on_disk(tmp_path, "g1")
    assert set(g1) == {"hide", "keep", "made", "new"}
    assert g1["hide"].should_prompt is False
    assert g1["hide"].metadata.get("retired") == "manual"
    assert set(_on_disk(tmp_path, "g2")) == {"move"}
    # 引擎内存里的索引与库里一致：被隐藏、被移走的不再参与本群注入。
    indexed = {u.unit_id for u in manager._indexer.list_all() if u.group_id == "g1"}
    assert indexed == {"keep", "made", "new"}


@pytest.mark.asyncio
async def test_when_both_sides_edit_the_same_unit_the_human_edit_wins(tmp_path: Path):
    manager = MemoryUnitManager(tmp_path)
    await manager.add_units("g1", [_unit("cat", "她养了一只猫")])

    async def edit():
        response = await api_persona_memory_unit_put(
            _request({"summary": "她养了一只橘猫，叫年糕"}, match={"unit_id": "cat"}), tmp_path
        )
        assert response.status == 200

    # 引擎这边判定新单元与「cat」矛盾，要在「cat」这一行上记下冲突关系。
    await _checkpoint_with_pause(
        manager,
        DedupVerdict(decision="CONFLICT", target_unit_id="cat", reason="数量不一致"),
        _unit("incoming", "她没有养猫"),
        edit,
    )

    on_disk = _on_disk(tmp_path)
    assert on_disk["cat"].summary == "她养了一只橘猫，叫年糕", "真人的修改不能被引擎的旧副本覆盖"
    assert "incoming" in on_disk, "引擎这次记下的新事实照常落库"
    in_memory = {u.unit_id: u.summary for u in manager.get_units_for_group("g1")}
    assert in_memory == {u: x.summary for u, x in on_disk.items()}


@pytest.mark.asyncio
async def test_a_unit_deleted_in_the_webui_is_not_resurrected_by_a_checkpoint(tmp_path: Path):
    from sirius_pulse.webui.memory_api import api_persona_memory_unit_delete

    manager = MemoryUnitManager(tmp_path)
    await manager.add_units("g1", [_unit("gone", "她讨厌香菜"), _unit("keep", "她养猫")])

    async def edit():
        response = await api_persona_memory_unit_delete(
            _request(match={"unit_id": "gone"}), tmp_path
        )
        assert response.status == 200

    # 合并目标恰好是被删掉的那条：引擎手里的旧副本不能把它写回来。
    await _checkpoint_with_pause(
        manager,
        DedupVerdict(decision="MERGE", target_unit_id="gone", merged_summary="她很讨厌香菜"),
        _unit("incoming", "她不吃香菜"),
        edit,
    )

    on_disk = _on_disk(tmp_path)
    assert "gone" not in on_disk
    # 合并结果以新单元的 ID 落库，它本身是一条新事实，照常保留。
    assert set(on_disk) == {"keep", "incoming"}


def test_engine_and_webui_writing_from_threads_lose_nothing(tmp_path: Path):
    """引擎线程不停追加单元，WebUI 线程同时新建、修改：结束时每一笔都在。"""
    manager = MemoryUnitManager(tmp_path)
    asyncio.run(manager.add_units("g1", [_unit("shared", "初始内容")]))
    errors: list[BaseException] = []

    def engine() -> None:
        try:

            async def run() -> None:
                for i in range(15):
                    await manager.add_units("g1", [_unit(f"engine-{i}", f"引擎写的第 {i} 条")])

            asyncio.run(run())
        except BaseException as exc:  # pragma: no cover - 失败时报告
            errors.append(exc)

    def webui() -> None:
        try:
            for i in range(15):
                payload, status = _create_memory_unit(
                    tmp_path,
                    {
                        "unit_id": f"webui-{i}",
                        "group_id": "g1",
                        "created_at": "2026-07-12T00:00:00+00:00",
                        "summary": f"真人写的第 {i} 条",
                    },
                )
                assert status == 201, payload
                payload, status = _update_memory_unit(
                    tmp_path, "shared", {"summary": f"真人改了 {i} 次"}
                )
                assert status == 200, payload
        except BaseException as exc:  # pragma: no cover - 失败时报告
            errors.append(exc)

    threads = [threading.Thread(target=engine), threading.Thread(target=webui)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    on_disk = _on_disk(tmp_path)
    assert {f"engine-{i}" for i in range(15)} <= set(on_disk)
    assert {f"webui-{i}" for i in range(15)} <= set(on_disk)
    assert on_disk["shared"].summary == "真人改了 14 次"
    queued = json.loads(
        (tmp_path / "engine_state" / "memory_dedupe" / "reconcile.json").read_text("utf-8")
    )
    assert "webui-14" in queued["unit_ids"]
