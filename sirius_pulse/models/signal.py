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
    用于粗筛阈值判断和注入主模型 prompt。
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

    def to_prompt_text(self) -> str:
        """生成注入主模型 system prompt 的信号摘要。"""
        lines: list[str] = []

        # 指向性
        if self.is_mentioned:
            lines.append("- 指向你: 是（被@或明确提及）")
        elif self.directed_score >= 0.5:
            lines.append(f"- 指向你: 可能 (directed_score={self.directed_score:.2f})")
        else:
            lines.append(f"- 指向你: 否 (directed_score={self.directed_score:.2f})")

        # 情绪
        if self.emotion:
            valence_label = "positive" if self.emotion.valence > 0.1 else "negative" if self.emotion.valence < -0.1 else "neutral"
            lines.append(
                f"- 情绪: {valence_label} "
                f"(valence={self.emotion.valence:.1f}, arousal={self.emotion.arousal:.1f})"
            )

        # 类型标签
        tags: list[str] = []
        if self.is_question:
            tags.append("问句")
        if self.is_imperative:
            tags.append("请求")
        if self.social_intent == "help_seeking":
            tags.append("求助")
        elif self.social_intent == "emotional":
            tags.append("情感表达")
        if tags:
            lines.append(f"- 类型: {', '.join(tags)}")

        # 节奏
        lines.append(f"- 群聊热度: {self.heat_level}, 节奏: {self.pace}")

        return "\n".join(lines)

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
