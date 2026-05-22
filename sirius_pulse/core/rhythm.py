"""Rhythm analyzer: conversation state machine (paper §6.1).

Extends heat.py with:
- pace detection (accelerating/steady/decelerating/silent)
- topic stability
- attention window detection
- burst detection
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sirius_pulse.models.emotion import EmotionState
from sirius_pulse.core.cognition import extract_keywords


@dataclass(slots=True)
class RhythmAnalysis:
    """Conversation rhythm analysis result."""

    heat_level: str = "warm"           # cold | warm | hot | overheated
    heat_score: float = 0.0
    pace: str = "steady"               # accelerating | steady | decelerating | silent
    gap_since_last_message: float = 0.0  # seconds
    topic_stability: float = 0.5       # 0~1
    topic_drift: float = 0.0           # v1.3+: 0~1, 窗口内话题漂移度，高值=发生了话题转换
    collective_mood: EmotionState | None = None
    attention_window_open: bool = False
    burst_detected: bool = False
    conversation_flows: int = 1
    turn_gap_readiness: float = 0.5  # 0~1, how ready the conversation is for AI insertion


class RhythmAnalyzer:
    """Analyzes conversation rhythm beyond simple heat metrics."""

    def __init__(self) -> None:
        self._history: dict[str, list[dict[str, Any]]] = {}  # group_id -> message metadata list

    def analyze(
        self,
        group_id: str,
        messages: list[dict[str, Any]],  # {user_id, content, timestamp, ...}
    ) -> RhythmAnalysis:
        """Analyze rhythm from recent messages."""
        if not messages:
            return RhythmAnalysis(heat_level="cold", pace="silent")

        # Update history
        self._history.setdefault(group_id, [])
        self._history[group_id].extend(messages)
        self._history[group_id] = self._history[group_id][-100:]

        recent = messages[-20:]

        # Heat score (simplified from heat.py)
        heat_score = self._compute_heat(recent)
        heat_level = self._heat_level(heat_score)

        # Pace
        pace = self._compute_pace(recent)

        # Gap
        gap = self._compute_gap(recent)

        # Topic stability
        stability = self._compute_topic_stability(recent)

        # v1.3+: Topic drift detection
        drift = self._compute_topic_drift(recent)

        # Attention window
        attention_open = self._attention_window(recent, stability)

        # Burst detection
        burst = self._detect_burst(recent)

        # Flows (simplified: count unique user transitions)
        flows = self._count_flows(recent)

        # Turn gap readiness: detect natural breakpoints in conversation
        gap_readiness = self._compute_turn_gap_readiness(recent, stability, burst, drift)

        return RhythmAnalysis(
            heat_level=heat_level,
            heat_score=round(heat_score, 3),
            pace=pace,
            gap_since_last_message=gap,
            topic_stability=round(stability, 3),
            topic_drift=round(drift, 3),
            attention_window_open=attention_open,
            burst_detected=burst,
            conversation_flows=flows,
            turn_gap_readiness=round(gap_readiness, 3),
        )

    @staticmethod
    def _compute_heat(messages: list[dict[str, Any]]) -> float:
        if not messages:
            return 0.0
        # Message density in last 5 minutes
        now = datetime.now(timezone.utc)
        recent_count = 0
        for m in messages:
            ts = m.get("timestamp", "")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if (now - dt).total_seconds() <= 300:
                        recent_count += 1
                except (ValueError, TypeError):
                    pass
        density = min(1.0, recent_count / 6.0)
        unique_users = len({m.get("user_id") for m in messages})
        crowd = min(1.0, max(0, unique_users - 1) / 4.0)
        return 0.45 * density + 0.35 * crowd + 0.20 * 0.0

    @staticmethod
    def _heat_level(score: float) -> str:
        if score < 0.2:
            return "cold"
        if score < 0.5:
            return "warm"
        if score < 0.8:
            return "hot"
        return "overheated"

    @staticmethod
    def _compute_pace(messages: list[dict[str, Any]]) -> str:
        if len(messages) < 3:
            return "steady"
        intervals = []
        for i in range(1, len(messages)):
            t1 = _parse_ts(messages[i - 1].get("timestamp", ""))
            t2 = _parse_ts(messages[i].get("timestamp", ""))
            if t1 and t2:
                intervals.append((t2 - t1).total_seconds())
        if len(intervals) < 2:
            return "steady"
        # EWMA of interval changes
        avg_interval = sum(intervals) / len(intervals)
        recent_avg = sum(intervals[-3:]) / len(intervals[-3:])
        if recent_avg < avg_interval * 0.7:
            return "accelerating"
        if recent_avg > avg_interval * 1.5:
            return "decelerating"
        if avg_interval > 300:
            return "silent"
        return "steady"

    @staticmethod
    def _compute_gap(messages: list[dict[str, Any]]) -> float:
        if not messages:
            return 0.0
        last_ts = messages[-1].get("timestamp", "")
        last_dt = _parse_ts(last_ts)
        if last_dt:
            return max(0.0, (datetime.now(timezone.utc) - last_dt).total_seconds())
        return 0.0

    @staticmethod
    def _compute_topic_stability(messages: list[dict[str, Any]]) -> float:
        """Simple keyword overlap topic stability.

        v1.3+: 使用 extract_keywords() 替代字符级 split，支持 bigram 短语匹配。
        """
        if len(messages) < 2:
            return 0.5
        contents = [str(m.get("content", "")) for m in messages[-5:]]
        if not contents:
            return 0.5
        sets = [extract_keywords(text) for text in contents]
        overlaps = []
        for i in range(1, len(sets)):
            inter = sets[i - 1] & sets[i]
            union = sets[i - 1] | sets[i]
            if union:
                overlaps.append(len(inter) / len(union))
        return sum(overlaps) / len(overlaps) if overlaps else 0.5

    @staticmethod
    def _attention_window(messages: list[dict[str, Any]], stability: float) -> bool:
        if len(messages) < 3:
            return False
        unique_users = len({m.get("user_id") for m in messages[-5:]})
        return unique_users >= 3 and stability > 0.3

    @staticmethod
    def _detect_burst(messages: list[dict[str, Any]]) -> bool:
        """Detect if a user sent >=4 messages within 15 seconds."""
        from collections import defaultdict
        user_msgs: dict[str, list[datetime]] = defaultdict(list)
        for m in messages[-15:]:
            uid = m.get("user_id", "")
            dt = _parse_ts(m.get("timestamp", ""))
            if uid and dt:
                user_msgs[uid].append(dt)
        for timestamps in user_msgs.values():
            if len(timestamps) >= 4:
                span = (timestamps[-1] - timestamps[-4]).total_seconds()
                if span <= 15:
                    return True
        return False

    @staticmethod
    def _compute_topic_drift(messages: list[dict[str, Any]]) -> float:
        """计算滑动窗口内的全局话题漂移度 (0~1)。

        将消息窗口从中间切分，计算前后两半的关键词集 Jaccard 距离。
        高漂移 = 对话在前半段和后半段聊的不是同一件事，
        常用于检测渐变式话题转换（非相邻跳转，而是 10 轮内缓慢漂移）。

        v1.3+ 新增。
        """
        if len(messages) < 4:
            return 0.0
        contents = [str(m.get("content", "")) for m in messages]
        n = len(contents)
        half = n // 2
        first_kw: set[str] = set()
        second_kw: set[str] = set()
        for i, text in enumerate(contents):
            kws = extract_keywords(text)
            if i < half:
                first_kw.update(kws)
            else:
                second_kw.update(kws)
        if not first_kw or not second_kw:
            return 0.0
        jaccard = len(first_kw & second_kw) / len(first_kw | second_kw)
        return max(0.0, 1.0 - jaccard)

    @staticmethod
    def _compute_turn_gap_readiness(
        messages: list[dict[str, Any]], stability: float, burst: bool, drift: float = 0.0
    ) -> float:
        """Detect how ready the conversation is for AI insertion.

        High readiness = natural breakpoint (question, topic shift, silence).
        Low readiness = conversation in full flow, don't interrupt.

        v1.3+: 新增 drift 参数，话题漂移高时提高 readiness（自然插入点）。
        """
        if not messages:
            return 0.5
        readiness = 0.3  # base
        last = str(messages[-1].get("content", ""))
        last_lower = last.lower()

        # Low topic stability = potential turning point
        if stability < 0.3:
            readiness += 0.25

        # v1.3+: High topic drift = conversation has shifted topic significantly
        if drift > 0.6:
            readiness += 0.2
        elif drift > 0.4:
            readiness += 0.1

        # Question = seeking response
        if any(m in last for m in ["?", "？", "吗", "呢", "怎么", "为什么", "如何"]):
            readiness += 0.2

        # Topic transition words = natural gap
        transitions = ["对了", "不过", "话说回来", "说到", "顺便", "另外", "总之", "那么", "所以"]
        if any(w in last for w in transitions):
            readiness += 0.15

        # Short context-dependent message = conversational follow-up
        if len(last) <= 8:
            readiness += 0.1

        # Silence before last message = people re-engaging
        if len(messages) >= 2:
            t1 = _parse_ts(messages[-2].get("timestamp", ""))
            t2 = _parse_ts(messages[-1].get("timestamp", ""))
            if t1 and t2:
                gap = (t2 - t1).total_seconds()
                if gap > 120:
                    readiness += 0.15

        # Burst = don't interrupt
        if burst:
            readiness -= 0.35

        # Long monologue from same user = wait for them to finish
        if len(messages) >= 3:
            last_uid = messages[-1].get("user_id")
            if last_uid and all(m.get("user_id") == last_uid for m in messages[-3:]):
                readiness -= 0.2

        return max(0.0, min(1.0, readiness))

    @staticmethod
    def _count_flows(messages: list[dict[str, Any]]) -> int:
        """Count parallel conversation flows (simplified)."""
        if len(messages) < 4:
            return 1
        # Count topic shifts
        shifts = 0
        for i in range(1, len(messages)):
            if messages[i].get("user_id") != messages[i - 1].get("user_id"):
                shifts += 1
        return max(1, shifts // 3)


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
