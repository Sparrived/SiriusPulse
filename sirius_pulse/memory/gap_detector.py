"""知识缺口检测。

检测"我知道我不知道什么"，影响 bot 行为。
只做"读"：检测缺口 → 影响 system prompt。
不做"写"：不追踪问答、不触发写入。
缺口消失方式：蒸馏管道自然运行 → 传记更新 → 缺口自然消失。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sirius_pulse.memory.biography.models import UserBiography

logger = logging.getLogger(__name__)

__all__ = ["GapType", "KnowledgeGap", "GapDetector"]


class GapType(str, Enum):
    """知识缺口类型。"""

    PROFILE_INCOMPLETE = "profile_incomplete"  # 传记信息不完整
    STALE_UNVERIFIED = "stale_unverified"  # 信息过时未验证
    INFERRED_UNVERIFIED = "inferred_unverified"  # 推断信息未确认
    UNRESOLVED_CONFLICT = "unresolved_conflict"  # 信息矛盾未解决


@dataclass
class KnowledgeGap:
    """知识缺口。"""

    gap_type: str
    domain: str  # 缺口领域: basic_info, relationships, identity, fact
    description: str
    importance: str = "medium"  # high / medium / low

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap_type": self.gap_type,
            "domain": self.domain,
            "description": self.description,
            "importance": self.importance,
        }


class GapDetector:
    """知识缺口检测器。

    纯规则检测，不使用 LLM。
    检测结果影响 bot 的 system prompt。
    """

    @staticmethod
    def detect(bio: UserBiography) -> list[KnowledgeGap]:
        """检测用户传记中的知识缺口。"""
        gaps: list[KnowledgeGap] = []

        # 传记信息不完整
        gaps.extend(GapDetector._detect_profile_gaps(bio))

        # 推断信息未确认
        gaps.extend(GapDetector._detect_unverified_gaps(bio))

        # 信息矛盾
        gaps.extend(GapDetector._detect_conflict_gaps(bio))

        return gaps

    @staticmethod
    def build_prompt_hint(gaps: list[KnowledgeGap]) -> str:
        """从缺口列表构建 system prompt 提示。"""
        if not gaps:
            return ""

        hints: list[str] = []

        if any(g.domain == "basic_info" for g in gaps):
            hints.append("你对这个用户了解有限，避免假设其个人信息。")

        if any(g.gap_type == GapType.STALE_UNVERIFIED for g in gaps):
            hints.append("这个用户的信息可能过时了，留意是否有更新。")

        if any(g.gap_type == GapType.UNRESOLVED_CONFLICT for g in gaps):
            hints.append("关于这个用户存在矛盾信息，回复时避免武断。")

        return "\n".join(hints)

    # ── 内部检测方法 ──

    @staticmethod
    def _detect_profile_gaps(bio: UserBiography) -> list[KnowledgeGap]:
        """检测传记信息不完整。"""
        gaps: list[KnowledgeGap] = []

        if not bio.short_bio or len(bio.short_bio) < 20:
            gaps.append(
                KnowledgeGap(
                    gap_type=GapType.PROFILE_INCOMPLETE,
                    domain="basic_info",
                    description="传记信息过少",
                    importance="high",
                )
            )

        if not bio.relationships:
            gaps.append(
                KnowledgeGap(
                    gap_type=GapType.PROFILE_INCOMPLETE,
                    domain="relationships",
                    description="未发现关系信息",
                    importance="medium",
                )
            )

        if not bio.identity_anchors:
            gaps.append(
                KnowledgeGap(
                    gap_type=GapType.PROFILE_INCOMPLETE,
                    domain="identity",
                    description="未提取到身份特征",
                    importance="medium",
                )
            )

        return gaps

    @staticmethod
    def _detect_unverified_gaps(bio: UserBiography) -> list[KnowledgeGap]:
        """检测推断信息未确认。"""
        gaps: list[KnowledgeGap] = []

        # 检查 uncertain 事实数
        if bio.uncertain_fact_count > 0:
            gaps.append(
                KnowledgeGap(
                    gap_type=GapType.INFERRED_UNVERIFIED,
                    domain="fact",
                    description=f"有 {bio.uncertain_fact_count} 条待确认信息",
                    importance="low",
                )
            )

        return gaps

    @staticmethod
    def _detect_conflict_gaps(bio: UserBiography) -> list[KnowledgeGap]:
        """检测信息矛盾。"""
        gaps: list[KnowledgeGap] = []

        # 检查被取代的事实数（说明存在过矛盾）
        if bio.superseded_fact_count > 3:
            gaps.append(
                KnowledgeGap(
                    gap_type=GapType.UNRESOLVED_CONFLICT,
                    domain="fact",
                    description=f"有 {bio.superseded_fact_count} 条被取代的信息",
                    importance="medium",
                )
            )

        return gaps
