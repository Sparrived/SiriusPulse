"""信号分析结果 — 规则计算层的输出。

替代 IntentAnalysisV3，仅包含规则计算结果，不包含 LLM 调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sirius_pulse.models.emotion import EmotionState


@dataclass
class SignalAnalysis:
    """规则计算层的输出信号。

    所有字段由纯规则计算得出，不涉及 LLM 调用。
    用于粗筛阈值判断和参与决策。
    """

    # ── 情绪 ──
    emotion: EmotionState | None = None

    # ── 指向性 ──
    directed_score: float = 0.0  # 综合指向分 0.0-1.0
    is_mentioned: bool = False  # 是否被 @ 或明确提及
    is_question: bool = False  # 是否是问句
    is_imperative: bool = False  # 是否是祈使句

    # ── 紧迫度 ──
    urgency_score: float = 0.0  # 0-100
    relevance_score: float = 0.0  # 0.0-1.0
    social_intent: str = "social"  # help_seeking|emotional|social|silent

    # ── 社交信号 ──
    sarcasm_score: float = 0.0
    entitlement_score: float = 0.5

    # ── 节奏 ──
    heat_level: str = "warm"  # cold|warm|hot|overheated
    pace: str = "steady"  # accelerating|steady|decelerating|silent
    burst_detected: bool = False
    turn_gap_readiness: float = 0.0

    # ── 附注 ──
    image_caption: str = ""
    search_query: str = ""

    # ── 共情策略 ──
    empathy: Any = None  # EmpathyStrategy

    # ── 参与决策 ──
    participation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（用于 cognition_store 持久化）。"""
        return {
            "directed_score": self.directed_score,
            "is_mentioned": self.is_mentioned,
            "is_question": self.is_question,
            "is_imperative": self.is_imperative,
            "urgency_score": self.urgency_score,
            "relevance_score": self.relevance_score,
            "social_intent": self.social_intent,
            "sarcasm_score": self.sarcasm_score,
            "entitlement_score": self.entitlement_score,
            "heat_level": self.heat_level,
            "pace": self.pace,
            "burst_detected": self.burst_detected,
            "turn_gap_readiness": self.turn_gap_readiness,
            "image_caption": self.image_caption,
            "search_query": self.search_query,
            "participation": dict(self.participation),
        }


__all__ = ["SignalAnalysis"]
