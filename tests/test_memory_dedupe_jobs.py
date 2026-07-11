import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from sirius_pulse.core.bg_tasks import BackgroundTasks
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
