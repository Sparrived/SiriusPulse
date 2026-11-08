"""Manager for checkpoint memory units."""

from __future__ import annotations

import asyncio
import logging
import threading
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
from sirius_pulse.memory.units.indexer import (
    ColdVectors,
    MemoryUnitIndexer,
    MemoryUnitRetriever,
    is_cold_eligible,
    normalize_rows,
)
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
        persona_prompt: str = "",
    ) -> None:
        self._store = MemoryUnitFileStore(work_path)
        self._embedding_client = embedding_client
        self._indexer = MemoryUnitIndexer(embedding_client=embedding_client)
        self._retriever = MemoryUnitRetriever(self._indexer)
        self._persona_prompt = persona_prompt.strip()
        self._generator = MemoryUnitGenerator()
        self._deduplicator = MemoryUnitDeduplicator(persona_context=self._persona_prompt)
        self._maintenance = MemoryUnitDedupeMaintenance(
            self, self._store, embedding_client, self._deduplicator
        )
        self._mutation_lock = asyncio.Lock()
        self._checkpointed_sources: dict[str, set[str]] = {}
        self._loaded_groups: set[str] = set()
        self._generation_backoff: dict[str, tuple[tuple[str, ...], float]] = {}
        #: 冷层（超额退休单元）的向量矩阵，按群缓存；在后台线程里构建，检索时只读。
        self._cold: dict[str, ColdVectors] = {}
        #: 每次某群单元被替换就加一，防止后台线程把旧快照写回冷层缓存。
        self._group_versions: dict[str, int] = {}
        self._cold_lock = threading.Lock()
        self._indexer.cold_source = self._cold.get

    def set_persona_prompt(self, persona_prompt: str) -> None:
        """更新记忆任务使用的人格设定（persona.json 热重载后调用）。"""
        self._persona_prompt = persona_prompt.strip()
        self._deduplicator.set_persona_context(self._persona_prompt)

    async def generate_from_candidates(
        self,
        *,
        group_id: str,
        candidates: list[BasicMemoryEntry],
        persona_name: str,
        persona_description: str,
        brain: Any,
        model_name: str,
        persona_aliases: list[str] | None = None,
        other_ai_names: list[str] | None = None,
        persona_prompt: str | None = None,
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
            persona_aliases=persona_aliases,
            other_ai_names=other_ai_names,
            persona_prompt=(persona_prompt if persona_prompt is not None else self._persona_prompt),
            brain=brain,
            model_name=model_name,
            max_retries=max(0, int(max_retries)),
            transport_retries=max(0, int(transport_retries)),
        )
        if result is None or (not result.units and result.dropped):
            self._generation_backoff[group_id] = (
                candidate_key,
                time.monotonic() + max(0.0, float(failure_backoff_seconds)),
            )
            return None
        if not result.units:
            # 模型看过这批记录、判定没有值得记的内容：这是正常结论而不是失败。调用方
            # 据此把整批标为已处理；当成失败退避的话，这批闲聊会永远堵在队首。
            self._generation_backoff.pop(group_id, None)
            return result

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
        # 新单元的向量一次请求补齐，且放到线程里：逐条同步请求会卡住聊天主循环。
        await asyncio.to_thread(self._indexer.encode_missing, units)
        async with self._mutation_lock:
            self.ensure_group_loaded(group_id)
            # 去重的语义候选要覆盖全组（含退休单元）——退休只是不再注入，不代表
            # 同一条事实可以被重新写一遍。所以这里按「带向量」读取，而不是沿用
            # ensure_group_loaded 为省内存而做的只读元数据。
            existing = self._store.load(group_id)
            # 索引里此刻只有「活跃单元带向量」（常驻路径为省内存故意如此），而语义去重
            # 的候选必须覆盖全组含退休单元。先用带向量的全量刷新一次，别让退休单元
            # 因为「内存里没有向量」而被漏判成 NEW。
            self._indexer.replace_group(group_id, existing)
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
                unit.metadata["retired"] = "expired"
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
                # 超额退休的单元进冷层：不常驻注入，但语义足够接近时仍能被检索到。
                unit.metadata["retired"] = "overflow"
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
            # 追加不需要现成向量，按元数据读取即可；真正要注入的单元在写盘前补一次。
            existing = self._store.load(group_id, with_embeddings=False)
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
                # 只给会被注入的单元补向量；退休单元的向量留在磁盘，去重时再按需读。
                self._store.hydrate_embeddings(
                    group_id,
                    existing,
                    unit_ids={unit.unit_id for unit in existing if unit.should_prompt},
                )
                self._store.save(group_id, existing)
                # 重新加载过的对象与索引里那一批不是同一批，退休标记必须同步进索引，
                # 否则模型侧仍按旧标记召回。
                self._replace_loaded_group(group_id, existing)

    def ensure_group_loaded(self, group_id: str) -> None:
        if group_id in self._loaded_groups:
            return
        # 只读元数据，不带向量：向量占单条单元的 99% 体积，而线上 90.6% 的向量属于
        # 永远不会被注入的退休单元（单群 7547/8047）。整组读向量是内存峰值的主因。
        units = self._store.load(group_id, with_embeddings=False)
        # 存量分组可能早就超额或早已过期（限额是后加的），加载时立即生效，不必等
        # 下一次 checkpoint；否则要等到有新对话才会收敛。
        any_recomputed = bool(self.retire_overflow(group_id, units))
        # 只给真正可能被注入的单元补向量；退休单元的向量由去重路径按需补。
        self._store.hydrate_embeddings(
            group_id, units, unit_ids={unit.unit_id for unit in units if unit.should_prompt}
        )
        if self._indexer.encode_missing(units):
            any_recomputed = True
        for unit in units:
            if self._indexer.add(unit):
                any_recomputed = True
            self._checkpointed_sources.setdefault(group_id, set()).update(unit.source_ids)
        # 旧格式（向量内联在 JSON 里）必须落一次盘才会搬进 sidecar，否则这次「省内存
        # 加载」省下的只是本次进程，几百 MB 的 JSON 会一直在每条消息的读路径上。
        if any_recomputed or self._store.is_legacy_layout(group_id):
            self._store.save(group_id, units)
        self._loaded_groups.add(group_id)
        self._invalidate_cold(group_id)
        logger.info("Loaded %d checkpoint memory units for group %s", len(units), group_id)

    def _replace_loaded_group(self, group_id: str, units: list[MemoryUnit]) -> None:
        # 退休单元的向量不留在索引里：它们永不参与注入，却占了线上 90.6% 的向量。
        # 清掉不等于丢数据——磁盘那份原样保留（写盘时会按文本指纹沿用它），去重
        # 需要时再按需读回。少了这一步，一次 checkpoint 就会把整组向量重新变常驻。
        for unit in units:
            if not unit.should_prompt:
                unit.embedding = None
        self._indexer.replace_group(group_id, units)
        self._checkpointed_sources[group_id] = {
            source_id for unit in units for source_id in unit.source_ids
        }
        self._loaded_groups.add(group_id)
        self._invalidate_cold(group_id)

    # ------------------------------------------------------------------
    # 冷层与检索准备
    # ------------------------------------------------------------------

    def _invalidate_cold(self, group_id: str) -> None:
        with self._cold_lock:
            self._group_versions[group_id] = self._group_versions.get(group_id, 0) + 1
            self._cold.pop(group_id, None)

    def build_cold_vectors(self, group_id: str) -> ColdVectors | None:
        """读出某群冷层单元的向量矩阵并缓存。会读盘，应在后台线程里调用。"""
        if not group_id or not self._indexer.semantic_available:
            return None
        with self._cold_lock:
            cached = self._cold.get(group_id)
            version = self._group_versions.get(group_id, 0)
        if cached is not None:
            return cached
        units = [
            unit
            for unit in self._indexer.list_all()
            if unit.group_id == group_id and is_cold_eligible(unit)
        ]
        ids, matrix = self._store.read_vector_matrix(group_id, units)
        by_id = {unit.unit_id: unit for unit in units}
        cold = ColdVectors(
            unit_ids=ids,
            matrix=normalize_rows(matrix).astype("float16") if ids else matrix,
            units=[by_id[unit_id] for unit_id in ids],
        )
        with self._cold_lock:
            if self._group_versions.get(group_id, 0) != version:
                return None
            self._cold[group_id] = cold
        return cold

    def prepare_retrieval(self, query: str, *, group_id: str = "", context: str = "") -> None:
        """把检索里会发网络请求、读大文件的部分提前做完。

        在 ``asyncio.to_thread`` 里调用：查询向量进缓存、冷层矩阵读进内存，之后
        聊天主循环里的 ``retrieve`` 只剩内存里的矩阵运算。任何失败都只让这一轮
        少一路召回，不会抛给聊天回合。
        """
        try:
            self._indexer.prepare_queries(query, context)
            if group_id and group_id in self._loaded_groups:
                self.build_cold_vectors(group_id)
        except Exception as exc:
            logger.warning("记忆检索预取失败，本轮退化为同步检索: %s", exc)

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
        context: str = "",
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
            context=context,
        )

    def retrieve_for_person(
        self,
        *,
        group_id: str,
        identity_keys: list[str],
        query: str = "",
        include_user_scope: bool = False,
        limit: int = 3,
    ) -> list[MemoryUnit]:
        """Read-only exact-identity memory lookup, without loading or computing vectors.

        ``identity_keys`` are verified UUIDs, legacy user IDs or platform UIDs supplied
        by PersonService, never display names. The ephemeral index cannot affect the
        resident index or persist a retired unit back to disk. Participant membership
        links shared group facts; user facts are owned only by their exact scope_id.
        """
        if not group_id or not identity_keys:
            return []
        limit = max(1, min(int(limit), 3))
        keys = {str(key).strip() for key in identity_keys if str(key).strip()}

        def exact_owner(scope_id: str) -> bool:
            return bool(scope_id and scope_id in keys)

        units = []
        for unit in self._store.load(group_id, with_embeddings=False):
            if (
                unit.group_id != group_id
                or not unit.should_prompt
                or MemoryUnitIndexer._is_expired(unit)
            ):
                continue
            if unit.scope == "user":
                if include_user_scope and exact_owner(unit.scope_id):
                    units.append(unit)
                continue
            if unit.scope in {"group", "persona", "global"} and any(
                exact_owner(participant) for participant in unit.participants
            ):
                # Aliases are retrieval hints, not ownership or identity evidence.
                units.append(unit)
        indexer = MemoryUnitIndexer(embedding_client=None)
        indexer.replace_group(group_id, units)
        retriever = MemoryUnitRetriever(indexer)
        if not query.strip():
            units.sort(
                key=lambda unit: (
                    unit.salience * unit.confidence,
                    unit.event_time or unit.created_at,
                ),
                reverse=True,
            )
            return units[:limit]
        return retriever.retrieve(
            query,
            group_id=group_id,
            top_k=limit,
            max_tokens_budget=180,
            user_id=identity_keys[0],
            identity_aliases=identity_keys,
            cross_group_enabled=False,
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
        with self._cold_lock:
            for group_id in list(self._group_versions):
                self._group_versions[group_id] += 1
            self._cold.clear()


def rebuild_memory_unit_embeddings(
    embedding_client: EmbeddingClient | None,
    store: MemoryUnitFileStore,
) -> tuple[int, int]:
    """按当前 embedding 模型重算全部记忆单元的向量并落盘。

    记忆单元的向量存在 ``memory_units/vectors/`` 下的 sidecar 文件里，换模型后维度
    失配，语义检索会静默退化成纯关键词检索。每组的维度都从当前模型重新学，因此不
    需要预知具体维度。

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
