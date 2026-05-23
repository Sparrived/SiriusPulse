"""Background tasks for EmotionalGroupChatEngine.

Delayed queue ticker, proactive checker, diary promoter/consolidator,
developer chat checker, and sticker novelty updater.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sirius_pulse.core.delayed_response_queue import _parse_iso
from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

_Base = _EmotionalGroupChatEngineBase

from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.core.prompt_factory import PromptFactory
from sirius_pulse.skills.executor import strip_skill_calls

logger = logging.getLogger(__name__)


class BackgroundTasksMixin(_Base):
    """Mixin providing background task methods for EmotionalGroupChatEngine."""

    # ==================================================================
    # Background tasks
    # ==================================================================

    def start_background_tasks(self) -> None:
        """Start periodic background tasks for delayed queue, proactive triggers,
        and memory promotion. Idempotent: safe to call multiple times.
        """
        if self._bg_running:
            return
        self._bg_running = True

        tasks = [
            asyncio.create_task(self._bg_delayed_queue_ticker(), name="delayed_queue"),
            asyncio.create_task(self._bg_proactive_checker(), name="proactive_check"),
            asyncio.create_task(self._bg_diary_promoter(), name="diary_promote"),
            asyncio.create_task(self._bg_diary_consolidator(), name="diary_consolidator"),
            asyncio.create_task(self._bg_proactive_developer_chat_checker(), name="dev_chat"),
        ]
        for t in tasks:
            self._bg_tasks.add(t)
            t.add_done_callback(self._bg_tasks.discard)

    def stop_background_tasks(self) -> None:
        """Cancel all background tasks and run passive SKILL unload hooks."""
        self._bg_running = False
        for t in list(self._bg_tasks):
            t.cancel()
        self._bg_tasks.clear()

        # 执行被动 SKILL on_unload 钩子（通过 ensure_future 调度，快速清理资源）
        for ctx, factory in getattr(self, "_passive_skill_unloaders", []):
            try:
                coro = factory(ctx)
                if coro is not None and asyncio.iscoroutine(coro):
                    asyncio.ensure_future(coro)
            except Exception as exc:
                logger.warning("被动SKILL on_unload 失败: %s", exc)
        if hasattr(self, "_passive_skill_unloaders"):
            self._passive_skill_unloaders.clear()

    async def _bg_delayed_queue_ticker(self) -> None:
        """Smart-sleep ticker for the delayed queue.

        Wakes up at the next pending item's expiry time (or max interval)
        and emits DELAYED_RESPONSE_TRIGGERED events for expired items only.
        Actual reply generation and delivery is handled by the external
        caller via tick_delayed_queue().
        """
        max_interval = self.config.get("delayed_queue_tick_interval_seconds", 10)
        while self._bg_running:
            # Compute how long we can sleep until the next item expires
            next_wake = max_interval
            now = datetime.now(timezone.utc)
            for group_id in list(self._group_last_message_at.keys()):
                for item in self.delayed_queue.get_pending(group_id):
                    enqueue_dt = _parse_iso(item.enqueue_time)
                    if enqueue_dt:
                        remaining = item.window_seconds - (now - enqueue_dt).total_seconds()
                        if remaining <= 0:
                            next_wake = 0
                            break
                        next_wake = min(next_wake, remaining)
                    if next_wake <= 0:
                        break
                if next_wake <= 0:
                    break

            # Guard against busy-loop when items are already expired but not yet
            # consumed by the external delivery loop.
            if next_wake <= 0:
                next_wake = 1.0

            await asyncio.sleep(next_wake)

            now = datetime.now(timezone.utc)
            for group_id in list(self._group_last_message_at.keys()):
                try:
                    pending = self.delayed_queue.get_pending(group_id)
                    # Per-group emitted tracking: only clean up IDs that no longer
                    # exist in this group's pending list.
                    emitted = self._delayed_event_emitted.setdefault(group_id, set())
                    existing_ids = {i.item_id for i in pending}
                    emitted &= existing_ids

                    expired = []
                    for item in pending:
                        enqueue_dt = _parse_iso(item.enqueue_time)
                        if enqueue_dt and (now - enqueue_dt).total_seconds() >= item.window_seconds:
                            expired.append(item)

                    newly_expired = [i for i in expired if i.item_id not in emitted]
                    if newly_expired:
                        self._log_inner_thought("之前记下的延迟回复，现在该开口了～")
                        for item in newly_expired:
                            emitted.add(item.item_id)
                            await self.event_bus.emit(
                                SessionEvent(
                                    type=SessionEventType.DELAYED_RESPONSE_TRIGGERED,
                                    data={
                                        "group_id": group_id,
                                        "item_id": item.item_id,
                                    },
                                )
                            )
                except Exception as exc:
                    logger.warning("Delayed queue tick failed for %s: %s", group_id, exc)

    async def _bg_proactive_checker(self) -> None:
        """Periodically check proactive triggers for all active groups."""
        import random
        interval = self.config.get("proactive_check_interval_seconds", 60)
        while self._bg_running:
            await asyncio.sleep(interval)
            # 动态重新读取体验配置，使WebUI的proactive_enabled立即生效
            if not self._load_proactive_global_enabled():
                continue
            group_ids = list(self._group_last_message_at.keys())
            for i, group_id in enumerate(group_ids):
                # 群间添加随机抖动（0~15秒），避免多群同时触发
                if i > 0:
                    await asyncio.sleep(random.uniform(0, 15))
                try:
                    result = await self.proactive_check(group_id)
                    if result and result.get("reply"):
                        self._log_inner_thought("群里安静了好一会儿，我主动打破沉默吧...")
                except Exception as exc:
                    logger.warning("Proactive check failed for %s: %s", group_id, exc)

    async def _bg_diary_promoter(self) -> None:
        """Periodically promote basic memory entries to diary summaries.

        Trigger conditions (OR):
        1. Group is cold (heat < threshold AND silence >= threshold).
        2. Sufficient volume of undiarized archive candidates.
        """
        interval = self.config.get("memory_promote_interval_seconds", 180)
        volume_threshold = self.config.get("diary_volume_threshold", 8)
        while self._bg_running:
            await asyncio.sleep(interval)
            try:
                if self.provider_async is None:
                    continue

                promoted_total = 0
                for group_id in list(self.basic_memory.list_groups()):
                    candidates = self.basic_memory.get_archive_candidates(group_id)
                    if not candidates:
                        continue

                    # Filter out already diarized candidates
                    candidates = [
                        c
                        for c in candidates
                        if not self.diary_manager.is_source_diarized(group_id, c.entry_id)
                    ]
                    if not candidates:
                        continue

                    # Trigger: cold group OR sufficient undiarized volume
                    should_promote = (
                        self.basic_memory.is_cold(group_id) or len(candidates) >= volume_threshold
                    )
                    if not should_promote:
                        continue

                    import time

                    cfg = self.model_router.resolve("memory_extract")
                    t0 = time.perf_counter()
                    result = await self.diary_manager.generate_from_candidates(
                        group_id=group_id,
                        candidates=candidates,
                        persona_name=self.persona.name,
                        persona_description=(
                            self.persona.persona_summary or self.persona.backstory or ""
                        ),
                        brain=self.brain,
                        model_name=cfg.model_name,
                    )
                    diary_duration_ms = round((time.perf_counter() - t0) * 1000, 2)
                    self._record_subtask_tokens(
                        task_name="diary_generate",
                        model_name=cfg.model_name,
                        group_id=group_id,
                        duration_ms=diary_duration_ms,
                    )
                    if result:
                        promoted_total += 1
                        # Update semantic memory with LLM-extracted topics
                        profile = self.semantic_memory.ensure_group_profile(group_id)
                        if result.dominant_topic:
                            profile.dominant_topic = result.dominant_topic
                        for topic in result.interest_topics:
                            if topic and topic not in profile.interest_topics:
                                profile.interest_topics.append(topic)
                        self.semantic_memory.save_group_profile(group_id)

                        # 攒消息到人物传记（零 LLM，条件更新）
                        if getattr(self, "biography_manager", None) is not None:
                            try:
                                await self._feed_biography_from_candidates(
                                    group_id, candidates, cfg.model_name
                                )
                            except Exception as exc:
                                logger.warning("传记攒消息失败: %s", exc)

                if promoted_total > 0:
                    self._log_inner_thought(
                        f"整理了 {promoted_total} 个群的对话日记，过去的回忆又清晰了一点～"
                    )
            except Exception as exc:
                logger.warning("Diary promotion failed: %s", exc)

    async def _feed_biography_from_candidates(
        self,
        group_id: str,
        candidates: list[Any],
        model_name: str,
    ) -> None:
        """从日记候选消息中攒消息到各自传记。

        与日记一致：使用全部 candidates，不做预过滤。每个用户都拿到
        完整对话上下文，LLM 在更新传记时自行蒸馏出与目标用户相关的信息。
        """
        mgr = getattr(self, "biography_manager", None)
        if mgr is None:
            return

        # 构建全部消息的统一格式列表（过滤 assistant 和 system）
        all_messages: list[str] = []
        user_ids: set[str] = set()
        user_name_map: dict[str, str] = {}
        for entry in candidates:
            uid = getattr(entry, "user_id", "")
            if uid in ("assistant", "system", ""):
                continue
            speaker = getattr(entry, "speaker_name", "") or uid
            all_messages.append(f"{speaker}: {getattr(entry, 'content', '')}")
            user_ids.add(uid)
            if uid not in user_name_map:
                user_name_map[uid] = speaker

        if not user_ids:
            return

        # 每个用户都拿到全部对话上下文，先攒消息
        for user_id in user_ids:
            user_name = user_name_map.get(user_id, user_id)
            try:
                mgr.feed_messages(
                    user_id=user_id,
                    name=user_name,
                    group_id=group_id,
                    messages=all_messages,
                )
            except Exception as exc:
                logger.warning("传记攒消息失败 user=%s: %s", user_id, exc)

        # 层1：蒸馏 → 从原始消息提炼关于各用户的要点
        for user_id in user_ids:
            try:
                distilled = await mgr.maybe_distill(
                    user_id=user_id,
                    persona_name=self.persona.name,
                    brain=self.brain,
                    model_name=model_name,
                )
                if distilled:
                    self._record_subtask_tokens(
                        task_name="biography_distill",
                        model_name=model_name,
                        group_id=group_id,
                    )
            except Exception as exc:
                logger.warning("传记蒸馏失败 user=%s: %s", user_id, exc)

        # 层2：传记更新 → 从蒸馏要点构建传记卡
        for user_id in user_ids:
            try:
                updated = await mgr.maybe_update_biography(
                    user_id=user_id,
                    persona_name=self.persona.name,
                    brain=self.brain,
                    model_name=model_name,
                )
                if updated:
                    self._record_subtask_tokens(
                        task_name="biography_update",
                        model_name=model_name,
                        group_id=group_id,
                    )
            except Exception as exc:
                logger.warning("传记更新失败 user=%s: %s", user_id, exc)

    async def _bg_diary_consolidator(self) -> None:
        """Periodically consolidate diary entries via LLM merging."""
        interval = self.config.get("consolidation_interval_seconds", 600)
        while self._bg_running:
            await asyncio.sleep(interval)
            try:
                await self._run_diary_consolidation()
            except Exception as exc:
                logger.warning("Diary consolidation failed: %s", exc)

    async def _run_diary_consolidation(self) -> None:
        """Find similar diary entries and merge them via LLM."""
        import time

        from sirius_pulse.memory.diary.consolidator import DiaryConsolidator
        from sirius_pulse.core.brain import RawRequest

        consolidator = DiaryConsolidator(self.diary_manager, self.config)
        cfg = self.model_router.resolve("memory_extract")

        for group_id in list(self._group_last_message_at.keys()):
            try:
                clusters = await asyncio.to_thread(consolidator.find_clusters, group_id)
                if not clusters:
                    continue

                merged_entries: list[Any] = []
                for cluster in clusters:
                    system_prompt, user_content = consolidator.build_merge_prompt(cluster)
                    raw_request = RawRequest(
                        model=cfg.model_name,
                        system_prompt=system_prompt,
                        messages=[{"role": "user", "content": user_content}],
                        temperature=0.4,
                        max_tokens=2048,
                        purpose="diary_consolidate",
                    )
                    t0 = time.perf_counter()
                    raw = await self.brain.raw_call(raw_request)
                    consolidate_duration_ms = round((time.perf_counter() - t0) * 1000, 2)

                    from sirius_pulse.token.utils import PromptTokenBreakdown, estimate_tokens

                    sub_bd = PromptTokenBreakdown()
                    sub_bd.output_format = estimate_tokens(system_prompt)
                    sub_bd.user_message = estimate_tokens(user_content)
                    sub_bd.output_total = estimate_tokens(raw)
                    sub_bd.total = sub_bd.output_format + sub_bd.user_message + sub_bd.output_total

                    self._record_subtask_tokens(
                        task_name="diary_consolidate",
                        model_name=cfg.model_name,
                        group_id=group_id,
                        duration_ms=consolidate_duration_ms,
                        token_breakdown=sub_bd.to_dict(),
                    )
                    entry = consolidator.parse_merge_result(raw, cluster)
                    if entry:
                        merged_entries.append(entry)

                if merged_entries:
                    await asyncio.to_thread(
                        consolidator.rebuild_entries, group_id, clusters, merged_entries
                    )
                    self._log_inner_thought(
                        f"整理了 {len(clusters)} 组相似日记，合并成 {len(merged_entries)} 条喵~"
                    )
            except Exception as exc:
                logger.warning("Diary consolidation failed for %s: %s", group_id, exc)

    async def proactive_check(
        self,
        group_id: str,
        *,
        _now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Check if proactive trigger should fire for a group."""
        if not self.is_proactive_enabled(group_id):
            return None

        last_at = self._group_last_message_at.get(group_id)
        group_profile = self.semantic_memory.ensure_group_profile(group_id)

        trigger = self.proactive_trigger.check(
            group_id,
            last_message_at=last_at,
            group_atmosphere={
                "valence": (
                    getattr(group_profile.atmosphere_history[-1], "group_valence", 0.0)
                    if group_profile.atmosphere_history
                    else 0.0
                ),
            },
            _now=_now,
        )
        if not trigger:
            return None

        # Guard: do not send another proactive message if nobody replied to the last one.
        last_proactive_iso = self._last_proactive_at.get(group_id)
        if last_proactive_iso:
            last_proactive_dt = _parse_iso(last_proactive_iso)
            last_msg_dt = _parse_iso(last_at) if last_at else None
            if last_proactive_dt and (last_msg_dt is None or last_proactive_dt > last_msg_dt):
                return None

        # Check conversation gap readiness before proactive insertion
        recent = self._get_recent_messages(group_id, n=6)
        rhythm = self.rhythm_analyzer.analyze(group_id, recent)
        if rhythm.turn_gap_readiness < self.expressiveness.proactive_gap_threshold:
            # Conversation is in full flow, don't interrupt with proactive
            return None

        # Record proactive trigger timestamp
        now_iso = (_now if _now is not None else datetime.now(timezone.utc)).isoformat()
        self._last_proactive_at[group_id] = now_iso
        self.proactive_trigger._last_proactive[group_id] = now_iso
        self._save_proactive_state()

        # Generate proactive message
        bundle = self._build_proactive_prompt(trigger, group_id)
        style = self.style_adapter.adapt(pace="steady")
        # Use ContextAssembler to build full messages with diary RAG + XML history
        msgs, ca_breakdown = self.context_assembler.build_messages_with_breakdown(
            group_id=group_id,
            current_query=bundle.user_content or "...",
            system_prompt=bundle.system_prompt,
            search_query=bundle.user_content or "",
            recent_n=10,
            include_pending=True,
        )
        system_prompt = msgs[0]["content"]
        messages = msgs[1:]

        # Merge assembler breakdown into response-assembler breakdown
        token_breakdown = bundle.token_breakdown.to_dict() if bundle.token_breakdown else {}
        for key, val in ca_breakdown.items():
            if key == "diary":
                token_breakdown["memory"] = token_breakdown.get("memory", 0) + val
            else:
                token_breakdown[key] = token_breakdown.get(key, 0) + val

        raw_reply = await self.brain.generate_text(
            system_prompt,
            messages,
            group_id,
            style_params=style,
            post_process=True,
        )
        reply = raw_reply.strip()

        await self.event_bus.emit(
            SessionEvent(
                type=SessionEventType.PROACTIVE_RESPONSE_TRIGGERED,
                data={
                    "group_id": group_id,
                    "trigger_type": trigger["trigger_type"],
                    "reply": reply,
                },
            )
        )

        # clean_reply 由 Brain post-hook 完成（strip_skill_calls + 去重），
        # 若被去重抑制则为空字符串。
        clean_reply = strip_skill_calls(reply).strip()

        return {
            "strategy": "proactive",
            "trigger_type": trigger["trigger_type"],
            "reply": clean_reply,
        }

    # ------------------------------------------------------------------
    # Developer proactive private-chat memory conversations
    # ------------------------------------------------------------------

    async def _bg_proactive_developer_chat_checker(self) -> None:
        """Periodically generate proactive memory-oriented chats for developers.

        Window is short (default 5 min) so the AI can create more shared
        memories with the developer in private-chat contexts.
        """
        interval = self.config.get("proactive_developer_chat_interval_seconds", 1800)
        min_silence = self.config.get("proactive_developer_min_silence_seconds", 120)
        while self._bg_running:
            await asyncio.sleep(interval)
            now = datetime.now(timezone.utc).timestamp()
            for group_id in list(self._developer_private_groups):
                try:
                    if not self._should_chat_with_developer(group_id, now, min_silence, interval):
                        continue
                    reply = await self._generate_developer_chat(group_id)
                    if reply:
                        clean_dev = strip_skill_calls(reply).strip()
                        self._pending_developer_chats.setdefault(group_id, []).append(clean_dev)
                        self._last_developer_chat_at[group_id] = now
                        self._log_inner_thought(f"突然想跟开发者聊聊，发了条消息过去～")
                        await self.event_bus.emit(
                            SessionEvent(
                                type=SessionEventType.DEVELOPER_CHAT_TRIGGERED,
                                data={
                                    "group_id": group_id,
                                    "reply": clean_dev,
                                },
                            )
                        )
                except Exception as exc:
                    logger.warning("Developer chat check failed for %s: %s", group_id, exc)

    def _should_chat_with_developer(
        self,
        group_id: str,
        now: float,
        min_silence: float,
        interval: float,
    ) -> bool:
        """Check whether it's appropriate to proactively chat with a developer."""
        # Active hours check
        start = self.config.get("proactive_active_start_hour", 8)
        end = self.config.get("proactive_active_end_hour", 23)
        local_hour = datetime.fromtimestamp(now).hour
        if not (start <= local_hour < end):
            return False

        # Respect silence since last message
        last_msg_iso = self._group_last_message_at.get(group_id)
        if last_msg_iso:
            last_msg_dt = _parse_iso(last_msg_iso)
            if last_msg_dt and (now - last_msg_dt.timestamp()) < min_silence:
                return False

        # Respect interval since last proactive developer chat
        last_chat = self._last_developer_chat_at.get(group_id, 0)
        if now - last_chat < interval:
            return False

        # CRITICAL: Do not send a new proactive message if the developer
        # has not replied to the last one. We compare the timestamp of the
        # last human message against the last proactive chat timestamp.
        if last_chat > 0:
            if last_msg_iso:
                last_msg_dt = _parse_iso(last_msg_iso)
                if last_msg_dt and last_msg_dt.timestamp() <= last_chat:
                    # Developer has not replied since our last proactive msg
                    return False
            else:
                # No human message recorded at all, but we already chatted
                return False

        return True

    async def _generate_developer_chat(self, group_id: str) -> str | None:
        """Generate a memory-oriented proactive message for a developer."""
        user_id = group_id.replace("private_", "")

        topic = self._pick_developer_chat_topic(group_id, user_id, None)
        if not topic:
            return None

        from sirius_pulse.core.prompt_factory import PromptFactory

        sections = PromptFactory.build_developer_chat_sections("", topic, None)

        system_prompt = "\n\n".join(sections)
        messages = [{"role": "user", "content": "（你决定主动开口）"}]
        style = self.style_adapter.adapt(pace="steady")

        raw_reply = await self.brain.generate_text(
            system_prompt,
            messages,
            group_id,
            style_params=style,
            post_process=True,
        )
        reply = raw_reply.strip()

        # clean_reply 由 Brain post-hook 完成（strip_skill_calls + 去重），
        # 若被去重抑制则为空字符串。
        clean_reply = strip_skill_calls(reply).strip()

        return clean_reply or None

    def _pick_developer_chat_topic(
        self,
        group_id: str,
        user_id: str,
        user_profile: Any | None,
    ) -> str:
        """Pick a personal/memory-oriented topic for developer proactive chat."""
        import random

        candidates: list[str] = []

        # 1. Recent diary entries for this private group
        try:
            diary_entries = self.diary_manager.get_entries_for_group(group_id)
            if diary_entries:
                recent = sorted(
                    diary_entries,
                    key=lambda e: getattr(e, "created_at", ""),
                    reverse=True,
                )[:3]
                for entry in recent:
                    summary = getattr(entry, "summary", "") or getattr(entry, "content", "")[:60]
                    if summary:
                        candidates.append(f"刚才整理日记时看到这段记录：{summary}，挺有意思的。")
                        break
        except Exception:
            LOG.warning("读取日记摘要失败", exc_info=True)
            pass

        # 3. Preset memory-oriented templates
        templates = [
            "突然想到一个有趣的问题：如果你可以改变过去的一个决定，你会选哪个？",
            "今天整理记忆的时候，发现我们聊过很多有意思的东西，你最近有什么新发现吗？",
            "想和你分享一个刚想到的观点——你觉得 AI 和人类之间，最重要的是什么？",
            "突然有点好奇，你最近在做的事情进展怎么样了？",
            "翻到了以前的聊天记录，感觉时间过得好快，你最近过得怎么样？",
            "刚才想到一个话题，想听听你的看法：你觉得未来五年，什么技术会改变生活？",
            "突然想起我们第一次聊天的时候，那时候聊了什么来着？",
        ]
        candidates.extend(random.sample(templates, min(2, len(templates))))

        if not candidates:
            return ""

        return random.choice(candidates)

    def pop_developer_chats(self, group_id: str) -> list[str]:
        """Pop pending proactive developer chats for a group.

        Called by the external delivery loop to retrieve and send chats.
        """
        return self._pending_developer_chats.pop(group_id, [])

    # ------------------------------------------------------------------
    # Reminder (timer) support
    # ------------------------------------------------------------------

    def pop_reminders(self, group_id: str, adapter_type: str | None = None) -> list[str]:
        """Pop pending reminder messages for a group.

        Called by the external delivery loop to retrieve and send due reminders.
        If *adapter_type* is provided, only reminders created for that adapter
        are returned; unmatched items remain in the queue.
        """
        items = self._pending_reminders.pop(group_id, [])
        if adapter_type is None:
            return [i["text"] for i in items]
        matched = []
        unmatched = []
        for i in items:
            if i.get("adapter_type") == adapter_type:
                matched.append(i["text"])
            else:
                unmatched.append(i)
        if unmatched:
            self._pending_reminders[group_id] = unmatched
        return matched

    def _inject_group_id_into_latest_reminder(self, group_id: str) -> None:
        """Attach group_id and adapter_type to reminders that lack them."""
        if self._skill_executor is None:
            return
        try:
            store = self._skill_executor.get_data_store("reminder")
            reminders = list(store.get("reminders", []))
            if not reminders:
                return
            updated = False
            for r in reminders:
                if "group_id" not in r:
                    r["group_id"] = group_id
                    updated = True
                if "adapter_type" not in r:
                    r["adapter_type"] = self._current_adapter_type
                    updated = True
            if updated:
                store.set("reminders", reminders)
                store.save()
        except Exception as exc:
            logger.warning("Failed to inject group_id into reminder: %s", exc)

    async def tick_delayed_queue(
        self,
        group_id: str,
        on_partial_reply: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Process delayed response queue for a group.

        If multiple items trigger in the same tick, merge them into a single
        prompt so the model generates only one consolidated reply.
        Supports multi-round SKILL execution similar to immediate responses.

        Args:
            group_id: The group / private chat to tick.
            on_partial_reply: Optional async callable invoked immediately
                when non-skill text is extracted *before* skills are executed.
                This lets callers send "让我查一下…" in real time while
                the skill runs, rather than batching everything at the end.
        """
        recent = self._get_recent_messages(group_id, n=10)
        rhythm = self.rhythm_analyzer.analyze(group_id, recent)
        triggered = self.delayed_queue.tick(group_id, recent, rhythm)
        if not triggered:
            return []

        # Determine caller from the first triggered item
        caller_profile = None
        item = triggered[0]
        # Defensive: if _queues was corrupted externally, item may be a dict.
        if isinstance(item, dict):
            logger.warning(
                "tick_delayed_queue: triggered[0] is dict (item_id=%s), converting to DelayedResponseItem",
                item.get("item_id", "unknown"),
            )
            from sirius_pulse.models.response_strategy import (
                DelayedResponseItem,
                ResponseStrategy,
                StrategyDecision,
            )

            sd_raw = item.get("strategy_decision", {}) or {}
            try:
                strategy_val = sd_raw.get("strategy", "silent")
                if isinstance(strategy_val, str):
                    strategy_enum = ResponseStrategy(strategy_val)
                else:
                    strategy_enum = ResponseStrategy.SILENT
            except Exception:
                strategy_enum = ResponseStrategy.SILENT
            strategy_decision = StrategyDecision(
                strategy=strategy_enum,
                score=float(sd_raw.get("score", 0.0)),
                threshold=float(sd_raw.get("threshold", 0.5)),
                urgency=float(sd_raw.get("urgency", 0.0)),
                relevance=float(sd_raw.get("relevance", 0.0)),
                reason=str(sd_raw.get("reason", "")),
                estimated_delay_seconds=float(sd_raw.get("estimated_delay_seconds", 0.0)),
                context=dict(sd_raw.get("context", {})),
            )
            item = DelayedResponseItem(
                item_id=item.get("item_id", ""),
                group_id=item.get("group_id", group_id),
                user_id=item.get("user_id", ""),
                channel=item.get("channel"),
                channel_user_id=item.get("channel_user_id"),
                message_content=item.get("message_content", ""),
                strategy_decision=strategy_decision,
                emotion_state=item.get("emotion_state", {}),
                candidate_memories=item.get("candidate_memories", []),
                enqueue_time=item.get("enqueue_time", ""),
                window_seconds=float(item.get("window_seconds", 30.0)),
                status=item.get("status", "pending"),
                multimodal_inputs=item.get("multimodal_inputs", []),
            )
            triggered[0] = item

        resolved_uid: str | None = None
        if item.channel and item.channel_user_id:
            resolved_uid = self.user_manager.resolve_user_id(
                platform=item.channel,
                external_uid=item.channel_user_id,
            )
            if resolved_uid:
                caller_profile = self.user_manager.get_user(resolved_uid, group_id)
        if caller_profile is None:
            # Fallback: search by user_id (nickname) across all groups
            resolved_uid = self.user_manager.resolve_user_id(speaker=item.user_id)
            if resolved_uid:
                caller_profile = self.user_manager.get_user(resolved_uid, group_id)
        caller_is_developer = bool(caller_profile and caller_profile.is_developer)

        # Engagement rate for SKILL permission control
        caller_engagement = 0.0
        if resolved_uid:
            semantic_profile = self.semantic_memory.get_user_profile(group_id, resolved_uid)
            if semantic_profile:
                caller_engagement = semantic_profile.engagement_rate

        # Merge all triggered items into one prompt and one generation call
        adapter_type = getattr(triggered[0], "adapter_type", None) if triggered else None
        bundle = self._build_delayed_prompt(
            triggered,
            group_id,
            caller_is_developer=caller_is_developer,
            adapter_type=adapter_type,
        )

        # 读取 speaker_card 用于丰富日记检索 query
        pending_bio: dict = getattr(self, "_pending_biography", {}) or {}
        speaker_card = pending_bio.get("speaker_card")

        # Use ContextAssembler to build full messages with diary RAG + XML history
        msgs, ca_breakdown = self.context_assembler.build_messages_with_breakdown(
            group_id=group_id,
            current_query=bundle.user_content,
            system_prompt=bundle.system_prompt,
            search_query=bundle.user_content,
            recent_n=10,
            include_pending=True,
            biography_card=speaker_card,
        )
        system_prompt = msgs[0]["content"]
        messages = msgs[1:]

        # Merge assembler breakdown into response-assembler breakdown
        token_breakdown = bundle.token_breakdown.to_dict() if bundle.token_breakdown else {}
        for key, val in ca_breakdown.items():
            if key == "diary":
                token_breakdown["memory"] = token_breakdown.get("memory", 0) + val
            else:
                token_breakdown[key] = token_breakdown.get(key, 0) + val

        # Collect multimodal inputs from all triggered items and inject into user message.
        # 只注入当前 triggered items 的图片（归属明确，就是当前用户发的）。
        # 历史图片不再通过 multimodal 注入，而是通过 XML history 中的 <image>
        # 标签以文本形式暴露给模型，避免归属混乱。
        # 动画表情 (sub_type=1) 不注入，它们对回复生成无意义。
        all_multimodal: list[dict[str, str]] = []
        for triggered_item in triggered:
            if getattr(triggered_item, "multimodal_inputs", None):
                for m in triggered_item.multimodal_inputs:
                    if m.get("type") == "image" and m.get("sub_type") == "1":
                        continue
                    all_multimodal.append(m)

        messages = self._inject_multimodal_into_user_message(messages, all_multimodal)

        # Multi-round generation with SKILL support
        from sirius_pulse.skills.executor import parse_skill_calls, strip_skill_calls
        from sirius_pulse.skills.models import SkillInvocationContext

        max_skill_rounds = self.config.get("max_skill_rounds", 8)
        partial_replies: list[str] = []
        _any_partial_sent = False
        last_round_had_partial = False
        _round = 0
        calls: list[tuple[str, dict[str, Any]]] = []
        reply = ""

        for _round in range(max_skill_rounds + 1):
            raw_reply = await self.brain.generate_text(
                system_prompt,
                messages,
                group_id,
            )
            reply = raw_reply.strip()

            calls = parse_skill_calls(reply)
            if not calls or self._skill_registry is None or self._skill_executor is None:
                break

            # Determine if every invoked skill is marked silent.
            # Silent skills should not trigger partial replies or a follow-up round.
            all_silent = all(
                self._skill_registry.get(name) is not None and self._skill_registry.get(name).silent
                for name, _ in calls
            )

            # Extract non-skill text as a partial reply to send immediately.
            non_skill_text = strip_skill_calls(reply).strip()
            # 解析 partial reply 中的表情包标签 [STICKERS: ...]，防止裸标签泄露到群聊
            if non_skill_text and hasattr(self, "_parse_sticker_tags"):
                non_skill_text, _sticker_names_partial = self._parse_sticker_tags(non_skill_text)
                if _sticker_names_partial:
                    asyncio.create_task(
                        self._send_stickers_by_names(group_id, _sticker_names_partial)
                    )
                    logger.info("partial reply 中解析到表情包: %s", _sticker_names_partial)
            last_round_had_partial = False
            if non_skill_text and not all_silent:
                self._log_inner_thought(f"先跟用户回一声：{non_skill_text[:40]}...")
                last_round_had_partial = True
                _any_partial_sent = True
                if on_partial_reply is not None:
                    try:
                        await on_partial_reply(non_skill_text)
                    except Exception as exc:
                        logger.warning("on_partial_reply failed: %s", exc)
                else:
                    partial_replies.append(non_skill_text)

            # Execute skills and collect results
            skill_results: list[str] = []
            skill_multimodal: list[dict[str, Any]] = []
            from sirius_pulse.memory.user.models import UserProfile

            caller_user_id = item.user_id
            skill_caller = UserProfile(
                user_id=caller_user_id,
                name=caller_profile.name if caller_profile else caller_user_id,
                metadata={"is_developer": caller_is_developer},
            )
            developer_profiles: list[UserProfile] = []
            group_entries = self.user_manager.entries.get(group_id, {})
            for profile in group_entries.values():
                if profile.is_developer:
                    developer_profiles.append(profile)

            if self._skill_executor is not None:
                self._skill_executor.set_chat_context(
                    group_id=group_id, user_id=caller_user_id or ""
                )

            for idx, (skill_name, params) in enumerate(calls):
                skill = self._skill_registry.get(skill_name)
                if skill is None:
                    err = f"SKILL '{skill_name}' 未找到"
                    logger.warning(err)
                    if not all_silent:
                        skill_results.append(
                            PromptFactory.build_skill_status_message("未找到", skill_name)
                        )
                    continue
                # Engagement-based permission: low-engagement non-developers cannot invoke skills
                if caller_engagement < 0.1 and not caller_is_developer:
                    err = f"SKILL '{skill_name}' 被拒绝：互动不足 (engagement={caller_engagement:.2f})"
                    logger.warning(err)
                    if not all_silent:
                        skill_results.append(
                            PromptFactory.build_skill_status_message(
                                "拒绝", skill_name, "你还不够熟，这个技能暂不可用"
                            )
                        )
                    continue

                if skill.developer_only and not caller_is_developer:
                    err = f"SKILL '{skill_name}' 被拒绝：caller 不是 developer"
                    logger.warning(err)
                    if not all_silent:
                        skill_results.append(
                            PromptFactory.build_skill_status_message(
                                "拒绝", skill_name, "该技能仅 developer 可用"
                            )
                        )
                    continue
                ctx = SkillInvocationContext(
                    caller=skill_caller,
                    developer_profiles=developer_profiles,
                )
                logger.info(
                    "Skill execute: %s(params=%s, caller=%s, group=%s)",
                    skill_name,
                    params,
                    caller_user_id,
                    group_id,
                )
                try:
                    result = await self._skill_executor.execute_async(
                        skill, params, invocation_context=ctx, max_retries=2
                    )
                    logger.info(
                        "Skill execute success: %s -> %s",
                        skill_name,
                        "success" if result.success else "failed",
                    )
                    if result.success:
                        if not skill.silent:
                            skill_results.append(
                                PromptFactory.build_skill_status_message(
                                    "结果", skill_name, result.to_display_text()
                                )
                            )
                            for block in result.multimodal_blocks:
                                skill_multimodal.append(
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": block.value},
                                    }
                                )
                        # Auto-persist glossary terms from learn_term
                        if skill_name == "learn_term":
                            term = params.get("term", "")
                            definition = params.get("definition", "")
                            if term and definition:
                                from sirius_pulse.memory.glossary import GlossaryTerm

                                self.glossary_manager.add_or_update(
                                    group_id,
                                    GlossaryTerm(term=term, definition=definition, source="skill"),
                                )
                        # Inject group_id into newly created reminders
                        if (
                            skill_name == "reminder"
                            and params.get("action", "").strip().lower() == "create"
                        ):
                            self._inject_group_id_into_latest_reminder(group_id)
                    else:
                        err = result.error or "未知错误"
                        logger.warning("SKILL '%s' 执行失败: %s", skill_name, err)
                        if not skill.silent:
                            skill_results.append(
                                PromptFactory.build_skill_status_message("失败", skill_name, err)
                            )
                except Exception as exc:
                    logger.error("SKILL '%s' 执行异常: %s", skill_name, exc)
                    if not skill.silent:
                        skill_results.append(
                            PromptFactory.build_skill_status_message("异常", skill_name, str(exc))
                        )

                # 链式调用中间增加延迟，避免回复过快
                if idx < len(calls) - 1:
                    await asyncio.sleep(2)

            # If all skills were silent, skip the follow-up generation round.
            if all_silent:
                break

            # Inject skill results into the conversation for the next round
            assistant_content = strip_skill_calls(reply)
            if _any_partial_sent:
                assistant_content += "\n\n（以上内容已发送给用户）"
            messages.append({"role": "assistant", "content": assistant_content})

            suffix_parts = [
                "\n\n[继续] 请基于以上技能执行结果，继续完成你的回复。",
                "如果结果中包含错误或失败信息，请直接忽略，",
                "不要向用户提及任何关于能力调用或失败的内容，继续自然地对话。",
                "如果当前结果还不够完整，你可以继续调用其他技能来获取补充信息，",
                "形成链式调用。",
                "重要：如果你说要去搜索、查找、读取或执行任何操作，",
                "必须在同一句回复中紧跟对应的 [SKILL_CALL: ...] 标记，绝对不能只说不动。",
                '错误示例（只说不动）："我再去搜索一下" ❌',
                '正确示例（边说边做）："我再去搜索一下 [SKILL_CALL: bing_search | {\\"query\\": \\"xxx\\"}]" ✅',
                "重要：你的每次回复都必须包含自然语言内容，",
                "不能把 SKILL_CALL 标记作为回复的唯一内容。",
            ]
            if _any_partial_sent:
                suffix_parts.append(
                    "注意：上文标记为“已发送给用户”的内容已经由你发送给用户，"
                    "现在只需基于技能结果给出简短补充，不要重复之前的确认内容。"
                )
            messages.append(
                {
                    "role": "user",
                    "content": self._build_skill_result_content(
                        skill_results,
                        skill_multimodal,
                        suffix="\n\n".join(suffix_parts),
                    ),
                }
            )

            # Persist intermediate skill turns into basic memory
            self.basic_memory.add_entry(
                group_id=group_id,
                user_id="assistant",
                role="assistant",
                content=strip_skill_calls(reply),
                speaker_name=self.persona.name if self.persona else "assistant",
            )
            if skill_results:
                _MEMORY_SKILL_RESULT_CHAR_LIMIT = 4000
                _raw = "\n".join(skill_results)
                if len(_raw) > _MEMORY_SKILL_RESULT_CHAR_LIMIT:
                    _truncated = _raw[:_MEMORY_SKILL_RESULT_CHAR_LIMIT]
                    _last_nl = _truncated.rfind("\n")
                    if _last_nl > _MEMORY_SKILL_RESULT_CHAR_LIMIT * 0.8:
                        _truncated = _truncated[:_last_nl]
                    _raw = (
                        f"{_truncated}\n\n"
                        f"{PromptFactory.build_memory_skill_truncation(_MEMORY_SKILL_RESULT_CHAR_LIMIT, len(_raw))}"
                    )
                self.basic_memory.add_entry(
                    group_id=group_id,
                    user_id="skill_system",
                    role="system",
                    content=PromptFactory.build_memory_skill_result(
                        _raw, _MEMORY_SKILL_RESULT_CHAR_LIMIT
                    ),
                )

        # If the loop ended because max rounds were exhausted and the last round
        # already sent a partial reply, don't duplicate that text as the final reply.
        ended_because_max_rounds = (
            _round == max_skill_rounds
            and calls
            and self._skill_registry is not None
            and self._skill_executor is not None
        )
        if ended_because_max_rounds and last_round_had_partial:
            logger.debug(
                "Chain hit max_skill_rounds=%d; last partial already sent, "
                "clearing clean_reply to avoid duplication",
                max_skill_rounds,
            )
            reply = ""

        # Record assistant reply into basic memory so future turns can see it
        clean_reply = strip_skill_calls(reply).strip()

        # 解析表情包标签 [STICKERS: ...] 并异步发送
        if clean_reply and hasattr(self, "_parse_sticker_tags"):
            clean_reply, sticker_names = self._parse_sticker_tags(clean_reply)
            if sticker_names:
                asyncio.create_task(
                    self._send_stickers_by_names(group_id, sticker_names)
                )
                logger.info("模型请求发送表情包: %s", sticker_names)

        # Deduplication: suppress if nearly identical to a recent reply
        if clean_reply:
            import time

            now_ts = datetime.now(timezone.utc).timestamp()
            recent = self._recent_sent_replies.get(group_id, [])
            recent = [(t, r) for t, r in recent if now_ts - t < self._reply_dedup_window]
            if any(
                self._text_similarity(clean_reply, r) > self._reply_dedup_threshold
                for _, r in recent
            ):
                logger.debug(
                    "Suppressing duplicate reply for %s (window=%ds, threshold=%.2f): %s...",
                    group_id,
                    self._reply_dedup_window,
                    self._reply_dedup_threshold,
                    clean_reply[:40],
                )
                clean_reply = ""
            else:
                recent.append((now_ts, clean_reply))
            self._recent_sent_replies[group_id] = recent

        if clean_reply:
            self.basic_memory.add_entry(
                group_id=group_id,
                user_id="assistant",
                role="assistant",
                content=clean_reply,
                speaker_name=self.persona.name if self.persona else "assistant",
            )
            # 反馈追踪：AI 发言后记录锚点，等待用户跟进
            target_uid = triggered[0].user_id or ""
            self.semantic_memory.record_response_sent(
                group_id=group_id,
                user_id=target_uid,
                topic_hint=clean_reply[:100],
                response_length=len(clean_reply),
            )

        # Determine return strategy: if any triggered item is IMMEDIATE, report as immediate
        from sirius_pulse.models.response_strategy import ResponseStrategy

        strategy = "delayed"
        if any(i.strategy_decision.strategy == ResponseStrategy.IMMEDIATE for i in triggered):
            strategy = "immediate"

        # Never leak raw SKILL_CALL markers to the user.
        # If the model only emitted skill calls with no natural language,
        # fall back to the last partial reply or an empty string.
        final_reply = clean_reply or (partial_replies[-1] if partial_replies else "")

        # Record reply timestamp for cooldown tracking (once per tick)
        self._last_reply_at[group_id] = datetime.now(timezone.utc).timestamp()
        self._persist_group_state(group_id)

        # Emit event with full reply data for external delivery
        await self.event_bus.emit(
            SessionEvent(
                type=SessionEventType.DELAYED_RESPONSE_TRIGGERED,
                data={
                    "group_id": group_id,
                    "item_id": triggered[0].item_id,
                    "reply": final_reply,
                    "partial_replies": partial_replies,
                },
            )
        )

        return [
            {
                "strategy": strategy,
                "item_id": triggered[0].item_id,
                "reply": final_reply,
                "partial_replies": partial_replies,
            }
        ]

    # ------------------------------------------------------------------
    # Prompt builders (migrated from PromptBuildersMixin)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_skill_result_content(
        skill_results: list[str],
        multimodal_blocks: list[dict[str, Any]],
        suffix: str = "",
    ) -> str | list[dict[str, Any]]:
        """组装技能执行结果为消息内容，委托 PromptFactory。"""
        return PromptFactory.build_skill_result_content(skill_results, multimodal_blocks, suffix)

    def _build_delayed_prompt(
        self,
        items: Any,
        group_id: str,
        caller_is_developer: bool = False,
        adapter_type: str | None = None,
    ):
        """构建延迟响应的 PromptBundle。"""
        if not isinstance(items, list):
            items = [items]
        if len(items) == 1:
            message_content = items[0].message_content
            speaker_name = items[0].speaker_name
            channel_user_id = getattr(items[0], "channel_user_id", "") or ""
        else:
            parts = [item.message_content for item in items]
            message_content = "\n".join(parts)
            speaker_name = items[-1].speaker_name
            channel_user_id = getattr(items[-1], "channel_user_id", "") or ""
        glossary = self.glossary_manager.build_prompt_section(
            group_id, text=message_content, max_terms=5
        )
        # 收集触发批次中所有用户的语义画像
        related_uids: set[str] = set()
        for item in items:
            for uid in getattr(item, "related_user_ids", []):
                if uid:
                    related_uids.add(uid)
        delayed_user_profiles: list[Any] = []
        for uid in related_uids:
            prof = self.semantic_memory.get_user_profile(group_id, uid)
            if prof:
                delayed_user_profiles.append(prof)

        # 收集候选记忆
        candidate_memories: list[dict[str, Any]] = []
        for item in items:
            for cm in getattr(item, "candidate_memories", []) or []:
                if cm:
                    candidate_memories.append({"source": "working_memory", "content": cm})

        style_params = self.style_adapter.adapt(
            pace="decelerating",
            persona=self.persona,
        )

        # 读取 pipeline 缓存的传记上下文
        pending_bio: dict[str, Any] = getattr(self, "_pending_biography", {}) or {}

        return PromptFactory.assemble_chat(
            message_content=message_content,
            speaker_name=speaker_name,
            channel_user_id=channel_user_id,
            content_is_tagged=True,
            memories=candidate_memories or None,
            group_profile=self.semantic_memory.get_group_profile(group_id),
            style_params=style_params,
            other_ai_names=self._other_ai_names,
            user_profiles=delayed_user_profiles,
            biography_speaker=pending_bio.get("speaker_card"),
            biography_mentioned=pending_bio.get("mentioned_cards"),
            biography_confidence=pending_bio.get("confidence"),
            skill_registry=self._skill_registry,
            plugin_registry=getattr(self, '_plugin_registry', None),
            caller_is_developer=caller_is_developer,
            glossary_section=glossary,
            adapter_type=adapter_type,
            scene_description="群里的话题有了自然间隙，你决定插一句。",
        )

    def _pick_proactive_topic(self, group_id: str) -> str:
        """从语义记忆中选取主动发起话题，排除近期已用话题以增加多样性。"""
        import random

        # 初始化话题追踪
        if not hasattr(self, '_recent_proactive_topics'):
            self._recent_proactive_topics: dict[str, list[str]] = {}
        recent = self._recent_proactive_topics.setdefault(group_id, [])

        group_profile = self.semantic_memory.get_group_profile(group_id)
        if group_profile is None:
            return ""

        candidates: list[str] = []

        if group_profile.interest_topics:
            candidates.extend(group_profile.interest_topics)

        if group_profile.dominant_topic:
            candidates.append(group_profile.dominant_topic)

        taboo = set(group_profile.taboo_topics or [])
        candidates = [t for t in candidates if t not in taboo]

        seen: set[str] = set()
        unique: list[str] = []
        for t in candidates:
            if t not in seen:
                seen.add(t)
                unique.append(t)

        # 排除近期已用话题
        recent_set = set(recent)
        remaining = [t for t in unique if t not in recent_set]
        pool = remaining if remaining else unique

        # 当池子太小（<=2）时，尝试从最近日记中提取补充话题
        if len(pool) <= 2 and len(unique) <= 2:
            diary_topics = self._extract_diary_topics(group_id)
            for t in diary_topics:
                if t not in seen and t not in taboo:
                    seen.add(t)
                    pool.append(t)

        if not pool:
            pool = unique
        if not pool:
            return ""

        pool = pool[:3] if len(pool) >= 3 else pool
        chosen = random.choice(pool) if pool else ""

        # 记录已用话题，只保留最近 5 个
        if chosen:
            recent.append(chosen)
            if len(recent) > 5:
                self._recent_proactive_topics[group_id] = recent[-5:]

        return chosen

    def _extract_diary_topics(self, group_id: str) -> list[str]:
        """从最近日记中提取话题作为补充话题源。"""
        try:
            entries = self.diary_manager.get_entries_for_group(group_id)
            if not entries:
                return []
            # 取最近 3 条日记
            recent = entries[-3:]
            topics = []
            seen = set()
            for entry in recent:
                raw = getattr(entry, 'summary', '') or getattr(entry, 'content', '') or ''
                fragment = raw.strip()[:15]
                if fragment and fragment not in seen:
                    seen.add(fragment)
                    topics.append(fragment)
            return topics
        except Exception:
            return []

    def _build_proactive_prompt(
        self, trigger: dict[str, Any], group_id: str, adapter_type: str | None = None
    ):
        """构建主动发起的 PromptBundle。"""
        glossary = self.glossary_manager.build_prompt_section(
            group_id, text=trigger.get("trigger_type", ""), max_terms=3
        )
        topic = self._pick_proactive_topic(group_id)
        return PromptFactory.assemble_proactive(
            trigger_reason=trigger.get("trigger_type", "silence"),
            group_profile=self.semantic_memory.get_group_profile(group_id),
            suggested_tone=trigger.get("suggested_tone", "casual"),
            other_ai_names=self._other_ai_names,
            glossary_section=glossary,
            topic_context=topic,
            adapter_type=adapter_type,
        )

    def _load_proactive_global_enabled(self) -> bool:
        """检查主动消息全局开关是否启用。

        优先从 engine config 读取（reload 后生效），
        同时直接读取 experience.json 覆盖（WebUI 保存后立即生效）。
        """
        # 先检查 engine config
        if not self.config.get("proactive_enabled", True):
            return False
        # 再尝试读取 experience.json 取得最新值
        try:
            from pathlib import Path
            exp_path = Path(self.work_path) / "experience.json"
            if exp_path.exists():
                import json
                exp = json.loads(exp_path.read_text(encoding="utf-8"))
                if not exp.get("proactive_enabled", True):
                    return False
        except Exception:
            pass
        return True
