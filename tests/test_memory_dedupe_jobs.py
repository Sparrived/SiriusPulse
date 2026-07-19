import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sirius_pulse.core.bg_tasks import BackgroundTasks
from sirius_pulse.memory.basic import BasicMemoryManager
from sirius_pulse.memory.cold_detector import ColdDetector
from sirius_pulse.utils.json_io import atomic_write_json


class _Manager:
    def __init__(self, result=None, error=None):
        self.result = result or {"summary": {}, "groups": {}, "total": 0}
        self.error = error
        self.reconciled = None

    async def scan_duplicates(self, **kwargs):
        if self.error:
            raise self.error
        return self.result

    async def apply_duplicate_report(self, report):
        return {"status": "stale"}

    async def reconcile_persisted_units(self, group_ids, unit_ids, **kwargs):
        self.reconciled = (group_ids, unit_ids)


class _CheckpointManager:
    def __init__(self, checkpointed=None):
        self.checkpointed = set(checkpointed or [])
        self.generated_candidates = []
        self.generated_batches = []

    def is_source_checkpointed(self, _group_id, entry_id):
        return entry_id in self.checkpointed

    async def generate_from_candidates(self, **kwargs):
        candidates = list(kwargs["candidates"])
        self.generated_candidates = candidates
        self.generated_batches.append(candidates)
        return SimpleNamespace(
            units=[SimpleNamespace(source_ids=[entry.entry_id for entry in candidates])]
        )


def _tasks(tmp_path, manager):
    engine = SimpleNamespace(
        work_path=tmp_path,
        memory_unit_manager=manager,
        model_router=SimpleNamespace(resolve=lambda _: SimpleNamespace(model_name="memory-model")),
        brain=object(),
        _bg_running=False,
    )
    return BackgroundTasks(engine)


def test_scan_job_writes_ready_status_and_report(tmp_path):
    tasks = _tasks(tmp_path, _Manager())
    job_dir = tmp_path / "engine_state" / "memory_dedupe"
    job_dir.mkdir(parents=True)
    atomic_write_json(job_dir / "request.json", {"action": "scan", "job_id": "job-1"})

    asyncio.run(tasks._process_memory_dedupe_request_once())

    status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert status["job_id"] == "job-1"
    assert status["status"] == "ready"
    assert Path(status["report_path"]).name == "job-1.json"


def test_apply_and_reconcile_preserve_historical_status(tmp_path):
    manager = _Manager()
    tasks = _tasks(tmp_path, manager)
    job_dir = tmp_path / "engine_state" / "memory_dedupe"
    job_dir.mkdir(parents=True)
    atomic_write_json(job_dir / "status.json", {"job_id": "job-1", "status": "ready"})
    atomic_write_json(job_dir / "reconcile.json", {"group_ids": ["g"], "unit_ids": ["u"]})

    asyncio.run(tasks._process_memory_dedupe_request_once())

    assert manager.reconciled == (["g"], ["u"])
    assert json.loads((job_dir / "status.json").read_text(encoding="utf-8"))["status"] == "ready"


def test_checkpoint_pass_prunes_covered_sources_and_consolidates_old_active_archive():
    now = datetime.now(timezone.utc)
    basic = BasicMemoryManager(context_window=5)
    old_entries = [
        basic.add_entry(
            "group_a",
            "alice",
            "human",
            f"old message {index}",
            timestamp=(now - timedelta(hours=2)).isoformat(),
        )
        for index in range(40)
    ]
    basic.add_entry(
        "group_a",
        "alice",
        "human",
        "recent archive message",
        timestamp=(now - timedelta(minutes=5)).isoformat(),
    )
    for index in range(5):
        basic.add_entry(
            "group_a",
            "alice",
            "human",
            f"current message {index}",
            timestamp=now.isoformat(),
        )

    manager = _CheckpointManager(checkpointed={old_entries[0].entry_id, old_entries[1].entry_id})
    engine = SimpleNamespace(
        config={
            "memory_idle_consolidation_seconds": 3600,
            "memory_unit_volume_threshold": 8,
            "memory_unit_token_trigger": 1,
            "memory_unit_token_target": 1,
        },
        provider_async=object(),
        basic_memory=basic,
        memory_unit_manager=manager,
        cold_detector=ColdDetector(),
        model_router=SimpleNamespace(resolve=lambda _: SimpleNamespace(model_name="memory-model")),
        persona=SimpleNamespace(name="Sirius", persona_summary="", backstory=""),
        brain=object(),
        _bg_running=False,
    )

    promoted = asyncio.run(BackgroundTasks(engine)._checkpoint_memory_once())

    assert promoted == 1
    assert [entry.content for entry in manager.generated_candidates] == [
        f"old message {index}" for index in range(2, 40)
    ]
    assert [entry.content for entry in basic.get_all("group_a")] == [
        "recent archive message",
        *[f"current message {index}" for index in range(5)],
    ]


def test_checkpoint_pass_repeats_active_batches_until_token_target():
    now = datetime.now(timezone.utc)
    basic = BasicMemoryManager(context_window=5)
    for index in range(80):
        basic.add_entry(
            "group_a",
            "alice",
            "human",
            f"old message {index}",
            timestamp=(now - timedelta(hours=2)).isoformat(),
        )
    for index in range(5):
        basic.add_entry(
            "group_a",
            "alice",
            "human",
            f"current message {index}",
            timestamp=now.isoformat(),
        )

    manager = _CheckpointManager()
    engine = SimpleNamespace(
        config={
            "memory_idle_consolidation_seconds": 3600,
            "memory_unit_volume_threshold": 8,
            "memory_unit_token_trigger": 1,
            "memory_unit_token_target": 100,
        },
        provider_async=object(),
        basic_memory=basic,
        memory_unit_manager=manager,
        cold_detector=ColdDetector(),
        model_router=SimpleNamespace(resolve=lambda _: SimpleNamespace(model_name="memory-model")),
        persona=SimpleNamespace(name="Sirius", persona_summary="", backstory=""),
        brain=object(),
        _bg_running=False,
    )

    promoted = asyncio.run(BackgroundTasks(engine)._checkpoint_memory_once())

    assert promoted == 1
    assert [len(batch) for batch in manager.generated_batches] == [64]
    assert BackgroundTasks(engine)._estimate_group_history_tokens("group_a") <= 100


def test_checkpoint_uses_latest_conversation_chain_tokens_for_trigger():
    now = datetime.now(timezone.utc)
    basic = BasicMemoryManager(context_window=5)
    for index in range(40):
        basic.add_entry(
            "group_a",
            "alice",
            "human",
            f"old message {index}",
            timestamp=(now - timedelta(hours=2)).isoformat(),
        )
    for index in range(5):
        basic.add_entry(
            "group_a",
            "alice",
            "human",
            f"current message {index}",
            timestamp=now.isoformat(),
        )
    basic.add_entry(
        "group_a",
        "assistant",
        "assistant",
        "current reply",
        timestamp=now.isoformat(),
        conversation_chain=[{"role": "system", "content": "x" * 260_000}],
    )

    manager = _CheckpointManager()
    engine = SimpleNamespace(
        config={"memory_idle_consolidation_seconds": 3600, "memory_unit_volume_threshold": 8},
        provider_async=object(),
        basic_memory=basic,
        memory_unit_manager=manager,
        cold_detector=ColdDetector(),
        model_router=SimpleNamespace(resolve=lambda _: SimpleNamespace(model_name="memory-model")),
        persona=SimpleNamespace(name="Sirius", persona_summary="", backstory=""),
        brain=object(),
        _bg_running=False,
    )

    promoted = asyncio.run(BackgroundTasks(engine)._checkpoint_memory_once())

    assert promoted == 1
    assert len(manager.generated_batches) == 1
