"""Manager for checkpoint memory units."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from sirius_pulse.core.constants import DEFAULT_MEMORY_UNIT_ACTIVE_LIMIT
from sirius_pulse.embedding.client import EmbeddingClient
from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.memory.units.deduplicator import (
    MemoryUnitDeduplicator,
    apply_verdict,
)
from sirius_pulse.memory.units.generator import MemoryUnitGenerator
from sirius_pulse.memory.units.indexer import MemoryUnitIndexer, MemoryUnitRetriever
from sirius_pulse.memory.units.maintenance import MemoryUnitDedupeMaintenance
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
        self._generation_backoff: dict[str, tuple[tuple[str, ...], float]] = {}

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
        max_candidate_count: int = 32,
        max_retries: int = 1,
        transport_retries: int = 0,
        failure_backoff_seconds: float = 3600.0,
    ) -> MemoryUnitGenerationResult | None:
        candidates = list(candidates[: max(1, int(max_candidate_count))])
        if len(candidates) < min_candidate_count:
            logger.debug(
                "Group %s has not enough memory checkpoint candidates (%d < %d)",
                group_id,
                len(candidates),
                min_candidate_count,
            )
            return None

        # Candidate order is an implementation detail of the basic-memory window.
        # A stable key prevents a failed batch from bypassing backoff merely because
        # the caller rebuilt the same candidate list in another order.
        candidate_key = tuple(sorted(entry.entry_id for entry in candidates))
        blocked = self._generation_backoff.get(group_id)
        if blocked and blocked[0] == candidate_key and time.monotonic() < blocked[1]:
            logger.debug("Skipping memory checkpoint retry for group %s", group_id)
            return None

        result = await self._generator.generate(
            group_id=group_id,
            candidates=candidates,
            persona_name=persona_name,
            persona_description=persona_description,
            brain=brain,
            model_name=model_name,
            max_retries=max(0, int(max_retries)),
            transport_retries=max(0, int(transport_retries)),
        )
        if result is None or not result.units:
            self._generation_backoff[group_id] = (
                candidate_key,
                time.monotonic() + max(0.0, float(failure_backoff_seconds)),
            )
            return None

        canonical_results = await self.reconcile_units(
            group_id,
            result.units,
            brain=brain,
            model_name=model_name,
        )
        self._generation_backoff.pop(group_id, None)
        return MemoryUnitGenerationResult(units=canonical_results)

    def defer_checkpoint_retry(
        self,
        group_id: str,
        candidates: list[BasicMemoryEntry],
        *,
        failure_backoff_seconds: float = 3600.0,
    ) -> None:
        """Back off a checkpoint batch that produced no removable progress."""
        candidate_key = tuple(sorted(entry.entry_id for entry in candidates))
        if not candidate_key:
            return
        self._generation_backoff[group_id] = (
            candidate_key,
            time.monotonic() + max(0.0, float(failure_backoff_seconds)),
        )

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
            self.retire_overflow(group_id, existing)
            self._store.save(group_id, existing)
            self._replace_loaded_group(group_id, existing)
            return list(accepted.values())

    def retire_overflow(self, group_id: str, units: list[MemoryUnit]) -> int:
        """把过期和超额的低价值单元标记为不再注入提示词；返回本次标记数。

        只翻转 ``should_prompt``，从不删除：记忆单元必须能被真人追溯，模型侧不再
        召回不等于数据可以丢。单元总量本身不是问题（summary 上限 180 字），注入
        污染才是，所以这里控制的是「谁参与检索」而不是「谁存在」。
        """
        retired = 0
        for unit in units:
            if unit.should_prompt and MemoryUnitIndexer._is_expired(unit):
                unit.should_prompt = False
                retired += 1

        active = [unit for unit in units if unit.should_prompt]
        if len(active) > DEFAULT_MEMORY_UNIT_ACTIVE_LIMIT:
            active.sort(
                key=lambda unit: (
                    unit.salience * unit.confidence,
                    MemoryUnitIndexer._parse_time(unit.event_time or unit.created_at),
                ),
                reverse=True,
            )
            for unit in active[DEFAULT_MEMORY_UNIT_ACTIVE_LIMIT:]:
                unit.should_prompt = False
                retired += 1

        if retired:
            logger.info(
                "Retired %d memory units for group %s (active limit %d)",
                retired,
                group_id,
                DEFAULT_MEMORY_UNIT_ACTIVE_LIMIT,
            )
        return retired

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

    async def add_units(self, group_id: str, units: list[MemoryUnit]) -> None:
        """把一批单元追加进某个群，与 checkpoint 路径共用同一把写锁。

        这里必须持锁：`reconcile_units()` 在持锁期间会 await 去重判定，而本方法
        做的是「读盘 → 追加 → 写盘」的读改写。不持锁时两者会在对方的 await
        窗口里交错，后写的一方覆盖前一方，表现为静默丢单元。
        """
        if not units:
            return
        async with self._mutation_lock:
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
                self.retire_overflow(group_id, existing)
                self._store.save(group_id, existing)
                # 重新加载过的对象与索引里那一批不是同一批，退休标记必须同步进索引，
                # 否则模型侧仍按旧标记召回。
                self._replace_loaded_group(group_id, existing)

    def ensure_group_loaded(self, group_id: str) -> None:
        if group_id in self._loaded_groups:
            return
        units = self._store.load(group_id)
        # 存量分组可能早就超额或早已过期（限额是后加的），加载时立即生效，不必等
        # 下一次 checkpoint；否则要等到有新对话才会收敛。
        any_recomputed = bool(self.retire_overflow(group_id, units))
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
        user_id: str = "",
        identity_aliases: list[str] | None = None,
        mentioned_user_ids: list[str] | None = None,
        cross_group_enabled: bool = False,
    ) -> list[MemoryUnit]:
        if group_id is not None:
            self.ensure_group_loaded(group_id)
        if cross_group_enabled:
            for loaded_group_id in self._store.list_group_ids():
                self.ensure_group_loaded(loaded_group_id)
        return self._retriever.retrieve(
            query=query,
            group_id=group_id or "",
            top_k=top_k,
            max_tokens_budget=max_tokens_budget,
            user_id=user_id,
            identity_aliases=identity_aliases,
            mentioned_user_ids=mentioned_user_ids,
            cross_group_enabled=cross_group_enabled,
        )

    def get_units_for_group(self, group_id: str) -> list[MemoryUnit]:
        self.ensure_group_loaded(group_id)
        return [unit for unit in self._indexer.list_all() if unit.group_id == group_id]

    def reload_from_disk(self) -> None:
        """丢弃已加载分组的缓存，下次访问时从磁盘重新读取。

        WebUI 重建完向量后由 worker 调用：这里缓存的向量还是旧维度的，不丢弃的话
        重建等于没做。
        """
        self._loaded_groups.clear()
        self._checkpointed_sources.clear()
        self._indexer.reset()


def rebuild_memory_unit_embeddings(
    embedding_client: EmbeddingClient | None,
    store: MemoryUnitFileStore,
) -> tuple[int, int]:
    """按当前 embedding 模型重算全部记忆单元的向量并落盘。

    记忆单元的向量内联存在 ``memory_units/*.json`` 里，换模型后维度失配，语义检索会
    静默退化成纯关键词检索。每组的维度都从当前模型重新学，因此不需要预知具体维度。

    返回 ``(总条数, 仍失败的条数)``。失败的条目要如实报给调用方：整批超时会让那一批
    原样留在旧维度，只回一个「成功」会让人以为索引已经修好了。
    """
    if embedding_client is None:
        return 0, 0
    indexer = MemoryUnitIndexer(embedding_client=embedding_client)
    expected = embedding_client.dimension
    total = 0
    failed = 0
    for group_id in store.list_group_ids():
        units = store.load(group_id)
        if not units:
            continue
        if indexer.ensure_model_current(units):
            store.save(group_id, units)
            total += len(units)
        if expected is not None:
            failed += sum(1 for unit in units if len(unit.embedding or []) != expected)
    return total, failed
