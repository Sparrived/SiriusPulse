"""日记切片器：将长日记按主题切片。

切片策略：
1. 按 Situation 的 topics 分组
2. 同主题的多个 Situation 合并为一个切片
3. 每个切片携带对应的三元组索引
4. 计算每个切片的 embedding
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sirius_pulse.memory.diary.slice_models import DiarySlice
from sirius_pulse.memory.situation.models import Situation

logger = logging.getLogger(__name__)

__all__ = ["DiarySlicer"]


class DiarySlicer:
    """将长日记按 Situation 分组切片。"""

    # 每个切片最大字符数
    MAX_SLICE_CHARS = 800

    async def slice(
        self,
        diary_content: str,
        situations: list[Situation],
        group_id: str,
        diary_id: str,
        embedding_client: Any | None = None,
    ) -> list[DiarySlice]:
        """将日记内容按 Situation 主题切片。

        策略：
        1. 按 Situation 的 topics 分组
        2. 同主题的多个 Situation 合并为一个切片
        3. 每个切片携带对应的三元组索引
        4. 可选：计算 embedding

        Args:
            diary_content: 完整日记正文
            situations: 本次日记对应的 Situation 列表
            group_id: 群组 ID
            diary_id: 日记 ID
            embedding_client: 可选的 embedding 客户端

        Returns:
            DiarySlice 列表
        """
        if not situations:
            # 没有 Situation，整篇日记作为一个切片
            return [self._create_single_slice(
                diary_content, group_id, diary_id, [], embedding_client
            )]

        # 按主题分组 Situation
        topic_groups = self._group_by_topics(situations)

        slices: list[DiarySlice] = []
        for idx, (topic, group_situations) in enumerate(topic_groups.items()):
            # 从日记中提取与该主题相关的段落
            content = self._extract_relevant_paragraphs(
                diary_content, group_situations
            )

            if not content:
                continue

            # 收集三元组索引和关联情景
            all_subjects: set[str] = set()
            all_predicates: set[str] = set()
            all_record_ids: list[str] = []
            all_participants: set[str] = set()
            all_situation_ids: list[str] = []

            for sit in group_situations:
                for t in sit.triples:
                    all_subjects.add(t.subject)
                    all_predicates.add(t.predicate)
                    if getattr(t, "source_record_id", ""):
                        all_record_ids.append(t.source_record_id)
                all_participants.update(sit.participants)
                all_situation_ids.append(sit.situation_id)

            # 时间范围
            start = min(
                (s.time_range_start for s in group_situations if s.time_range_start),
                default="",
            )
            end = max(
                (s.time_range_end for s in group_situations if s.time_range_end),
                default="",
            )

            summary = self._build_slice_summary(group_situations)

            diary_slice = DiarySlice(
                slice_id=str(uuid.uuid4())[:8],
                diary_id=diary_id,
                group_id=group_id,
                content=content,
                summary=summary,
                keywords=list(all_subjects)[:5],
                topics=[topic] if topic else [],
                triple_subjects=list(all_subjects),
                triple_predicates=list(all_predicates),
                source_record_ids=all_record_ids,
                situation_ids=all_situation_ids,
                participants=list(all_participants),
                time_range_start=start,
                time_range_end=end,
                index=idx,
            )

            # 计算 embedding
            if embedding_client:
                try:
                    diary_slice.embedding = await embedding_client.embed(
                        f"{summary} {content[:200]}"
                    )
                except Exception:
                    pass

            slices.append(diary_slice)

        logger.info(
            "群 %s 日记切片完成: %d 个 Situation → %d 个切片",
            group_id, len(situations), len(slices),
        )
        return slices

    # ── 内部方法 ──

    def _group_by_topics(
        self, situations: list[Situation]
    ) -> dict[str, list[Situation]]:
        """按主题分组 Situation。

        没有主题的 Situation 归入 "其它" 组。
        """
        groups: dict[str, list[Situation]] = {}

        for sit in situations:
            if sit.topics:
                # 用第一个主题作为分组键
                topic = sit.topics[0]
                if topic not in groups:
                    groups[topic] = []
                groups[topic].append(sit)
            else:
                if "其它" not in groups:
                    groups["其它"] = []
                groups["其它"].append(sit)

        return groups

    def _extract_relevant_paragraphs(
        self,
        diary_content: str,
        situations: list[Situation],
    ) -> str:
        """从日记中提取与指定 Situation 相关的段落。

        策略：按段落分割，用 Situation 摘要中的关键词匹配。
        """
        paragraphs = [p.strip() for p in diary_content.split("\n") if p.strip()]

        if not paragraphs:
            return ""

        # 收集关键词
        keywords: set[str] = set()
        for sit in situations:
            for t in sit.triples:
                keywords.add(t.subject)
                keywords.add(t.obj)
            # 从摘要中提取关键词
            if sit.summary:
                for word in sit.summary:
                    if len(word) >= 2:
                        keywords.add(word)

        # 匹配段落
        relevant = []
        for para in paragraphs:
            if any(kw in para for kw in keywords if len(kw) >= 2):
                relevant.append(para)

        # 如果没有匹配到，返回整篇日记（后续切片会处理）
        if not relevant:
            return diary_content[:self.MAX_SLICE_CHARS]

        result = "\n".join(relevant)
        return result[:self.MAX_SLICE_CHARS]

    def _create_single_slice(
        self,
        content: str,
        group_id: str,
        diary_id: str,
        situations: list[Situation],
        embedding_client: Any | None = None,
    ) -> DiarySlice:
        """创建单个切片（无 Situation 分组时）。"""
        return DiarySlice(
            slice_id=str(uuid.uuid4())[:8],
            diary_id=diary_id,
            group_id=group_id,
            content=content[:self.MAX_SLICE_CHARS],
            summary=content[:50] if content else "",
            index=0,
        )

    @staticmethod
    def _build_slice_summary(situations: list[Situation]) -> str:
        """从 Situation 列表构建切片摘要。"""
        summaries = [s.summary for s in situations if s.summary]
        return "；".join(summaries[:3]) if summaries else ""
