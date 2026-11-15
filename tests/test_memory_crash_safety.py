"""记忆在异常情况下不丢：进程被强杀、单元文件损坏、WebUI 与后台整理同时改同一个群。"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from sirius_pulse.core.engine_persistence import EnginePersistence, EngineStateStore
from sirius_pulse.memory.basic import BasicMemoryFileStore, BasicMemoryManager
from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.memory.units import MemoryUnit, MemoryUnitManager, MemoryUnitStore
from sirius_pulse.memory.units.deduplicator import DedupVerdict
from sirius_pulse.memory.units.store import MemoryUnitStoreError
from sirius_pulse.webui.memory_api import (
    api_persona_memory_unit_delete,
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


def _entry(entry_id: str, content: str, timestamp: str, group_id: str = "g1") -> BasicMemoryEntry:
    return BasicMemoryEntry(
        entry_id=entry_id,
        group_id=group_id,
        user_id="u1",
        role="human",
        content=content,
        timestamp=timestamp,
    )


def _request(body: dict | None = None, *, match: dict | None = None):
    async def json_body():
        return body or {}

    return SimpleNamespace(json=json_body, query={}, match_info=match or {})


def _summaries(work_path: Path, group_id: str = "g1") -> dict[str, str]:
    return {u.unit_id: u.summary for u in MemoryUnitStore(work_path).load(group_id)}


# ---------------------------------------------------------------------------
# 进程被强杀：上次正常落盘之后的聊天要从归档接回来
# ---------------------------------------------------------------------------


def _restart(work_path: Path, units: MemoryUnitManager | None = None) -> BasicMemoryManager:
    """模拟重启：读快照，再从归档接回快照之后的聊天。"""
    state = EngineStateStore(work_path)
    snapshot = state.load_basic_memory()
    engine = SimpleNamespace(
        basic_memory=BasicMemoryManager.from_dict(snapshot) if snapshot else BasicMemoryManager(),
        basic_store=BasicMemoryFileStore(work_path),
        memory_unit_manager=units or MemoryUnitManager(work_path),
        _state_store=state,
    )
    EnginePersistence(engine).replay_unsaved_entries(state.basic_memory_saved_at())
    return engine.basic_memory


def test_chat_after_the_last_clean_shutdown_survives_a_kill(tmp_path: Path):
    archive = BasicMemoryFileStore(tmp_path)
    window = BasicMemoryManager()
    before = _entry("e1", "早上好", "2026-07-12T08:00:00+00:00")
    window.restore_entries("g1", [before])
    archive.append(before)
    EngineStateStore(tmp_path).save_basic_memory(window.to_dict())  # 上次正常退出

    # 之后又聊了两句，进程随即被强杀：只有归档里有它们。
    for entry in (
        _entry("e2", "今晚去看电影吗", "2026-07-12T09:00:00+00:00"),
        _entry("e3", "七点见", "2026-07-12T09:01:00+00:00"),
    ):
        archive.append(entry)

    restored = _restart(tmp_path)

    assert [e.content for e in restored.get_all("g1")] == ["早上好", "今晚去看电影吗", "七点见"]


def test_restart_does_not_bring_back_chat_already_turned_into_memory(tmp_path: Path):
    archive = BasicMemoryFileStore(tmp_path)
    EngineStateStore(tmp_path).save_basic_memory({"g1": []})  # 上次正常退出时窗口是空的
    saved_at = datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc).timestamp()
    os.utime(tmp_path / "engine_state" / "basic_memory.json", (saved_at, saved_at))
    for entry in (
        _entry("e1", "我下周搬家", "2026-07-12T09:00:00+00:00"),
        _entry("e2", "需要帮忙吗", "2026-07-12T09:01:00+00:00"),
    ):
        archive.append(entry)
    units = MemoryUnitManager(tmp_path)
    asyncio.run(units.add_units("g1", [_unit("m1", "她下周搬家", source_ids=["e1"])]))

    restored = _restart(tmp_path, units)

    assert [e.entry_id for e in restored.get_all("g1")] == ["e2"]


def test_clean_restart_restores_nothing_extra(tmp_path: Path):
    archive = BasicMemoryFileStore(tmp_path)
    window = BasicMemoryManager()
    for entry in (
        _entry("e1", "a", "2026-07-12T08:00:00+00:00"),
        _entry("e2", "b", "2026-07-12T08:01:00+00:00"),
    ):
        archive.append(entry)
        window.restore_entries("g1", [entry])
    EngineStateStore(tmp_path).save_basic_memory(window.to_dict())

    restored = _restart(tmp_path)

    assert [e.entry_id for e in restored.get_all("g1")] == ["e1", "e2"]


# ---------------------------------------------------------------------------
# 记忆读不出来时，绝不能当成「没有记忆」，更不能拿「空」去覆盖
# ---------------------------------------------------------------------------


def test_a_corrupt_legacy_unit_file_is_kept_aside_instead_of_lost(tmp_path: Path):
    """升级前断电留下的半截 JSON：导不进来，但原样留进备份，其它群照常迁移。"""
    legacy = tmp_path / "memory_units"
    legacy.mkdir()
    (legacy / "g1.json").write_text('{"group_id": "g1", "units": [', encoding="utf-8")
    (legacy / "g2.json").write_text(
        '{"group_id": "g2", "units": [{"unit_id": "m1", "summary": "她养了一只猫"}]}',
        encoding="utf-8",
    )

    store = MemoryUnitStore(tmp_path)
    assert store.load("g1") == []
    assert _summaries(tmp_path, "g2") == {"m1": "她养了一只猫"}

    kept = list((tmp_path / "backups" / "memory_units_json").glob("*/g1.json"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8").startswith('{"group_id"')
    store.save("g1", [_unit("m2", "她喜欢下雨天")])
    assert _summaries(tmp_path) == {"m2": "她喜欢下雨天"}


def _break_database(work_path: Path) -> bytes:
    """把 persona.db 换成一个读不出来的文件（磁盘损坏、被别的程序覆盖）。"""
    for suffix in ("-wal", "-shm"):
        (work_path / f"persona.db{suffix}").unlink(missing_ok=True)
    garbage = b"not a sqlite database" * 64
    (work_path / "persona.db").write_bytes(garbage)
    return garbage


def test_an_unreadable_database_is_never_read_as_an_empty_group(tmp_path: Path):
    MemoryUnitStore(tmp_path).save("g1", [_unit("m1", "她养了一只猫")])
    garbage = _break_database(tmp_path)

    with pytest.raises(MemoryUnitStoreError):
        MemoryUnitStore(tmp_path).load("g1")

    # 聊天侧降级为本轮不召回，而不是报错；也不会把这个群标成「已加载、没有记忆」。
    manager = MemoryUnitManager(tmp_path)
    assert manager.retrieve("猫", group_id="g1") == []
    assert "g1" not in manager._loaded_groups
    with pytest.raises(MemoryUnitStoreError):
        asyncio.run(manager.add_units("g1", [_unit("m2", "她喜欢下雨天")]))
    assert (tmp_path / "persona.db").read_bytes() == garbage


@pytest.mark.asyncio
async def test_webui_refuses_to_add_a_unit_when_memory_is_unreadable(tmp_path: Path):
    MemoryUnitStore(tmp_path).save("g1", [_unit("m1", "她养了一只猫")])
    garbage = _break_database(tmp_path)

    response = await api_persona_memory_units_post(
        _request({"group_id": "g1", "summary": "她喜欢下雨天"}), tmp_path
    )

    assert response.status == 503
    assert (tmp_path / "persona.db").read_bytes() == garbage


# ---------------------------------------------------------------------------
# 后台整理（去重要等 LLM）期间，WebUI 里的人工修改不能被覆盖
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webui_edits_made_during_a_checkpoint_survive_it(tmp_path: Path):
    manager = MemoryUnitManager(tmp_path)
    await manager.add_units(
        "g1",
        [
            _unit("keep", "她养了一只猫"),
            _unit("edit", "她住在北京"),
            _unit("gone", "她讨厌香菜"),
        ],
    )
    deciding = asyncio.Event()
    resume = asyncio.Event()

    class _SlowDeduplicator:
        async def decide(self, incoming, existing, indexer, *, brain, model_name):
            deciding.set()
            await resume.wait()  # 去重在等模型
            return DedupVerdict(decision="NEW")

    manager._deduplicator = _SlowDeduplicator()
    checkpoint = asyncio.create_task(
        manager.reconcile_units("g1", [_unit("new", "她下周搬家")], brain=object(), model_name="memory")
    )
    await deciding.wait()

    # 这时有人在 WebUI 里改了一条、删了一条。
    put = await api_persona_memory_unit_put(
        _request({"summary": "她搬到了上海"}, match={"unit_id": "edit"}), tmp_path
    )
    delete = await api_persona_memory_unit_delete(_request(match={"unit_id": "gone"}), tmp_path)
    assert (put.status, delete.status) == (200, 200)

    resume.set()
    await checkpoint

    assert _summaries(tmp_path) == {
        "keep": "她养了一只猫",
        "edit": "她搬到了上海",
        "new": "她下周搬家",
    }
    in_memory = {u.unit_id: u.summary for u in manager.get_units_for_group("g1")}
    assert in_memory == _summaries(tmp_path)
