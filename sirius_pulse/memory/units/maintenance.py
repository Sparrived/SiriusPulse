"""Previewable historical memory-unit deduplication."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from sirius_pulse.embedding.client import EmbeddingClient
from sirius_pulse.memory.units.deduplicator import MemoryUnitDeduplicator, apply_verdict
from sirius_pulse.memory.units.indexer import MemoryUnitIndexer
from sirius_pulse.memory.units.models import MemoryUnit
from sirius_pulse.memory.units.store import MemoryUnitFileStore

if TYPE_CHECKING:
    from sirius_pulse.memory.units.manager import MemoryUnitManager


class MemoryUnitDedupeMaintenance:
    """Scans persisted units without mutation and applies verified reports."""

    def __init__(
        self,
        manager: "MemoryUnitManager",
        store: MemoryUnitFileStore,
        embedding_client: EmbeddingClient | None,
        deduplicator: MemoryUnitDeduplicator,
    ) -> None:
        self._manager = manager
        self._store = store
        self._embedding_client = embedding_client
        self._deduplicator = deduplicator

    @staticmethod
    def fingerprint(units: list[MemoryUnit]) -> str:
        payload = json.dumps(
            [unit.to_dict() for unit in units],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    async def scan(
        self,
        *,
        brain: Any,
        model_name: str,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        group_ids = self._store.list_group_ids()
        total = sum(len(self._store.load(group_id)) for group_id in group_ids)
        processed = 0
        summary = {"new": 0, "exact_duplicate": 0, "duplicate": 0, "merge": 0, "conflict": 0}
        groups: dict[str, Any] = {}
        for group_id in group_ids:
            originals = self._store.load(group_id)
            working: list[MemoryUnit] = []
            indexer = MemoryUnitIndexer(self._embedding_client)
            operations: list[dict[str, Any]] = []
            for incoming in sorted(originals, key=lambda unit: (unit.created_at, unit.unit_id)):
                verdict = await self._deduplicator.decide(
                    incoming, working, indexer, brain=brain, model_name=model_name
                )
                target = next((unit for unit in working if unit.unit_id == verdict.target_unit_id), None)
                working, accepted = apply_verdict(
                    working, incoming, verdict, now_iso=datetime.now(timezone.utc).isoformat()
                )
                indexer.replace_group(group_id, working)
                summary_key = (
                    "exact_duplicate"
                    if verdict.decision == "DUPLICATE" and verdict.reason == "normalized exact match"
                    else verdict.decision.lower()
                )
                summary[summary_key] += 1
                operations.append(
                    {
                        "incoming_unit_id": incoming.unit_id,
                        "incoming_summary": incoming.summary,
                        "target_unit_id": verdict.target_unit_id,
                        "target_summary": target.summary if target else "",
                        "result_unit_id": accepted.unit_id,
                        "result_summary": accepted.summary,
                        "decision": verdict.decision,
                        "reason": verdict.reason,
                    }
                )
                processed += 1
                if progress:
                    progress(processed, total)
            groups[group_id] = {
                "fingerprint": self.fingerprint(originals),
                "operations": operations,
                "final_units": [unit.to_dict() for unit in working],
            }
        return {"summary": summary, "groups": groups, "total": total}

    async def apply(self, report: dict[str, Any]) -> dict[str, Any]:
        report_groups = dict(report.get("groups") or {})
        current = {group_id: self._store.load(group_id) for group_id in report_groups}
        if any(
            self.fingerprint(current[group_id]) != group_report.get("fingerprint")
            for group_id, group_report in report_groups.items()
        ):
            return {"status": "stale"}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = self._store.base_dir.parent / "backups" / "memory_units" / stamp
        backup_dir.parent.mkdir(parents=True, exist_ok=True)
        if self._store.base_dir.exists():
            shutil.copytree(self._store.base_dir, backup_dir)
        else:
            backup_dir.mkdir()
        prepared = {
            group_id: [MemoryUnit.from_dict(item) for item in group_report.get("final_units", [])]
            for group_id, group_report in report_groups.items()
        }
        try:
            self._store.save_many_atomically(prepared)
            for group_id, units in prepared.items():
                self._manager._replace_loaded_group(group_id, units)
        except Exception as exc:
            self._store.save_many_atomically(current)
            for group_id, units in current.items():
                self._manager._replace_loaded_group(group_id, units)
            return {"status": "failed", "error": str(exc), "backup": str(backup_dir)}
        return {"status": "completed", "backup": str(backup_dir)}
