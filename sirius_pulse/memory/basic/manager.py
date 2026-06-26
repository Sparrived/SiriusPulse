"""Basic memory manager: full retention window with heat tracking."""

from __future__ import annotations

import logging
import math
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from sirius_pulse.core.constants import (
    COLD_HEAT_THRESHOLD,
    DEFAULT_BASIC_MEMORY_CONTEXT_WINDOW,
    DEFAULT_BASIC_MEMORY_HARD_LIMIT,
    SILENCE_THRESHOLD_SECONDS,
)
from sirius_pulse.memory.basic.models import BasicMemoryEntry, HeatState

logger = logging.getLogger(__name__)

HARD_LIMIT = DEFAULT_BASIC_MEMORY_HARD_LIMIT
CONTEXT_WINDOW = DEFAULT_BASIC_MEMORY_CONTEXT_WINDOW
COLD_THRESHOLD = COLD_HEAT_THRESHOLD
SILENCE_THRESHOLD_SEC = SILENCE_THRESHOLD_SECONDS


class HeatCalculator:
    """Compute group chat heat based on message frequency, speakers, and recency."""

    @staticmethod
    def calculate(entries: list[BasicMemoryEntry]) -> float:
        """Return heat score in [0.0, 1.0]. Higher = hotter."""
        if not entries:
            return 0.0

        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - 300.0  # 5 minutes

        recent = [
            e
            for e in entries
            if e.timestamp
            and datetime.fromisoformat(e.timestamp.replace("Z", "+00:00")).timestamp() > cutoff
        ]

        msg_per_min = len(recent) / 5.0
        msg_rate_factor = min(1.0, msg_per_min / 5.0)

        unique_speakers = len({e.user_id for e in recent if e.user_id})
        unique_speakers_factor = min(1.0, unique_speakers / 5.0)

        last_ts = max(
            (
                datetime.fromisoformat(e.timestamp.replace("Z", "+00:00")).timestamp()
                for e in entries
                if e.timestamp
            ),
            default=now.timestamp(),
        )
        seconds_since_last = max(0.0, now.timestamp() - last_ts)
        recency_factor = math.exp(-seconds_since_last / SILENCE_THRESHOLD_SEC)

        heat = min(1.0, msg_rate_factor * 0.4 + unique_speakers_factor * 0.3 + recency_factor * 0.3)
        return round(heat, 4)

    @staticmethod
    def is_cold(heat: float, seconds_since_last: float) -> bool:
        """Check if group is cold enough for diary promotion."""
        return heat < COLD_THRESHOLD and seconds_since_last >= SILENCE_THRESHOLD_SEC


class BasicMemoryManager:
    """Manages per-group basic memory windows.

    - Retains raw messages in memory for later append-only archival.
    - Always keeps the most recent CONTEXT_WINDOW messages active.
    - Older messages are "archive candidates" for diary promotion.
    - Tracks heat per group for cold-detection.
    """

    def __init__(self, hard_limit: int = HARD_LIMIT, context_window: int = CONTEXT_WINDOW) -> None:
        self.hard_limit = hard_limit
        self.context_window = context_window
        self._windows: dict[str, deque[BasicMemoryEntry]] = {}
        self._heat_state: dict[str, HeatState] = {}
        self._heat_calc = HeatCalculator()

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def add_entry(
        self,
        group_id: str,
        user_id: str,
        role: str,
        content: str,
        *,
        speaker_name: str = "",
        system_prompt: str = "",
        timestamp: str | None = None,
        channel_user_id: str = "",
        platform_message_id: str = "",
        multimodal_inputs: list[dict[str, str]] | None = None,
        tags: list[dict[str, str]] | None = None,
        conversation_chain: list[dict[str, Any]] | None = None,
    ) -> BasicMemoryEntry:
        """Add an entry to a group's basic memory window."""
        gid = group_id or "default"
        from sirius_pulse.core.utils import now_iso

        entry = BasicMemoryEntry(
            entry_id=f"bme_{uuid.uuid4().hex[:12]}",
            group_id=gid,
            user_id=user_id,
            speaker_name=speaker_name,
            role=role,
            content=content,
            timestamp=timestamp or now_iso(),
            system_prompt=system_prompt,
            channel_user_id=channel_user_id,
            platform_message_id=platform_message_id,
            multimodal_inputs=[
                dict(item) for item in (multimodal_inputs or []) if isinstance(item, dict)
            ],
            tags=list(tags) if tags else [],
            conversation_chain=list(conversation_chain) if conversation_chain else [],
        )

        window = self._windows.setdefault(gid, deque())
        window.append(entry)

        # Update heat state
        self._update_heat(gid)

        return entry

    def get_context(self, group_id: str, n: int | None = None) -> list[BasicMemoryEntry]:
        """Get the most recent n entries for immediate context."""
        window = self._windows.get(group_id or "default", deque())
        count = n if n is not None else self.context_window
        return list(window)[-count:] if window else []

    def get_archive_candidates(self, group_id: str) -> list[BasicMemoryEntry]:
        """Get entries beyond the context window (candidates for diary promotion)."""
        window = self._windows.get(group_id or "default", deque())
        if len(window) <= self.context_window:
            return []
        return list(window)[: -self.context_window]

    def get_consolidation_candidates(
        self,
        group_id: str,
        *,
        include_context: bool = False,
    ) -> list[BasicMemoryEntry]:
        """Get raw messages eligible for diary consolidation.

        During normal chat flow the active context window stays out of diary
        generation. Once a group has been idle long enough, callers can include
        that active context so the whole finished conversation segment is
        summarized without deleting the raw entries.
        """
        if include_context:
            return self.get_all(group_id)
        return self.get_archive_candidates(group_id)

    def get_all(self, group_id: str) -> list[BasicMemoryEntry]:
        """Get all entries in a group's window."""
        return list(self._windows.get(group_id or "default", deque()))

    def clear_group(self, group_id: str) -> None:
        """Clear basic memory for a specific group."""
        self._windows.pop(group_id or "default", None)
        self._heat_state.pop(group_id or "default", None)

    def list_groups(self) -> list[str]:
        """Return all group IDs that have basic memory entries."""
        return list(self._windows.keys())

    def get_entries_by_user(
        self,
        user_id: str,
        *,
        exclude_group_id: str | None = None,
        n: int = 10,
    ) -> list[BasicMemoryEntry]:
        """Get recent entries for a specific user across all groups.

        Used for cross-group memory awareness. Only returns entries
        from groups other than exclude_group_id (typically the current group).
        """
        all_entries: list[BasicMemoryEntry] = []
        for gid, window in self._windows.items():
            if exclude_group_id and gid == exclude_group_id:
                continue
            for entry in window:
                if entry.user_id == user_id:
                    all_entries.append(entry)
        # Sort by timestamp descending, take most recent n
        all_entries.sort(
            key=lambda e: datetime.fromisoformat(e.timestamp.replace("Z", "+00:00")).timestamp()
            if e.timestamp
            else 0.0,
            reverse=True,
        )
        return all_entries[:n]

    # ------------------------------------------------------------------
    # Heat tracking
    # ------------------------------------------------------------------

    def compute_heat(self, group_id: str) -> float:
        """Compute current heat for a group."""
        entries = self.get_all(group_id)
        return self._heat_calc.calculate(entries)

    def is_cold(self, group_id: str) -> bool:
        """Check if group is cold enough for diary promotion."""
        entries = self.get_all(group_id)
        if not entries:
            return False
        _heat = self._heat_calc.calculate(entries)
        last_ts = max(
            (
                datetime.fromisoformat(e.timestamp.replace("Z", "+00:00")).timestamp()
                for e in entries
                if e.timestamp
            ),
            default=0.0,
        )
        seconds_since_last = datetime.now(timezone.utc).timestamp() - last_ts
        return self._heat_calc.is_cold(_heat, seconds_since_last)

    def _update_heat(self, group_id: str) -> None:
        entries = self.get_all(group_id)
        last_ts = max(
            (
                datetime.fromisoformat(e.timestamp.replace("Z", "+00:00")).timestamp()
                for e in entries
                if e.timestamp
            ),
            default=0.0,
        )
        recent = [
            e
            for e in entries
            if e.timestamp
            and datetime.fromisoformat(e.timestamp.replace("Z", "+00:00")).timestamp()
            > last_ts - 300.0
        ]
        self._heat_state[group_id] = HeatState(
            message_count_5min=len(recent),
            last_message_at=entries[-1].timestamp if entries else "",
            unique_speakers_5min=len({e.user_id for e in recent}),
            avg_interval_sec=(300.0 / len(recent)) if recent else 0.0,
        )

    def get_heat_state(self, group_id: str) -> HeatState | None:
        return self._heat_state.get(group_id)

    def get_cold_params(self, group_id: str) -> tuple[float, float]:
        """获取冷检测参数：(heat, seconds_since_last)。

        供 ColdDetector 使用。
        """
        entries = self.get_all(group_id)
        if not entries:
            return 0.0, 999999.0
        heat = self._heat_calc.calculate(entries)
        last_ts = max(
            (
                datetime.fromisoformat(e.timestamp.replace("Z", "+00:00")).timestamp()
                for e in entries
                if e.timestamp
            ),
            default=0.0,
        )
        seconds_since_last = datetime.now(timezone.utc).timestamp() - last_ts
        return heat, seconds_since_last

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {gid: [e.to_dict() for e in entries] for gid, entries in self._windows.items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BasicMemoryManager":
        mgr = cls()
        for gid, entries in data.items():
            for e in entries:
                if isinstance(e, dict):
                    mgr._windows.setdefault(gid, deque()).append(BasicMemoryEntry.from_dict(e))
            mgr._update_heat(gid)
        return mgr

    def restore_from_snapshot(self, group_id: str, entries: list[dict[str, Any]]) -> None:
        """从工作记忆快照恢复一个群的上下文。

        Args:
            group_id: 群组 ID。
            entries: 快照条目列表，每条包含 user_id, role, content, timestamp。
        """
        if not entries:
            return
        window = self._windows.setdefault(group_id, deque())
        for e in entries:
            if isinstance(e, dict):
                window.append(BasicMemoryEntry.from_dict(e))
        self._update_heat(group_id)
