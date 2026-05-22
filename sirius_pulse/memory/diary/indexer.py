"""Diary indexer: semantic embedding and RAG retrieval.

Embedding 是强依赖：所有语义计算均通过 EmbeddingClient 调用远程微服务，
不再内置 SentenceTransformer 本地模型加载与 fallback 逻辑。
"""

from __future__ import annotations

import logging
import math
from typing import Any

from sirius_pulse.embedding.client import EmbeddingClient
from sirius_pulse.memory.diary.models import DiaryEntry
from sirius_pulse.memory.diary.vector_store import DiaryVectorStore

logger = logging.getLogger(__name__)


class DiaryIndexer:
    """日记语义索引，使用 ChromaDB 持久化向量存储。

    所有 embedding 计算均通过 EmbeddingClient 调用远程微服务。
    ``enable_semantic=False`` 时仅使用关键词检索（供单元测试使用）。
    """

    def __init__(
        self,
        enable_semantic: bool = True,
        vector_store: DiaryVectorStore | None = None,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self._entries: list[DiaryEntry] = []
        self._enable_semantic = enable_semantic
        self._vector_store = vector_store
        self._embedding_client = embedding_client

        if not enable_semantic:
            logger.debug("日记语义索引已禁用（enable_semantic=False）")
        elif embedding_client is None:
            logger.warning("未提供 EmbeddingClient，日记语义检索不可用")

    @property
    def semantic_available(self) -> bool:
        """语义能力是否可用（需要 enable_semantic 且 EmbeddingClient 可用）。"""
        if not self._enable_semantic:
            return False
        return self._embedding_client is not None and self._embedding_client.available

    def encode_texts(self, texts: list[str]) -> list[list[float]]:
        """通过远程 Embedding 服务编码文本列表。

        Raises:
            RuntimeError: 无 EmbeddingClient 或服务调用失败。
        """
        if self._embedding_client is None:
            raise RuntimeError("EmbeddingClient 未初始化，无法计算 embedding")
        return self._embedding_client.encode(texts)

    def encode_single(self, text: str) -> list[float]:
        """编码单条文本，返回嵌入向量。"""
        return self._embedding_client.encode_single(text) if self._embedding_client else []

    def add(self, entry: DiaryEntry) -> bool:
        """将条目加入索引，自动计算 embedding。

        当 semantic_available 为 True 且条目尚无 embedding 时，
        自动通过远程服务计算并填充。

        Returns:
            True 表示本次新计算了 embedding。
        """
        recomputed = False
        if self.semantic_available:
            if not entry.embedding:
                vec = self.encode_single(entry.content)
                if vec:
                    entry.embedding = vec
                    recomputed = True
                    logger.info("日记 embedding 已计算: %s", entry.entry_id)

        # 持久化到向量存储
        if self._vector_store is not None and self._vector_store.available:
            self._vector_store.add(entry)

        self._entries.append(entry)
        return recomputed

    def search(
        self,
        query: str,
        top_k: int = 5,
        group_id: str = "",
    ) -> list[tuple[DiaryEntry, float]]:
        """混合检索：融合语义相似度 + 关键词匹配。

        Args:
            query: 检索查询。
            top_k: 最大返回条目数。
            group_id: 若指定，仅检索该群组的条目。

        Returns:
            (entry, score) 列表，按分数降序排列。
        """
        entries = self._entries
        if group_id:
            entries = [e for e in entries if e.group_id == group_id]
        if not entries:
            logger.debug("日记检索: group=%s 无条目可检索", group_id)
            return []

        # 语义检索
        semantic_scores: dict[str, float] = {}
        if self.semantic_available:
            if self._vector_store is not None and self._vector_store.available and group_id:
                try:
                    query_vec = self.encode_single(query)
                    if query_vec:
                        for eid, score in self._vector_store.search(
                            query_vec, group_id, top_k=top_k * 2
                        ):
                            semantic_scores[eid] = score
                except Exception as exc:
                    logger.warning("向量存储检索失败: %s", exc)
            else:
                for entry, score in self._semantic_search(query, len(entries), entries):
                    semantic_scores[entry.entry_id] = score

        # 关键词检索
        keyword_scores: dict[str, float] = {}
        for entry, score in self._keyword_search(query, len(entries), entries):
            keyword_scores[entry.entry_id] = score

        # 融合: 语义 60% + 关键词 40%
        fused: list[tuple[DiaryEntry, float]] = []
        for entry in entries:
            s = semantic_scores.get(entry.entry_id, 0.0)
            k = keyword_scores.get(entry.entry_id, 0.0)
            final = 0.6 * s + 0.4 * min(k / 2.0, 1.0)
            if final > 0.05:
                fused.append((entry, final))

        fused.sort(key=lambda x: x[1], reverse=True)
        result = fused[:top_k]
        logger.info(
            "日记检索: group=%s query=%.20s... | 候选=%d | 语义=%s | 返回=%d 条",
            group_id,
            query,
            len(entries),
            "开" if self.semantic_available else "关",
            len(result),
        )
        return result

    def _semantic_search(
        self,
        query: str,
        top_k: int,
        entries: list[DiaryEntry],
    ) -> list[tuple[DiaryEntry, float]]:
        """内存中的语义检索。"""
        query_vec = self.encode_single(query)
        if not query_vec:
            return []

        scored: list[tuple[DiaryEntry, float]] = []
        for entry in entries:
            if not entry.embedding:
                continue
            score = self._cosine_sim(query_vec, entry.embedding)
            if score > 0.25:
                scored.append((entry, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    def _keyword_search(
        self,
        query: str,
        top_k: int,
        entries: list[DiaryEntry],
    ) -> list[tuple[DiaryEntry, float]]:
        query_lower = query.lower()
        scored: list[tuple[DiaryEntry, float]] = []
        for entry in entries:
            score = 0.0
            if query_lower in entry.content.lower():
                score += 1.0
            for kw in entry.keywords:
                if query_lower in kw.lower():
                    score += 0.5
            if query_lower in entry.summary.lower():
                score += 0.8
            if score > 0:
                scored.append((entry, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def remove_by_source_ids(self, source_ids: set[str]) -> int:
        """Remove entries whose source_ids overlap with the given set.

        Returns number of removed entries.
        """
        original = len(self._entries)
        removed_ids: list[str] = []
        new_entries: list[DiaryEntry] = []
        for e in self._entries:
            if set(e.source_ids) & source_ids:
                removed_ids.append(e.entry_id)
            else:
                new_entries.append(e)
        self._entries = new_entries

        if removed_ids and self._vector_store is not None and self._vector_store.available:
            affected_groups = {e.group_id for e in self._entries + new_entries}
            for gid in affected_groups:
                self._vector_store.clear_group(gid)
            for e in self._entries:
                self._vector_store.add(e)

        return original - len(self._entries)

    def list_all(self) -> list[DiaryEntry]:
        return list(self._entries)

    def clear(self) -> None:
        self._entries.clear()


class DiaryRetriever:
    """High-level retriever with token budget management."""

    def __init__(self, indexer: DiaryIndexer) -> None:
        self._indexer = indexer

    def retrieve(
        self,
        query: str,
        *,
        group_id: str = "",
        top_k: int = 5,
        max_tokens_budget: int = 800,
    ) -> list[DiaryEntry]:
        """Retrieve relevant diary entries within token budget.

        Approximates 1 token ≈ 1.5 Chinese characters or 0.75 English words.
        If *group_id* is provided, only entries from that group are returned.
        """
        results = self._indexer.search(query, top_k=top_k, group_id=group_id)
        if not results:
            return []

        selected: list[DiaryEntry] = []
        total_chars = 0
        char_budget = int(max_tokens_budget * 1.5)

        for entry, score in results:
            added_chars = len(entry.content) + len(entry.summary)
            if total_chars + added_chars > char_budget and selected:
                break
            selected.append(entry)
            total_chars += added_chars

        return selected


__all__ = ["DiaryIndexer", "DiaryRetriever"]
