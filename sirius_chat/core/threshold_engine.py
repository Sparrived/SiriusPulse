"""Threshold engine: multi-factor dynamic threshold (paper §2.2.3).

    threshold = base_threshold × activity_factor × relationship_factor × time_factor
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sirius_chat.memory.semantic.models import RelationshipState

logger = logging.getLogger(__name__)


class ThresholdEngine:
    """Computes dynamic engagement threshold based on multiple factors."""

    def __init__(
        self,
        base_low: float = 0.30,
        base_high: float = 0.60,
    ) -> None:
        self.base_low = base_low
        self.base_high = base_high

    def compute(
        self,
        *,
        sensitivity: float = 0.5,
        heat_level: str = "warm",
        messages_per_minute: float = 0.0,
        relationship_state: RelationshipState | None = None,
        hour_of_day: int | None = None,
        sender_type: str = "human",
    ) -> float:
        """Compute dynamic threshold."""
        base = self.base_high - sensitivity * (self.base_high - self.base_low)
        activity = self._activity_factor(heat_level, messages_per_minute)
        relationship = self._relationship_factor(relationship_state)
        time_f = self._time_factor(hour_of_day)
        # peer-AI 消息的阈值更高（更难触发回复）
        peer_factor = 1.3 if sender_type == "other_ai" else 1.0
        threshold = base * activity * relationship * time_f * peer_factor
        return round(max(0.1, min(0.9, threshold)), 4)

    @staticmethod
    def _activity_factor(heat_level: str, messages_per_minute: float) -> float:
        mapping = {
            "cold": 0.8,
            "warm": 1.0,
            "hot": 1.3,
            "overheated": 1.6,
        }
        base = mapping.get(heat_level, 1.0)
        # Fine-tune by message rate
        if messages_per_minute > 6:
            base += 0.2
        elif messages_per_minute < 0.5:
            base -= 0.1
        return base

    @staticmethod
    def _relationship_factor(state: RelationshipState | None) -> float:
        if state is None:
            return 1.0

        # First interaction: lower threshold to be friendly
        if not state.first_interaction_at:
            return 0.7

        # Trust-based分层
        if state.trust_score < 0.3:
            return 1.2  # Low trust → harder to trigger
        if state.trust_score > 0.7:
            return 0.7  # High trust → easier to trigger

        # Familiarity-based分层
        familiarity = state.compute_familiarity()
        if familiarity >= 0.9:
            return 0.6
        if familiarity >= 0.6:
            return 0.8
        if familiarity >= 0.3:
            return 1.0
        return 1.1  # Slightly harder but not as harsh as before

    @staticmethod
    def _time_factor(hour: int | None) -> float:
        if hour is None:
            hour = datetime.now().hour
        if 0 <= hour < 6:
            return 1.3
        if 9 <= hour < 18:
            return 1.1
        if 19 <= hour < 23:
            return 0.9
        return 1.0
