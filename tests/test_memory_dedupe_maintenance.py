import asyncio

from sirius_pulse.memory.units import MemoryUnit, MemoryUnitManager


def _unit(unit_id, group_id, summary):
    return MemoryUnit(
        unit_id=unit_id,
        group_id=group_id,
        created_at="2026-07-12T00:00:00+00:00",
        unit_type="preference",
        scope="user",
        scope_id="alice",
        summary=summary,
        source_ids=[unit_id],
    )


class _Brain:
    async def raw_call(self, request):
        raise AssertionError(f"unexpected model call: {request.purpose}")


def _manager(tmp_path):
    manager = MemoryUnitManager(tmp_path)
    manager.add_units(
        "group-a",
        [
            _unit("one", "group-a", "Alice prefers concise replies."),
            _unit("two", "group-a", "Alice prefers concise replies!"),
            _unit("three", "group-a", "Alice prefers concise replies。"),
        ],
    )
    manager.add_units("group-b", [_unit("four", "group-b", "Alice prefers concise replies.")])
    return manager


def test_scan_is_dry_run_and_apply_creates_backup(tmp_path):
    manager = _manager(tmp_path)

    report = asyncio.run(manager.scan_duplicates(brain=_Brain(), model_name="memory-model"))

    assert len(manager.get_units_for_group("group-a")) == 3
    assert report["summary"]["exact_duplicate"] == 2
    assert len(report["groups"]["group-a"]["final_units"]) == 1

    result = asyncio.run(manager.apply_duplicate_report(report))

    assert result["status"] == "completed"
    assert len(manager.get_units_for_group("group-a")) == 1
    assert list((tmp_path / "backups" / "memory_units").glob("*/group-a.json"))


def test_apply_rejects_stale_report_without_changing_files(tmp_path):
    manager = _manager(tmp_path)
    report = asyncio.run(manager.scan_duplicates(brain=_Brain(), model_name="memory-model"))
    manager.add_units("group-a", [_unit("four", "group-a", "A separate fact.")])
    before = manager.get_units_for_group("group-a")

    result = asyncio.run(manager.apply_duplicate_report(report))

    assert result == {"status": "stale"}
    assert manager.get_units_for_group("group-a") == before


def test_apply_rolls_back_all_groups_when_staged_replace_fails(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    report = asyncio.run(manager.scan_duplicates(brain=_Brain(), model_name="memory-model"))
    before_a = manager.get_units_for_group("group-a")
    before_b = manager.get_units_for_group("group-b")
    original = manager._store._replace_staged
    calls = 0

    def fail_second(staged, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk failed")
        original(staged, destination)

    monkeypatch.setattr(manager._store, "_replace_staged", fail_second)
    result = asyncio.run(manager.apply_duplicate_report(report))

    assert result["status"] == "failed"
    assert manager.get_units_for_group("group-a") == before_a
    assert manager.get_units_for_group("group-b") == before_b
