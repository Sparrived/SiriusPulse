"""Manager for checkpoint memory units."""

from __future__ import annotations

import logging
from typing import Any

from sirius_pulse.embedding.client import EmbeddingClient
from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.memory.units.generator import MemoryUnitGenerator
from sirius_pulse.memory.units.indexer import MemoryUnitIndexer, MemoryUnitRetriever
from sirius_pulse.memory.units.models import MemoryUnit, MemoryUnitGenerationResult
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
        self._indexer = MemoryUnitIndexer(embedding_client=embedding_client)
        self._retriever = MemoryUnitRetriever(self._indexer)
        self._generator = MemoryUnitGenerator()
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

        self.add_units(group_id, result.units)
        return result

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
