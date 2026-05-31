"""日记切片三路召回检索器。

不使用简单关键词识别，采用三路融合：
1. 语义检索（embedding cosine similarity）— 适合模糊意图
2. 三元组精确匹配（entity vs triple_subjects）— 适合精确查询
3. 关键词匹配（降级 fallback）— 适合话题召回
"""

from __future__ import annotations

import logging
import math
from typing import Any

from sirius_pulse.memory.diary.slice_models import DiarySlice

logger = logging.getLogger(__name__)

__all__ = ["DiarySliceRetriever"]


class DiarySliceRetriever:
    """三路融合检索器。"""

    # 权重配置
    WEIGHT_SEMANTIC = 0.4
    WEIGHT_TRIPLE = 0.4
    WEIGHT_KEYWORD = 0.2

    def __init__(
        self,
        slices: list[DiarySlice] | None = None,
        embedding_client: Any | None = None,
    ) -> None:
        self._slices: list[DiarySlice] = []
        self._embedding_client = embedding_client

        if slices:
            for s in slices:
                self.add(s)

    def add(self, slice: DiarySlice) -> None:
        """添加切片到索引。"""
        self._slices.append(slice)

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
        """三路检索融合。

        Args:
            query: 用户查询文本
            query_entities: 查询中提取的实体名（用于三元组精确匹配）
            group_id: 群组 ID
            token_budget: token 预算
            top_k: 最多返回数量

        Returns:
            按相关性排序的 DiarySlice 列表
        """
        # 过滤群组
        group_slices = [s for s in self._slices if s.group_id == group_id]
        if not group_slices:
            return []

        # 三路检索
        semantic_scores = self._semantic_search(query, group_slices)
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

    # ── 路径 1: 语义检索 ──

    def _semantic_search(
        self, query: str, slices: list[DiarySlice]
    ) -> dict[str, float]:
        """语义检索：embedding cosine similarity。"""
        if not self._embedding_client or not slices:
            return {}

        # 计算 query embedding
        try:
            query_embedding = self._embedding_client.encode([query])[0]
        except Exception:
            return {}

        scores: dict[str, float] = {}
        for s in slices:
            if s.embedding:
                similarity = self._cosine_similarity(query_embedding, s.embedding)
                if similarity > 0.3:
                    scores[s.slice_id] = similarity

        return scores

    # ── 路径 2: 三元组精确匹配 ──

    def _triple_search(
        self, entities: list[str], slices: list[DiarySlice]
    ) -> dict[str, float]:
        """三元组精确匹配：entity 在 DiarySlice.triple_subjects 中。"""
        if not entities or not slices:
            return {}

        scores: dict[str, float] = {}
        for s in slices:
            match_count = 0
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
        """关键词匹配：BM25 风格。"""
        if not query or not slices:
            return {}

        # 分词（简单按字符）
        query_chars = set(query)
        if not query_chars:
            return {}

        scores: dict[str, float] = {}
        for s in slices:
            text = f"{s.content} {s.summary} {' '.join(s.keywords)}"
            text_chars = set(text)

            # 字符重叠率
            overlap = len(query_chars & text_chars)
            if overlap > 0:
                score = overlap / len(query_chars)
                scores[s.slice_id] = min(1.0, score)

        return scores

    # ── 工具方法 ──

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """计算两个向量的余弦相似度。"""
        if not a or not b or len(a) != len(b):
            return 0.0

        dot_product = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot_product / (norm_a * norm_b)
