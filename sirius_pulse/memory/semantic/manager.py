from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from sirius_pulse.memory.semantic.models import (
    AtmosphereSnapshot,
    GroupSemanticProfile,
    ResponseRecord,
    UserSemanticProfile,
)
from sirius_pulse.memory.semantic.store import SemanticProfileStore

logger = logging.getLogger(__name__)

_MAX_ATMOSPHERE_HISTORY = 100
_MAX_PENDING_RECORDS = 20
_FEEDBACK_WINDOW = 20
_FEEDBACK_TIMEOUT_S = 120

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
    """Manages semantic profiles with disk persistence.

    - Group norms: inferred from message stream (passive learning)
    - Atmosphere history: recorded after each cognition cycle
    - User interaction count: incremented per message
    - Response feedback: AI 发言后记录锚点，用户跟进时结算 engagement
    """

    def __init__(self, work_path: Any) -> None:
        self._store = SemanticProfileStore(work_path)
        self._groups: dict[str, GroupSemanticProfile] = {}
        self._users: dict[str, UserSemanticProfile] = {}

    # ------------------------------------------------------------------
    # Group profiles
    # ------------------------------------------------------------------

    def ensure_group_profile(self, group_id: str) -> GroupSemanticProfile:
        if group_id not in self._groups:
            loaded = self._store.load_group_profile(group_id)
            self._groups[group_id] = loaded or GroupSemanticProfile(group_id=group_id)
        return self._groups[group_id]

    def get_group_profile(self, group_id: str) -> GroupSemanticProfile | None:
        return self.ensure_group_profile(group_id)

    def save_group_profile(self, group_id: str) -> None:
        profile = self._groups.get(group_id)
        if profile is not None:
            self._store.save_group_profile(group_id, profile)

    # ------------------------------------------------------------------
    # User profiles
    # ------------------------------------------------------------------

    def get_user_profile(self, group_id: str, user_id: str) -> UserSemanticProfile:
        key = f"{group_id}:{user_id}"
        if key not in self._users:
            loaded = self._store.load_user_profile(group_id, user_id)
            if loaded is not None:
                self._users[key] = loaded
            else:
                self._users[key] = UserSemanticProfile(user_id=user_id)
        return self._users[key]

    def save_user_profile(self, group_id: str, user_id: str) -> None:
        key = f"{group_id}:{user_id}"
        profile = self._users.get(key)
        if profile is not None:
            self._store.save_user_profile(group_id, user_id, profile)

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
        return self._store.list_group_user_profiles(group_id)

    # ------------------------------------------------------------------
    # Passive learning: group norms from message stream
    # ------------------------------------------------------------------

    def learn_from_message(
        self,
        group_id: str,
        content: str,
        social_intent: str = "",
    ) -> None:
        profile = self.ensure_group_profile(group_id)
        norms = profile.group_norms
        text = content or ""
        length = len(text)

        old_count = norms.get("message_count", 0)
        new_count = old_count + 1
        old_avg = norms.get("avg_message_length", 0.0)
        norms["avg_message_length"] = (old_avg * old_count + length) / new_count
        norms["message_count"] = new_count

        bucket = "short" if length < 20 else "medium" if length < 100 else "long"
        dist = norms.get("length_distribution", {})
        dist[bucket] = dist.get(bucket, 0) + 1
        norms["length_distribution"] = dist

        has_emoji = bool(_EMOJI_PATTERN.search(text))
        emoji_total = norms.get("emoji_total", 0) + (1 if has_emoji else 0)
        norms["emoji_total"] = emoji_total
        norms["emoji_usage_rate"] = round(emoji_total / new_count, 4)

        has_mention = "@" in text
        mention_total = norms.get("mention_total", 0) + (1 if has_mention else 0)
        norms["mention_total"] = mention_total
        norms["mention_rate"] = round(mention_total / new_count, 4)

        from datetime import datetime, timezone
        hour = datetime.now(timezone.utc).hour
        hours = norms.get("active_hours", {})
        hours[str(hour)] = hours.get(str(hour), 0) + 1
        norms["active_hours"] = hours

        if social_intent:
            last = norms.get("last_intent", "")
            if last and social_intent != last:
                norms["topic_switches"] = norms.get("topic_switches", 0) + 1
            norms["last_intent"] = social_intent
            switches = norms.get("topic_switches", 0)
            norms["topic_switch_frequency"] = round(switches / new_count, 4)

        self.save_group_profile(group_id)

    # ------------------------------------------------------------------
    # Atmosphere recording
    # ------------------------------------------------------------------

    def record_atmosphere(
        self,
        group_id: str,
        valence: float,
        arousal: float,
        active_participants: int = 0,
    ) -> None:
        from sirius_pulse.core.utils import now_iso
        profile = self.ensure_group_profile(group_id)
        profile.atmosphere_history.append(
            AtmosphereSnapshot(
                timestamp=now_iso(),
                group_valence=valence,
                group_arousal=arousal,
                active_participants=active_participants,
            )
        )
        if len(profile.atmosphere_history) > _MAX_ATMOSPHERE_HISTORY:
            profile.atmosphere_history = profile.atmosphere_history[-_MAX_ATMOSPHERE_HISTORY:]
        self.save_group_profile(group_id)

    # ------------------------------------------------------------------
    # 反馈驱动：AI 发言后记录反馈锚点
    # ------------------------------------------------------------------

    def record_response_sent(
        self,
        group_id: str,
        user_id: str,
        topic_hint: str = "",
        response_length: int = 0,
    ) -> None:
        """AI 发送消息后调用，记录待反馈的 ResponseRecord。"""
        from sirius_pulse.core.utils import now_iso

        record = ResponseRecord(
            sent_at=now_iso(),
            target_user_id=user_id,
            topic_hint=topic_hint,
            response_length=response_length,
        )

        if user_id:
            profile = self.get_user_profile(group_id, user_id)
            profile.pending_responses.append(record)
            if len(profile.pending_responses) > _MAX_PENDING_RECORDS:
                profile.pending_responses = profile.pending_responses[-_MAX_PENDING_RECORDS:]
            self.save_user_profile(group_id, user_id)

        group_profile = self.ensure_group_profile(group_id)
        group_profile.pending_ai_responses.append(record)
        if len(group_profile.pending_ai_responses) > _MAX_PENDING_RECORDS:
            group_profile.pending_ai_responses = group_profile.pending_ai_responses[-_MAX_PENDING_RECORDS:]
        self.save_group_profile(group_id)

    # ------------------------------------------------------------------
    # 反馈驱动：用户消息到达时结算反馈
    # ------------------------------------------------------------------

    def resolve_pending_feedback(
        self,
        group_id: str,
        user_id: str,
        directed_score: float = 0.0,
    ) -> None:
        """用户发送消息时调用，结算该用户和群组中所有待反馈的 ResponseRecord。

        结算规则：
        - 120 秒内到达且 directed_score >= 0.3 → was_engaged = True（真正指向 AI 的回应）
        - 120 秒内到达但 directed_score < 0.3 → 不结算（群聊噪音，用户可能在跟别人聊）
        - 超过 120 秒的记录 → was_engaged = False（用户未跟进）
        - 结算后更新 engagement_rate + style_feedback 长度分桶统计
        """
        from sirius_pulse.core.utils import now_iso

        now_iso_str = now_iso()
        now_dt = datetime.fromisoformat(now_iso_str.replace("Z", "+00:00"))
        is_directed = directed_score >= 0.3

        # 结算用户级 pending
        if user_id:
            profile = self.get_user_profile(group_id, user_id)
            resolved = []
            still_pending = []
            for rec in profile.pending_responses:
                try:
                    sent_dt = datetime.fromisoformat(rec.sent_at.replace("Z", "+00:00"))
                    latency = (now_dt - sent_dt).total_seconds()
                except Exception:
                    logger.warning("语义记忆投票处理失败", exc_info=True)
                    still_pending.append(rec)
                    continue

                if 0 < latency <= _FEEDBACK_TIMEOUT_S:
                    if is_directed:
                        rec.was_engaged = True
                        rec.engagement_latency_s = round(latency, 2)
                        resolved.append(rec)
                    else:
                        # 群聊噪音：用户可能在跟别人聊，暂不结算
                        still_pending.append(rec)
                elif latency > _FEEDBACK_TIMEOUT_S:
                    rec.was_engaged = False
                    resolved.append(rec)
                else:
                    still_pending.append(rec)

            profile.pending_responses = still_pending
            self._recompute_engagement(profile, resolved)
            self.save_user_profile(group_id, user_id)

        # 结算群组级 pending
        group_profile = self.ensure_group_profile(group_id)
        grp_resolved = []
        grp_still_pending = []
        for rec in group_profile.pending_ai_responses:
            try:
                sent_dt = datetime.fromisoformat(rec.sent_at.replace("Z", "+00:00"))
                latency = (now_dt - sent_dt).total_seconds()
            except Exception:
                logger.warning("语义记忆分组投票处理失败", exc_info=True)
                grp_still_pending.append(rec)
                continue

            if 0 < latency <= _FEEDBACK_TIMEOUT_S:
                if is_directed:
                    rec.was_engaged = True
                    rec.engagement_latency_s = round(latency, 2)
                    grp_resolved.append(rec)
                else:
                    grp_still_pending.append(rec)
            elif latency > _FEEDBACK_TIMEOUT_S:
                rec.was_engaged = False
                grp_resolved.append(rec)
            else:
                grp_still_pending.append(rec)

        group_profile.pending_ai_responses = grp_still_pending
        self._recompute_group_engagement(group_profile, grp_resolved)
        self.save_group_profile(group_id)

    def _recompute_engagement(
        self, profile: UserSemanticProfile, new_records: list[ResponseRecord]
    ) -> None:
        """将新结算的记录纳入用户 engagement_rate 的滚动窗口。"""
        if not new_records:
            return
        engaged_count = sum(1 for r in new_records if r.was_engaged)
        total = len(new_records)
        old_rate = profile.engagement_rate
        alpha = 0.3
        batch_rate = engaged_count / total if total > 0 else 0.0
        profile.engagement_rate = round(old_rate * (1 - alpha) + batch_rate * alpha, 4)
        logger.debug(
            "User %s engagement: batch=%d/%d → rate %.3f→%.3f",
            profile.user_id, engaged_count, total, old_rate, profile.engagement_rate,
        )

    def _recompute_group_engagement(
        self, profile: GroupSemanticProfile, new_records: list[ResponseRecord]
    ) -> None:
        """将新结算的记录纳入群组 engagement_rate 的滚动窗口。"""
        if not new_records:
            return
        engaged_count = sum(1 for r in new_records if r.was_engaged)
        total = len(new_records)
        old_rate = profile.response_engagement_rate
        alpha = 0.3
        batch_rate = engaged_count / total if total > 0 else 0.0
        profile.response_engagement_rate = round(old_rate * (1 - alpha) + batch_rate * alpha, 4)
        logger.debug(
            "Group %s engagement: batch=%d/%d → rate %.3f→%.3f",
            profile.group_id, engaged_count, total, old_rate, profile.response_engagement_rate,
        )

    # ------------------------------------------------------------------
    # 用户交互记录（简化版，不再维护 RelationshipState）
    # ------------------------------------------------------------------

    def record_user_interaction(
        self,
        group_id: str,
        user_id: str,
    ) -> None:
        """用户发送消息时调用，记录交互次数和时间戳。"""
        from sirius_pulse.core.utils import now_iso

        profile = self.get_user_profile(group_id, user_id)
        profile.record_interaction(now_iso())
        self.save_user_profile(group_id, user_id)
