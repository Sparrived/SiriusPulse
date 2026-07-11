"""Manager for checkpoint memory units."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from sirius_pulse.embedding.client import EmbeddingClient
from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.memory.units.deduplicator import (
    MemoryUnitDeduplicator,
    apply_verdict,
)
from sirius_pulse.memory.units.generator import MemoryUnitGenerator
from sirius_pulse.memory.units.indexer import MemoryUnitIndexer, MemoryUnitRetriever
from sirius_pulse.memory.units.models import MemoryUnit, MemoryUnitGenerationResult
from sirius_pulse.memory.units.maintenance import MemoryUnitDedupeMaintenance
from sirius_pulse.memory.units.store import MemoryUnitFileStore

logger = logging.getLogger(__name__)


class MemoryUnitManager:
    """High-level lifecycle manager for checkpoint memory units."""

    def __init__(
        self,
        work_path: Any,
        *,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self._store = MemoryUnitFileStore(work_path)
        self._embedding_client = embedding_client
        self._indexer = MemoryUnitIndexer(embedding_client=embedding_client)
        self._retriever = MemoryUnitRetriever(self._indexer)
        self._generator = MemoryUnitGenerator()
        self._deduplicator = MemoryUnitDeduplicator()
        self._maintenance = MemoryUnitDedupeMaintenance(
            self, self._store, embedding_client, self._deduplicator
        )
        self._mutation_lock = asyncio.Lock()
        self._checkpointed_sources: dict[str, set[str]] = {}
        self._loaded_groups: set[str] = set()

    async def generate_from_candidates(
        self,
        *,
        group_id: str,
        candidates: list[BasicMemoryEntry],
        persona_name: str,
        persona_description: str,
        brain: Any,
        model_name: str,
        min_candidate_count: int = 8,
    ) -> MemoryUnitGenerationResult | None:
        if len(candidates) < min_candidate_count:
            logger.debug(
                "Group %s has not enough memory checkpoint candidates (%d < %d)",
                group_id,
                len(candidates),
                min_candidate_count,
            )
            return None

        result = await self._generator.generate(
            group_id=group_id,
            candidates=candidates,
            persona_name=persona_name,
            persona_description=persona_description,
            brain=brain,
            model_name=model_name,
        )
        if result is None or not result.units:
            return None

        canonical_results = await self.reconcile_units(
            group_id,
            result.units,
            brain=brain,
            model_name=model_name,
        )
        return MemoryUnitGenerationResult(units=canonical_results)

    async def reconcile_units(
        self,
        group_id: str,
        units: list[MemoryUnit],
        *,
        brain: Any,
        model_name: str,
    ) -> list[MemoryUnit]:
        """Reconcile generated units against the current group under one write lock."""
        if not units:
            return []
        async with self._mutation_lock:
            self.ensure_group_loaded(group_id)
            existing = self._store.load(group_id)
            accepted: dict[str, MemoryUnit] = {}
            for incoming in units:
                verdict = await self._deduplicator.decide(
                    incoming,
                    existing,
                    self._indexer,
                    brain=brain,
                    model_name=model_name,
                )
                existing, result = apply_verdict(
                    existing,
                    incoming,
                    verdict,
                    now_iso=datetime.now(timezone.utc).isoformat(),
                )
                accepted[result.unit_id] = result
                self._indexer.replace_group(group_id, existing)
            self._store.save(group_id, existing)
            self._replace_loaded_group(group_id, existing)
            return list(accepted.values())

    async def reconcile_persisted_units(
        self,
        group_ids: list[str],
        unit_ids: list[str],
        *,
        brain: Any,
        model_name: str,
    ) -> None:
        """Reconcile selected persisted units after an offline CRUD update."""
        selected_ids = set(unit_ids)
        async with self._mutation_lock:
            for group_id in sorted(set(group_ids)):
                loaded = self._store.load(group_id)
                incoming = [unit for unit in loaded if unit.unit_id in selected_ids]
                working = [unit for unit in loaded if unit.unit_id not in selected_ids]
                self._indexer.replace_group(group_id, working)
                for unit in sorted(incoming, key=lambda item: (item.created_at, item.unit_id)):
                    verdict = await self._deduplicator.decide(
                        unit,
                        working,
                        self._indexer,
                        brain=brain,
                        model_name=model_name,
                    )
                    working, _accepted = apply_verdict(
                        working,
                        unit,
                        verdict,
                        now_iso=datetime.now(timezone.utc).isoformat(),
                    )
                    self._indexer.replace_group(group_id, working)
                self._store.save(group_id, working)
                self._replace_loaded_group(group_id, working)

    async def scan_duplicates(
        self,
        *,
        brain: Any,
        model_name: str,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        return await self._maintenance.scan(brain=brain, model_name=model_name, progress=progress)

    async def apply_duplicate_report(self, report: dict[str, Any]) -> dict[str, Any]:
        async with self._mutation_lock:
            return await self._maintenance.apply(report)

    def add_units(self, group_id: str, units: list[MemoryUnit]) -> None:
        if not units:
            return
        self.ensure_group_loaded(group_id)
        existing = self._store.load(group_id)
        existing_ids = {unit.unit_id for unit in existing}
        changed = False
        for unit in units:
            if unit.unit_id in existing_ids:
                continue
            self._indexer.add(unit)
            existing.append(unit)
            existing_ids.add(unit.unit_id)
            self._checkpointed_sources.setdefault(group_id, set()).update(unit.source_ids)
            changed = True
        if changed:
            self._store.save(group_id, existing)

    def ensure_group_loaded(self, group_id: str) -> None:
        if group_id in self._loaded_groups:
            return
        units = self._store.load(group_id)
        any_recomputed = False
        for unit in units:
            if self._indexer.add(unit):
                any_recomputed = True
            self._checkpointed_sources.setdefault(group_id, set()).update(unit.source_ids)
        if any_recomputed:
            self._store.save(group_id, units)
        self._loaded_groups.add(group_id)
        logger.info("Loaded %d checkpoint memory units for group %s", len(units), group_id)

    def _replace_loaded_group(self, group_id: str, units: list[MemoryUnit]) -> None:
        self._indexer.replace_group(group_id, units)
        self._checkpointed_sources[group_id] = {
            source_id for unit in units for source_id in unit.source_ids
        }
        self._loaded_groups.add(group_id)

    def is_source_checkpointed(self, group_id: str, entry_id: str) -> bool:
        self.ensure_group_loaded(group_id)
        return entry_id in self._checkpointed_sources.get(group_id, set())

    def retrieve(
        self,
        query: str,
        *,
        group_id: str | None = None,
        top_k: int = 5,
        max_tokens_budget: int = 800,
    ) -> list[MemoryUnit]:
        if group_id is not None:
            self.ensure_group_loaded(group_id)
        return self._retriever.retrieve(
            query=query,
            group_id=group_id or "",
            top_k=top_k,
            max_tokens_budget=max_tokens_budget,
        )

    def get_units_for_group(self, group_id: str) -> list[MemoryUnit]:
        self.ensure_group_loaded(group_id)
        return [unit for unit in self._indexer.list_all() if unit.group_id == group_id]
