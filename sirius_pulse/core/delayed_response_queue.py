"""Delayed response queue: hold responses and trigger at natural timing.

Monitors conversation during the wait window:
- If topic gap appears → trigger immediately

IMMEDIATE 策略使用 5s 防抖窗口，窗口期内每收到一条新消息增加 1s，上限 12s。
在同 group 内合并连续消息，避免刷屏。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sirius_pulse.models.response_strategy import (
    DelayedResponseItem,
    ResponseStrategy,
    StrategyDecision,
)
from sirius_pulse.core.rhythm import RhythmAnalysis

logger = logging.getLogger(__name__)

_IMMEDIATE_DEBOUNCE_SECONDS = 5.0
_IMMEDIATE_WINDOW_MAX = 12.0

# Heat-based window multipliers: hotter groups = longer wait
_HEAT_WINDOW_MULT = {
    "cold": 0.7,
    "warm": 1.0,
    "hot": 1.5,
    "overheated": 2.5,
}

# Heat-based gap thresholds: hotter groups need longer silence to trigger
_HEAT_GAP_SECONDS = {
    "cold": 5.0,
    "warm": 10.0,
    "hot": 15.0,
    "overheated": 25.0,
}

# Pace modifiers for gap threshold
_PACE_GAP_MULT = {
    "accelerating": 1.5,
    "steady": 1.0,
    "decelerating": 0.5,
    "silent": 0.0,  # handled separately: trigger if window half-expired
}


class DelayedResponseQueue:
    """Queue for DELAYED and IMMEDIATE strategy responses."""

    def __init__(self) -> None:
        # group_id -> list of items
        self._queues: dict[str, list[DelayedResponseItem]] = {}

    def enqueue(
        self,
        group_id: str,
        user_id: str,
        message_content: str,
        strategy_decision: StrategyDecision,
        emotion_state: dict[str, Any] | None = None,
        candidate_memories: list[str] | None = None,
        channel: str | None = None,
        channel_user_id: str | None = None,
        multimodal_inputs: list[dict[str, str]] | None = None,
        adapter_type: str | None = None,
        heat_level: str = "warm",
        pace: str = "steady",
        speaker_name: str = "",
    ) -> DelayedResponseItem:
        """Add an item to the delayed queue.

        For IMMEDIATE strategy, if the same group already has a pending
        item, merge the message content.  Each additional IMMEDIATE
        message extends the window by 1 second (capped at 12s).
        """
        from sirius_pulse.core.utils import now_iso
        import html as _html
        from datetime import datetime, timedelta, timezone

        def _tag_content(content: str, sp: str, uid: str) -> str:
            safe_sp = _html.escape(sp or "有人", quote=True)
            safe_uid = _html.escape(uid or "", quote=True)
            now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
            return (
                f'<message speaker="{safe_sp}" user_id="{safe_uid}" time="{now_str}">'
                f"\n{content}\n</message>"
            )

        # Debounce: merge with any existing pending item in the same group.
        # This prevents multiple independent replies during high-frequency
        # message bursts; all messages within the debounce window are
        # consolidated into one prompt.
        queue = self._queues.get(group_id, [])
        for item in queue:
            if item.status == "pending":
                item.message_content += f"\n{_tag_content(message_content, speaker_name, channel_user_id or '')}"
                if strategy_decision.strategy == ResponseStrategy.IMMEDIATE:
                    if item.strategy_decision.strategy == ResponseStrategy.IMMEDIATE:
                        item.window_seconds = min(
                            item.window_seconds + 1.0, _IMMEDIATE_WINDOW_MAX
                        )
                    else:
                        item.window_seconds = _IMMEDIATE_DEBOUNCE_SECONDS
                    item.strategy_decision = strategy_decision
                else:
                    new_window = self._window_for_item(strategy_decision, heat_level)
                    item.window_seconds = min(item.window_seconds, new_window)
                # Update heat/pace to the latest state
                item.heat_level = heat_level
                item.pace = pace
                item.emotion_state.update(emotion_state or {})
                if candidate_memories:
                    item.candidate_memories.extend(candidate_memories)
                if multimodal_inputs:
                    item.multimodal_inputs.extend(multimodal_inputs)
                # Update caller identity to the latest message (most relevant for skill auth)
                item.user_id = user_id
                item.channel = channel
                item.channel_user_id = channel_user_id
                if adapter_type:
                    item.adapter_type = adapter_type
                # Track all users whose messages were merged into this item
                if user_id and user_id not in item.related_user_ids:
                    item.related_user_ids.append(user_id)
                logger.debug(
                    "Merged %s item %s for group %s (content now %d chars, window %.1fs)",
                    strategy_decision.strategy.value,
                    item.item_id,
                    group_id,
                    len(item.message_content),
                    item.window_seconds,
                )
                return item

        tagged_content = _tag_content(message_content, speaker_name, channel_user_id or "")
        item = DelayedResponseItem(
            item_id=f"dri_{uuid.uuid4().hex[:12]}",
            group_id=group_id,
            user_id=user_id,
            channel=channel,
            channel_user_id=channel_user_id,
            message_content=tagged_content,
            speaker_name=speaker_name,
            strategy_decision=strategy_decision,
            emotion_state=dict(emotion_state or {}),
            candidate_memories=list(candidate_memories or []),
            enqueue_time=now_iso(),
            window_seconds=self._window_for_item(strategy_decision, heat_level),
            status="pending",
            multimodal_inputs=list(multimodal_inputs or []),
            adapter_type=adapter_type,
            heat_level=heat_level,
            pace=pace,
            related_user_ids=[user_id] if user_id else [],
        )
        if group_id not in self._queues:
            self._queues[group_id] = []
        self._queues[group_id].append(item)
        logger.debug(
            "Enqueued %s item %s for group %s (window %.1fs, heat=%s, pace=%s)",
            strategy_decision.strategy.value,
            item.item_id,
            group_id,
            item.window_seconds,
            heat_level,
            pace,
        )
        return item

    def tick(
        self,
        group_id: str,
        recent_messages: list[dict[str, Any]],
        rhythm: RhythmAnalysis | None = None,
    ) -> list[DelayedResponseItem]:
        """Process queue for a group based on recent conversation.

        Returns items that should be triggered now.
        """
        queue = self._queues.get(group_id, [])
        if not queue:
            return []

        # Defensive: filter out corrupted dict entries (should never happen,
        # but protects against external mutation of _queues).
        clean_queue: list[DelayedResponseItem] = []
        for i in queue:
            if isinstance(i, DelayedResponseItem):
                clean_queue.append(i)
            else:
                logger.warning(
                    "DelayedResponseQueue: skipping corrupted entry in group %s (type=%s)",
                    group_id,
                    type(i).__name__,
                )
        self._queues[group_id] = clean_queue

        triggered: list[DelayedResponseItem] = []
        remaining: list[DelayedResponseItem] = []

        for item in clean_queue:
            if item.status != "pending":
                continue

            action = self._evaluate_item(item, recent_messages, rhythm)
            if action == "trigger":
                item.status = "triggered"
                triggered.append(item)
            else:
                remaining.append(item)

        self._queues[group_id] = remaining
        return triggered

    def cancel_all_for_user(self, group_id: str, user_id: str) -> int:
        """Cancel all pending items for a user in a group."""
        queue = self._queues.get(group_id, [])
        cancelled = 0
        for item in queue:
            if item.user_id == user_id and item.status == "pending":
                item.status = "cancelled"
                cancelled += 1
        return cancelled

    def get_pending(self, group_id: str) -> list[DelayedResponseItem]:
        """Get all pending items for a group."""
        return [i for i in self._queues.get(group_id, []) if i.status == "pending"]

    def has_pending(self, group_id: str) -> bool:
        """检查指定 group 是否有等待中的队列项。"""
        return any(i.status == "pending" for i in self._queues.get(group_id, []))

    def merge_incoming(
        self,
        group_id: str,
        user_id: str,
        message_content: str,
        speaker_name: str = "",
        channel: str | None = None,
        channel_user_id: str | None = None,
        multimodal_inputs: list[dict[str, str]] | None = None,
    ) -> bool:
        """轻量合并：将新消息合并进已有 pending 项，跳过完整管线。

        当 group 已有待触发的队列项时，直接追加消息内容，
        避免重复调用认知/决策 LLM。

        Returns:
            True 表示成功合并，False 表示无 pending 项（需走完整管线）。
        """
        queue = self._queues.get(group_id, [])
        for item in queue:
            if item.status != "pending":
                continue
            import html as _html
            from datetime import datetime, timedelta, timezone

            safe_sp = _html.escape(speaker_name or "有人", quote=True)
            safe_uid = _html.escape(channel_user_id or "", quote=True)
            now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
            tagged = (
                f'<message speaker="{safe_sp}" user_id="{safe_uid}" time="{now_str}">'
                f"\n{message_content}\n</message>"
            )
            item.message_content += f"\n{tagged}"
            if multimodal_inputs:
                item.multimodal_inputs.extend(multimodal_inputs)
            if user_id and user_id not in item.related_user_ids:
                item.related_user_ids.append(user_id)
            logger.debug(
                "管线短路合并: group=%s item=%s content=%d chars",
                group_id, item.item_id, len(item.message_content),
            )
            return True
        return False

    def clear_group(self, group_id: str) -> None:
        """Clear all items for a group."""
        self._queues.pop(group_id, None)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evaluate_item(
        self,
        item: DelayedResponseItem,
        recent_messages: list[dict[str, Any]],
        rhythm: RhythmAnalysis | None = None,
    ) -> str:
        """Evaluate whether to trigger or keep waiting."""
        now = datetime.now(timezone.utc)

        # Check if window expired
        enqueue_dt = _parse_iso(item.enqueue_time)
        if enqueue_dt:
            elapsed = (now - enqueue_dt).total_seconds()
            if elapsed >= item.window_seconds:
                logger.debug(
                    "Item %s triggered (window expired: %.1fs >= %.1fs)",
                    item.item_id,
                    elapsed,
                    item.window_seconds,
                )
                return "trigger"
            logger.debug(
                "Item %s waiting (elapsed %.1fs < window %.1fs)",
                item.item_id,
                elapsed,
                item.window_seconds,
            )

        # IMMEDIATE items only check window expiration (no topic gap)
        if item.strategy_decision.strategy == ResponseStrategy.IMMEDIATE:
            return "wait"

        # Pace-aware early trigger: if conversation went silent and
        # our window is at least half-expired, go ahead.
        if item.pace == "silent" and enqueue_dt:
            elapsed = (now - enqueue_dt).total_seconds()
            if elapsed >= item.window_seconds * 0.5:
                logger.debug(
                    "Item %s triggered (silent pace + half window: %.1fs >= %.1fs)",
                    item.item_id,
                    elapsed,
                    item.window_seconds * 0.5,
                )
                return "trigger"

        # DELAYED items also check topic gap (trigger)
        if recent_messages:
            last_msg_time = recent_messages[-1].get("timestamp", "")
            last_dt = _parse_iso(last_msg_time)
            if last_dt:
                gap = (now - last_dt).total_seconds()
                gap_threshold = self._gap_for_item(item, rhythm)
                if gap >= gap_threshold:
                    logger.debug(
                        "Delayed item %s triggered (topic gap: %.1fs >= %.1fs)",
                        item.item_id,
                        gap,
                        gap_threshold,
                    )
                    return "trigger"
                logger.debug(
                    "Delayed item %s waiting (topic gap: %.1fs < %.1fs)",
                    item.item_id,
                    gap,
                    gap_threshold,
                )

        return "wait"

    @staticmethod
    def _window_for_item(strategy_decision: StrategyDecision, heat_level: str = "warm") -> float:
        """Return debounce/wait window based on strategy, urgency, and heat."""
        if strategy_decision.strategy == ResponseStrategy.IMMEDIATE:
            return _IMMEDIATE_DEBOUNCE_SECONDS
        if strategy_decision.urgency >= 70:
            base = 15.0
        elif strategy_decision.urgency >= 40:
            base = 30.0
        else:
            base = 60.0
        mult = _HEAT_WINDOW_MULT.get(heat_level, 1.0)
        return base * mult

    @staticmethod
    def _gap_for_item(item: DelayedResponseItem, rhythm: RhythmAnalysis | None = None) -> float:
        """Compute dynamic topic-gap threshold for a queued item.

        Considers both the heat level at enqueue time and the current pace.
        """
        base = _HEAT_GAP_SECONDS.get(item.heat_level, 10.0)
        # If live rhythm is provided, apply pace modifier
        if rhythm is not None:
            pace_mult = _PACE_GAP_MULT.get(rhythm.pace, 1.0)
            # silent pace is handled separately in _evaluate_item (half-window trigger)
            if rhythm.pace == "silent":
                pace_mult = 1.0
            base *= pace_mult
        else:
            # Fallback: use item's own pace snapshot
            pace_mult = _PACE_GAP_MULT.get(item.pace, 1.0)
            if item.pace == "silent":
                pace_mult = 1.0
            base *= pace_mult
        return max(3.0, base)  # Minimum 3-second gap


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
