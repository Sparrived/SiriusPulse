"""日记切片三路召回检索器。

三路融合：
1. 语义检索（ChromaDB embedding）— 适合模糊意图
2. 三元组精确匹配（entity vs triple_subjects）— 适合精确查询
3. 关键词匹配（降级 fallback）— 适合话题召回
"""

from __future__ import annotations

import logging
from typing import Any

from sirius_pulse.memory.diary.slice_models import DiarySlice

logger = logging.getLogger(__name__)

__all__ = ["DiarySliceRetriever"]


class DiarySliceRetriever:
    """三路融合检索器。"""

    WEIGHT_SEMANTIC = 0.4
    WEIGHT_TRIPLE = 0.4
    WEIGHT_KEYWORD = 0.2

    def __init__(
        self,
        slices: list[DiarySlice] | None = None,
        embedding_client: Any | None = None,
        vector_store: Any | None = None,
    ) -> None:
        self._slices: list[DiarySlice] = []
        self._embedding_client = embedding_client
        self._vector_store = vector_store

        if slices:
            for s in slices:
                self.add(s)

    def add(self, slice: DiarySlice) -> None:
        """添加切片到索引（内存 + ChromaDB）。"""
        self._slices.append(slice)
        if self._vector_store and self._vector_store.available and slice.embedding:
            self._vector_store.add(slice)

    def clear(self) -> None:
        """清空索引。"""
        self._slices.clear()

    def retrieve(
        self,
        query: str,
        query_entities: list[str],
        group_id: str,
        token_budget: int = 800,
        top_k: int = 10,
    ) -> list[DiarySlice]:
        """三路检索融合。"""
        group_slices = [s for s in self._slices if s.group_id == group_id]
        if not group_slices:
            return []

        # 三路检索
        semantic_scores = self._semantic_search(query, group_id, group_slices)
        triple_scores = self._triple_search(query_entities, group_slices)
        keyword_scores = self._keyword_search(query, group_slices)

        # 融合分数
        merged: dict[str, float] = {}
        for slice_id, score in semantic_scores.items():
            merged[slice_id] = merged.get(slice_id, 0) + score * self.WEIGHT_SEMANTIC
        for slice_id, score in triple_scores.items():
            merged[slice_id] = merged.get(slice_id, 0) + score * self.WEIGHT_TRIPLE
        for slice_id, score in keyword_scores.items():
            merged[slice_id] = merged.get(slice_id, 0) + score * self.WEIGHT_KEYWORD

        # 按分数排序
        slice_map = {s.slice_id: s for s in group_slices}
        sorted_ids = sorted(merged.keys(), key=lambda x: merged[x], reverse=True)

        # 按 token 预算裁剪
        result: list[DiarySlice] = []
        total_chars = 0
        for sid in sorted_ids[:top_k]:
            s = slice_map.get(sid)
            if not s:
                continue
            if total_chars + len(s.content) > token_budget * 3:
                break
            result.append(s)
            total_chars += len(s.content)

        return result

    # ── 路径 1: 语义检索（ChromaDB）──

    def _semantic_search(
        self, query: str, group_id: str, slices: list[DiarySlice]
    ) -> dict[str, float]:
        """语义检索：使用 ChromaDB。"""
        if not self._embedding_client or not slices:
            return {}
        if not self._vector_store or not self._vector_store.available:
            return {}

        try:
            query_embedding = self._embedding_client.encode([query])[0]
        except Exception:
            return {}

        results = self._vector_store.search(
            query_embedding=query_embedding,
            group_id=group_id,
            top_k=len(slices),
        )
        return {sid: score for sid, score in results}

    # ── 路径 2: 三元组精确匹配 ──

    def _triple_search(
        self, entities: list[str], slices: list[DiarySlice]
    ) -> dict[str, float]:
        """三元组精确匹配。"""
        if not entities or not slices:
            return {}

        scores: dict[str, float] = {}
        for s in slices:
            match_count: float = 0
            for entity in entities:
                if entity in s.triple_subjects:
                    match_count += 1
                elif any(entity in sub for sub in s.triple_subjects):
                    match_count += 0.5

            if match_count > 0:
                scores[s.slice_id] = min(1.0, match_count / len(entities))

        return scores

    # ── 路径 3: 关键词匹配 ──

    def _keyword_search(
        self, query: str, slices: list[DiarySlice]
    ) -> dict[str, float]:
        """关键词匹配。"""
        if not query or not slices:
            return {}

        query_chars = set(query)
        if not query_chars:
            return {}

        scores: dict[str, float] = {}
        for s in slices:
            text = f"{s.content} {s.summary} {' '.join(s.keywords)}"
            text_chars = set(text)

            overlap = len(query_chars & text_chars)
            if overlap > 0:
                score = overlap / len(query_chars)
                scores[s.slice_id] = min(1.0, score)

        return scores
