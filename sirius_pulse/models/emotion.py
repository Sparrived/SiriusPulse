"""Emotion models: 2D valence-arousal with 19 basic emotion mappings."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BasicEmotion(Enum):
    """19 basic emotions mapped to (name_cn, valence, arousal)."""

    JOY = ("喜悦", 0.8, 0.7)
    CONTENTMENT = ("满足", 0.6, 0.3)
    RELIEF = ("释然", 0.5, 0.2)
    EXCITEMENT = ("兴奋", 0.9, 0.9)
    SADNESS = ("悲伤", -0.8, 0.2)
    GRIEF = ("悲痛", -0.9, 0.3)
    ANGER = ("愤怒", -0.7, 0.9)
    IRRITATION = ("恼怒", -0.5, 0.6)
    ANXIETY = ("焦虑", -0.6, 0.8)
    FEAR = ("恐惧", -0.8, 0.9)
    DISGUST = ("厌恶", -0.6, 0.5)
    SURPRISE = ("惊讶", 0.3, 0.9)
    TRUST = ("信任", 0.7, 0.4)
    ANTICIPATION = ("期待", 0.4, 0.6)
    LOVE = ("喜爱", 0.9, 0.5)
    LONELINESS = ("孤独", -0.7, 0.3)
    GRATITUDE = ("感激", 0.8, 0.4)
    HOPE = ("希望", 0.7, 0.5)
    NEUTRAL = ("中性", 0.0, 0.3)

    @property
    def name_cn(self) -> str:
        return self.value[0]

    @property
    def ref_valence(self) -> float:
        return self.value[1]

    @property
    def ref_arousal(self) -> float:
        return self.value[2]


def describe_mood(valence: float, arousal: float) -> str:
    """把她的情绪坐标说成一句人话，而不是 19 个标签里最近的那个词。

    她的基线在 (0.2, 0.3)，最近的标签恰好是「中性」：于是绝大多数时候提示词里写的
    都是「心情：中性」，等于告诉模型「没有情绪」。这里按愉悦度分档、再用唤醒度
    补一个状态词，平稳时也是「心情还不错、挺放松」这样有温度的说法。
    """
    v = max(-1.0, min(1.0, float(valence)))
    a = max(0.0, min(1.0, float(arousal)))
    high, low = a >= 0.6, a <= 0.25
    if v >= 0.5:
        return "特别开心，有点兴奋" if high else "心情很好"
    if v >= 0.25:
        if high:
            return "心情不错，挺来劲的"
        return "心情不错，懒洋洋的" if low else "心情不错"
    if v >= 0.05:
        return "心情平稳，挺放松" if not high else "心情平稳，有点坐不住"
    if v >= -0.2:
        if high:
            return "有点烦"
        return "有点提不起劲" if low else "心情平平"
    if v >= -0.5:
        return "有点烦躁" if high else "有点低落"
    return "很生气" if high else "心情很差"


@dataclass(slots=True)
class EmotionState:
    """2D emotion state with valence (-1~+1) and arousal (0~1)."""

    valence: float = 0.0
    arousal: float = 0.3
    basic_emotion: BasicEmotion | None = None
    intensity: float = 0.5
    confidence: float = 0.8

    def __post_init__(self) -> None:
        self.valence = max(-1.0, min(1.0, float(self.valence)))
        self.arousal = max(0.0, min(1.0, float(self.arousal)))
        self.intensity = max(0.0, min(1.0, float(self.intensity)))
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if self.basic_emotion is None:
            self.basic_emotion = self._map_to_basic_emotion()

    def _map_to_basic_emotion(self) -> BasicEmotion:
        min_dist = float("inf")
        closest = BasicEmotion.NEUTRAL
        for emotion in BasicEmotion:
            _, ev, ea = emotion.value
            dist = math.sqrt((self.valence - ev) ** 2 + (self.arousal - ea) ** 2)
            if dist < min_dist:
                min_dist = dist
                closest = emotion
        return closest

    def to_dict(self) -> dict[str, Any]:
        return {
            "valence": self.valence,
            "arousal": self.arousal,
            "basic_emotion": self.basic_emotion.name if self.basic_emotion else None,
            "intensity": self.intensity,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmotionState":
        be_raw = data.get("basic_emotion")
        be = BasicEmotion[be_raw] if be_raw else None
        return cls(
            valence=data.get("valence", 0.0),
            arousal=data.get("arousal", 0.3),
            basic_emotion=be,
            intensity=data.get("intensity", 0.5),
            confidence=data.get("confidence", 0.8),
        )


@dataclass(slots=True)
class EmpathyStrategy:
    """Empathy response strategy selected by EmotionAnalyzer."""

    strategy_type: str  # confirm_action | cognitive | action | share_joy | presence
    priority: int  # 1 highest, 4 lowest
    depth_level: int  # 1~3
    personalization_params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AssistantEmotionState:
    """Assistant's own persistent emotion state with inertia & recovery."""

    valence: float = 0.2
    arousal: float = 0.3
    inertia_factor: float = 0.3
    recovery_rate_per_10min: float = 0.1
    baseline_valence: float = 0.2
    baseline_arousal: float = 0.3
    last_updated_at: str = ""
    user_bias: dict[str, tuple[float, float]] = field(default_factory=dict)

    def update_from_interaction(
        self,
        user_emotion: EmotionState,
        user_id: str,
    ) -> None:
        """Update assistant emotion after an interaction, respecting inertia."""
        import datetime

        target_v = self.baseline_valence
        target_a = self.baseline_arousal
        if user_emotion.valence < -0.3:
            target_v = 0.1
            target_a = 0.5
        elif user_emotion.valence > 0.5:
            target_v = 0.6
            target_a = 0.5
        bias = self.user_bias.get(user_id)
        if bias:
            target_v = (target_v + bias[0]) / 2
            target_a = (target_a + bias[1]) / 2
        max_delta_v = abs(target_v - self.valence) * self.inertia_factor
        max_delta_a = abs(target_a - self.arousal) * self.inertia_factor
        if target_v > self.valence:
            self.valence = min(target_v, self.valence + max_delta_v)
        else:
            self.valence = max(target_v, self.valence - max_delta_v)
        if target_a > self.arousal:
            self.arousal = min(target_a, self.arousal + max_delta_a)
        else:
            self.arousal = max(target_a, self.arousal - max_delta_a)
        self.valence = max(-1.0, min(1.0, self.valence))
        self.arousal = max(0.0, min(1.0, self.arousal))
        self.last_updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def apply_event(self, valence_delta: float, arousal_delta: float = 0.0) -> None:
        """Nudge her mood from something in her own life (finished a step, got woken up).

        Interactions pull her toward a target; life events push her by a small,
        bounded amount instead, so a good afternoon can lift her mood without any
        conversation at all.  Each push is clamped to ±0.2.
        """
        import datetime

        dv = max(-0.2, min(0.2, float(valence_delta)))
        da = max(-0.2, min(0.2, float(arousal_delta)))
        self.valence = max(-1.0, min(1.0, self.valence + dv))
        self.arousal = max(0.0, min(1.0, self.arousal + da))
        self.last_updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def shift_baseline(self, valence_delta: float, *, limit: float = 0.05) -> None:
        """Move the mood she recovers toward, by at most *limit* per call.

        The baseline is her temperament, so it only ever drifts slowly (one
        nightly reflection at a time) and stays within a moderate band.
        """
        dv = max(-limit, min(limit, float(valence_delta)))
        self.baseline_valence = round(max(-0.4, min(0.6, self.baseline_valence + dv)), 4)

    def tick_recovery(self) -> None:
        """Gradually drift back to baseline (call periodically)."""
        for attr, base in (("valence", self.baseline_valence), ("arousal", self.baseline_arousal)):
            current = getattr(self, attr)
            if current > base:
                setattr(self, attr, max(base, current - self.recovery_rate_per_10min))
            elif current < base:
                setattr(self, attr, min(base, current + self.recovery_rate_per_10min))
