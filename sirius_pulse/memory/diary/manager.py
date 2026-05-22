"""Diary manager: orchestrates generation, indexing, storage, and retrieval."""

from __future__ import annotations

import logging
from typing import Any

from sirius_pulse.embedding.client import EmbeddingClient
from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.memory.diary.generator import DiaryGenerator
from sirius_pulse.memory.diary.indexer import DiaryIndexer, DiaryRetriever
from sirius_pulse.memory.diary.models import DiaryEntry, DiaryGenerationResult
from sirius_pulse.memory.diary.store import DiaryFileStore

logger = logging.getLogger(__name__)


class DiaryManager:
    """High-level manager for diary memory lifecycle.

    - Generates diary entries from basic memory candidates.
    - Indexes entries for semantic retrieval.
    - Persists to disk.
    """

    def __init__(
        self,
        work_path: Any,
        vector_store: Any | None = None,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self._store = DiaryFileStore(work_path)
        self._indexer = DiaryIndexer(
            vector_store=vector_store,
            embedding_client=embedding_client,
        )
        self._retriever = DiaryRetriever(self._indexer)
        self._generator = DiaryGenerator()
        # Track source_ids that have already been diary-ized per group
        self._diarized_sources: dict[str, set[str]] = {}
        # Track which groups have been loaded from disk (lazy loading)
        self._loaded_groups: set[str] = set()
        # Track the last few source_ids of each group's most recent diary entry
        # for continuity overlap on next generation.
        self._last_diary_tail_sources: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    async def generate_from_candidates(
        self,
        *,
        group_id: str,
        candidates: list[BasicMemoryEntry],
        persona_name: str,
        persona_description: str,
        provider_async: Any,
        model_name: str,
        min_candidate_count: int = 12,
        overlap_tail_count: int = 3,
    ) -> DiaryGenerationResult | None:
        """Generate a diary entry from candidates and index it.

        Args:
            min_candidate_count: Minimum number of undiarized candidates
                required before generating a diary entry.
            overlap_tail_count: Number of source_ids from the previous
                diary entry to prepend as overlap for continuity.
        """
        if not candidates:
            return None

        if len(candidates) < min_candidate_count:
            logger.debug(
                "群 %s 日记候选消息不足 %d 条（当前 %d 条），暂不生成日记。",
                group_id,
                min_candidate_count,
                len(candidates),
            )
            return None

        # Build overlap from previous diary tail sources for continuity
        overlap_sources = self._last_diary_tail_sources.get(group_id, [])
        if overlap_sources:
            # Find candidates that match overlap source_ids and prepend them
            overlap_map = {c.entry_id: c for c in candidates}
            overlapped = []
            for sid in overlap_sources:
                if sid in overlap_map:
                    overlapped.append(overlap_map[sid])
            # Deduplicate while preserving order
            seen: set[str] = set()
            unique_overlap: list[BasicMemoryEntry] = []
            for c in overlapped:
                if c.entry_id not in seen:
                    seen.add(c.entry_id)
                    unique_overlap.append(c)
            if unique_overlap:
                candidates = unique_overlap + candidates
                logger.info(
                    "群 %s 日记生成时带上了前次末尾 %d 条重叠消息以保证连续性。",
                    group_id,
                    len(unique_overlap),
                )

        result = await self._generator.generate(
            group_id=group_id,
            candidates=candidates,
            persona_name=persona_name,
            persona_description=persona_description,
            provider_async=provider_async,
            model_name=model_name,
        )
        if result is None:
            return None

        self.add_entry(group_id, result.entry)

        # Mark sources as diarized
        sources = self._diarized_sources.setdefault(group_id, set())
        sources.update(result.entry.source_ids)

        # Remember the tail sources of this diary for next overlap
        self._last_diary_tail_sources[group_id] = list(result.entry.source_ids)[-overlap_tail_count:]

        logger.info(
            "群 %s 的日记写好了，总结了 %d 条对话。",
            group_id,
            len(result.entry.source_ids),
        )
        return result

    def ensure_group_loaded(self, group_id: str) -> None:
        """Lazy-load persisted entries for a group if not already loaded.

        Safe to call multiple times (idempotent).  This is the entry point
        for external callers (e.g. EmotionalGroupChatEngine) to warm up
        the diary index before retrieval.
        """
        if group_id in self._loaded_groups:
            return
        self.load_group(group_id)
        self._loaded_groups.add(group_id)

    def is_source_diarized(self, group_id: str, entry_id: str) -> bool:
        """Check if a basic memory entry has already been processed into a diary."""
        self.ensure_group_loaded(group_id)
        return entry_id in self._diarized_sources.get(group_id, set())

    # ------------------------------------------------------------------
    # Index / Store
    # ------------------------------------------------------------------

    def add_entry(self, group_id: str, entry: DiaryEntry) -> None:
        """Add an entry to memory index and persist."""
        self.ensure_group_loaded(group_id)
        self._indexer.add(entry)
        existing = self._store.load(group_id)
        existing.append(entry)
        self._store.save(group_id, existing)

    def load_group(self, group_id: str) -> None:
        """Load persisted entries for a group into the index."""
        entries = self._store.load(group_id)
        logger.info("群 %s 日记加载中: 磁盘条目=%d", group_id, len(entries))
        any_recomputed = False
        for entry in entries:
            if self._indexer.add(entry):
                any_recomputed = True
            sources = self._diarized_sources.setdefault(group_id, set())
            sources.update(entry.source_ids)
        # If any stale embeddings were recomputed (e.g. model swap),
        # persist the updated entries so the migration happens only once.
        if any_recomputed:
            self._store.save(group_id, entries)
            logger.info("群 %s 的日记 embedding 已自动迁移并持久化", group_id)
        # Bulk-migrate existing entries with embeddings to vector store
        # (one-shot migration for old data created before Chroma backend).
        self._maybe_migrate_to_vector_store(entries)
        logger.info("群 %s 日记加载完成: 索引条目=%d", group_id, len(entries))

    def _maybe_migrate_to_vector_store(self, entries: list[DiaryEntry]) -> None:
        """Migrate entries with embeddings to vector store if not already present."""
        vs = self._indexer._vector_store
        if vs is None or not vs.available or not entries:
            return
        # Only migrate entries that have embeddings
        to_migrate = [e for e in entries if e.embedding]
        if not to_migrate:
            return
        # Check current count for each group to avoid unnecessary work
        by_group: dict[str, list[DiaryEntry]] = {}
        for e in to_migrate:
            by_group.setdefault(e.group_id, []).append(e)
        migrated = 0
        for gid, group_entries in by_group.items():
            current_count = vs.count(gid)
            # Simple heuristic: if vector store already has entries for this group,
            # assume migration was done before. This avoids re-upserting on every load.
            if current_count > 0:
                continue
            migrated += vs.add_many(group_entries)
        if migrated > 0:
            logger.info("日记向量存储迁移完成: %d 条旧数据已写入 Chroma", migrated)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        *,
        group_id: str | None = None,
        top_k: int = 5,
        max_tokens_budget: int = 800,
    ) -> list[DiaryEntry]:
        """Retrieve relevant diary entries.

        Args:
            query: Search query.
            group_id: If provided, lazy-load this group's entries before retrieval.
            top_k: Maximum number of entries to return.
            max_tokens_budget: Maximum tokens for the returned content.
        """
        if group_id is not None:
            self.ensure_group_loaded(group_id)
        results = self._retriever.retrieve(
            query=query,
            top_k=top_k,
            max_tokens_budget=max_tokens_budget,
        )
        logger.info(
            "日记检索结果: group=%s | 返回 %d 条 (预算 %d tokens)",
            group_id,
            len(results),
            max_tokens_budget,
        )
        return results

    # ------------------------------------------------------------------
    # Consolidation helpers
    # ------------------------------------------------------------------

    def get_entries_for_group(self, group_id: str) -> list[DiaryEntry]:
        """Get all indexed entries for a group."""
        self.ensure_group_loaded(group_id)
        return [e for e in self._indexer.list_all() if e.group_id == group_id]

    def replace_entries(self, group_id: str, new_entries: list[DiaryEntry]) -> None:
        """Replace all entries for a group (used after consolidation).

        Batch-clears the in-memory index and vector store for this group,
        then adds all new entries in one pass to avoid O(n²) ChromaDB writes.
        """
        # 1. Remove old in-memory entries for this group
        old = self._store.load(group_id)
        old_source_ids: set[str] = set()
        for e in old:
            old_source_ids.update(e.source_ids)
        self._indexer._entries = [
            e for e in self._indexer._entries if e.group_id != group_id
        ]

        # 2. Clear vector store for this group once (instead of per-entry)
        vs = self._indexer._vector_store
        if vs is not None and vs.available:
            vs.clear_group(group_id)

        # 3. Add new entries (embedding + vector store) in one pass
        for e in new_entries:
            self._indexer.add(e)

        # 4. Persist to disk
        self._store.save(group_id, new_entries)
