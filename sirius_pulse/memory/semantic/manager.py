from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from sirius_pulse.core.constants import FEEDBACK_TIMEOUT_SECONDS
from sirius_pulse.memory.semantic.models import (
    AtmosphereSnapshot,
    GroupSemanticProfile,
    ResponseRecord,
    UserSemanticProfile,
)
from sirius_pulse.memory.storage import MemoryStorage

logger = logging.getLogger(__name__)

_MAX_ATMOSPHERE_HISTORY = 100
_MAX_PENDING_RECORDS = 20
_FEEDBACK_WINDOW = 20
_FEEDBACK_TIMEOUT_S = FEEDBACK_TIMEOUT_SECONDS

_EMOJI_PATTERN = re.compile(
    "["
    "\U0001f600-\U0001f64f"
    "\U0001f300-\U0001f5ff"
    "\U0001f680-\U0001f6ff"
    "\U0001f1e0-\U0001f1ff"
    "\U00002702-\U000027b0"
    "\U000024c2-\U0001f251"
    "]+",
    flags=re.UNICODE,
)


class SemanticMemoryManager:
    """Manages semantic profiles with SQLite persistence.

    - Group norms: inferred from message stream (passive learning)
    - Atmosphere history: recorded after each cognition cycle
    - User interaction count: incremented per message
    - Response feedback: AI 发言后记录锚点，用户跟进时结算 engagement
    """

    def __init__(self, work_path: Any, storage: MemoryStorage | None = None) -> None:
        if storage is not None:
            self._storage = storage
            self._owns_storage = False
        else:
            from pathlib import Path

            db_path = Path(work_path) / "memory.db" if work_path else ":memory:"
            self._storage = MemoryStorage(db_path)
            self._owns_storage = True

        self._groups: dict[str, GroupSemanticProfile] = {}
        self._users: dict[str, UserSemanticProfile] = {}

    def close(self) -> None:
        if self._owns_storage:
            self._storage.close()

    # ------------------------------------------------------------------
    # Group profiles
    # ------------------------------------------------------------------

    def ensure_group_profile(self, group_id: str) -> GroupSemanticProfile:
        if group_id not in self._groups:
            data = self._storage.get_group_semantic_profile(group_id)
            if data:
                self._groups[group_id] = GroupSemanticProfile.from_dict(data)
            else:
                self._groups[group_id] = GroupSemanticProfile(group_id=group_id)
        return self._groups[group_id]

    def get_group_profile(self, group_id: str) -> GroupSemanticProfile | None:
        return self.ensure_group_profile(group_id)

    def save_group_profile(self, group_id: str) -> None:
        profile = self._groups.get(group_id)
        if profile is not None:
            self._storage.save_group_semantic_profile(group_id, profile.to_dict())

    # ------------------------------------------------------------------
    # User profiles
    # ------------------------------------------------------------------

    def get_user_profile(self, group_id: str, user_id: str) -> UserSemanticProfile:
        key = f"{group_id}:{user_id}"
        if key not in self._users:
            data = self._storage.get_semantic_profile(group_id, user_id)
            if data:
                self._users[key] = UserSemanticProfile.from_dict(data)
            else:
                self._users[key] = UserSemanticProfile(user_id=user_id)
        return self._users[key]

    def save_user_profile(self, group_id: str, user_id: str) -> None:
        key = f"{group_id}:{user_id}"
        profile = self._users.get(key)
        if profile is not None:
            self._storage.save_semantic_profile(group_id, user_id, profile.to_dict())

    def set_user_profile_fields(
        self,
        group_id: str,
        user_id: str,
        *,
        name: str = "",
    ) -> None:
        profile = self.get_user_profile(group_id, user_id)
        if name:
            profile.name = name
        self.save_user_profile(group_id, user_id)

    def list_group_user_profiles(self, group_id: str) -> list[UserSemanticProfile]:
        profiles_data = self._storage.list_semantic_profiles(group_id)
        return [UserSemanticProfile.from_dict(d) for d in profiles_data]

    # ------------------------------------------------------------------
    # Passive learning: group norms from message stream
    # ------------------------------------------------------------------

    def learn_from_message(
        self,
        group_id: str,
        speaker_id: str,
        content: str,
        *,
        channel: str | None = None,
    ) -> None:
        profile = self.ensure_group_profile(group_id)
        profile.group_norms["last_active"] = datetime.now().isoformat()

    def record_atmosphere(
        self,
        group_id: str,
        snapshot: AtmosphereSnapshot,
    ) -> None:
        profile = self.ensure_group_profile(group_id)
        profile.atmosphere_history.append(snapshot)
        if len(profile.atmosphere_history) > _MAX_ATMOSPHERE_HISTORY:
            profile.atmosphere_history = profile.atmosphere_history[-_MAX_ATMOSPHERE_HISTORY:]
        self.save_group_profile(group_id)

    def record_interaction(self, group_id: str, user_id: str, timestamp: str) -> None:
        profile = self.get_user_profile(group_id, user_id)
        profile.record_interaction(timestamp)
        self.save_user_profile(group_id, user_id)

    def record_ai_sent(
        self,
        group_id: str,
        target_user_id: str,
        topic_hint: str = "",
        response_length: int = 0,
    ) -> None:
        profile = self.ensure_group_profile(group_id)
        record = ResponseRecord(
            sent_at=datetime.now().isoformat(),
            target_user_id=target_user_id,
            topic_hint=topic_hint,
            response_length=response_length,
        )
        profile.pending_ai_responses.append(record)
        if len(profile.pending_ai_responses) > _MAX_PENDING_RECORDS:
            profile.pending_ai_responses = profile.pending_ai_responses[-_MAX_PENDING_RECORDS:]
        self.save_group_profile(group_id)

    def settle_engagement(
        self,
        group_id: str,
        user_id: str,
        directed_score: float,
        timestamp: str,
    ) -> None:
        profile = self.ensure_group_profile(group_id)
        settled = False
        for record in reversed(profile.pending_ai_responses):
            if record.target_user_id == user_id and not record.was_engaged:
                try:
                    sent_dt = datetime.fromisoformat(record.sent_at)
                    reply_dt = datetime.fromisoformat(timestamp)
                    latency = (reply_dt - sent_dt).total_seconds()
                except (ValueError, TypeError):
                    latency = 0.0
                if latency < 0:
                    latency = 0.0
                if latency <= _FEEDBACK_TIMEOUT_S and directed_score >= 0.3:
                    record.was_engaged = True
                    record.engagement_latency_s = latency
                    user_profile = self.get_user_profile(group_id, user_id)
                    user_profile.engagement_rate = user_profile.engagement_rate * 0.9 + 0.1
                    self.save_user_profile(group_id, user_id)
                    settled = True
                break

        if settled:
            self.save_group_profile(group_id)


__all__ = ["SemanticMemoryManager"]
