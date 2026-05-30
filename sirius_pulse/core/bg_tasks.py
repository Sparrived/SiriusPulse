"""后台任务管理组件。

重构为组合模式，将主动消息和延迟队列任务拆分到独立文件。
核心类负责任务调度和生命周期管理。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sirius_pulse.core.events import SessionEvent, SessionEventType

if TYPE_CHECKING:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

logger = logging.getLogger(__name__)


class BackgroundTasks:
    """后台任务管理组件。

    通过引擎实例访问属性，实现组合模式。
    子任务委托给 ProactiveTasks 和 DelayedQueueTasks。
    """

    def __init__(self, engine: _EmotionalGroupChatEngineBase) -> None:
        self._engine = engine
        # 延迟初始化子任务组件
        self._proactive: Any = None
        self._delayed: Any = None

    @property
    def proactive(self) -> Any:
        """获取主动消息任务组件（延迟初始化）。"""
        if self._proactive is None:
            from sirius_pulse.core.bg_tasks_proactive import ProactiveTasks
            self._proactive = ProactiveTasks(self._engine)
        return self._proactive

    @property
    def delayed(self) -> Any:
        """获取延迟队列任务组件（延迟初始化）。"""
        if self._delayed is None:
            from sirius_pulse.core.bg_tasks_delayed import DelayedQueueTasks
            self._delayed = DelayedQueueTasks(self._engine)
        return self._delayed

    # ==================================================================
    # 任务生命周期管理
    # ==================================================================

    def start(self) -> None:
        """Start periodic background tasks for delayed queue, proactive triggers,
        and memory promotion. Idempotent: safe to call multiple times.
        """
        engine = self._engine
        if engine._bg_running:
            return
        engine._bg_running = True

        tasks = [
            asyncio.create_task(self.delayed.delayed_queue_ticker(), name="delayed_queue"),
            asyncio.create_task(self.proactive.proactive_checker(), name="proactive_check"),
            asyncio.create_task(self._diary_promoter(), name="diary_promote"),
            asyncio.create_task(self._diary_consolidator(), name="diary_consolidator"),
            asyncio.create_task(self.proactive.proactive_developer_chat_checker(), name="dev_chat"),
        ]
        for t in tasks:
            engine._bg_tasks.add(t)
            t.add_done_callback(engine._bg_tasks.discard)

    def stop(self) -> None:
        """Cancel all background tasks and run passive SKILL unload hooks."""
        engine = self._engine
        engine._bg_running = False
        for t in list(engine._bg_tasks):
            t.cancel()
        engine._bg_tasks.clear()

        # 执行被动 SKILL on_unload 钩子（通过 ensure_future 调度，快速清理资源）
        for ctx, factory in getattr(engine, "_passive_skill_unloaders", []):
            try:
                coro = factory(ctx)
                if coro is not None and asyncio.iscoroutine(coro):
                    asyncio.ensure_future(coro)
            except Exception as exc:
                logger.warning("被动SKILL on_unload 失败: %s", exc)
        if hasattr(engine, "_passive_skill_unloaders"):
            engine._passive_skill_unloaders.clear()

    # ==================================================================
    # 日记相关任务
    # ==================================================================

    async def _diary_promoter(self) -> None:
        """Periodically promote basic memory entries to diary summaries.

        Trigger conditions (OR):
        1. Group is cold (heat < threshold AND silence >= threshold).
        2. Sufficient volume of undiarized archive candidates.
        """
        engine = self._engine
        interval = engine.config.get("memory_promote_interval_seconds", 180)
        volume_threshold = engine.config.get("diary_volume_threshold", 8)
        while engine._bg_running:
            await asyncio.sleep(interval)
            try:
                if engine.provider_async is None:
                    continue

                promoted_total = 0
                for group_id in list(engine.basic_memory.list_groups()):
                    candidates = engine.basic_memory.get_archive_candidates(group_id)
                    if not candidates:
                        continue

                    # Filter out already diarized candidates
                    candidates = [
                        c
                        for c in candidates
                        if not engine.diary_manager.is_source_diarized(group_id, c.entry_id)
                    ]
                    if not candidates:
                        continue

                    # Trigger: cold group OR sufficient undiarized volume
                    should_promote = (
                        engine.basic_memory.is_cold(group_id) or len(candidates) >= volume_threshold
                    )
                    if not should_promote:
                        continue

                    import time

                    cfg = engine.model_router.resolve("memory_extract")
                    t0 = time.perf_counter()
                    result = await engine.diary_manager.generate_from_candidates(
                        group_id=group_id,
                        candidates=candidates,
                        persona_name=engine.persona.name,
                        persona_description=(
                            engine.persona.persona_summary or engine.persona.backstory or ""
                        ),
                        brain=engine.brain,
                        model_name=cfg.model_name,
                    )
                    diary_duration_ms = round((time.perf_counter() - t0) * 1000, 2)
                    engine._helpers.record_subtask_tokens(
                        task_name="diary_generate",
                        model_name=cfg.model_name,
                        group_id=group_id,
                        duration_ms=diary_duration_ms,
                    )
                    if result:
                        promoted_total += 1
                        # Update semantic memory with LLM-extracted topics
                        profile = engine.semantic_memory.ensure_group_profile(group_id)
                        if result.dominant_topic:
                            profile.dominant_topic = result.dominant_topic
                        for topic in result.interest_topics:
                            if topic and topic not in profile.interest_topics:
                                profile.interest_topics.append(topic)
                        engine.semantic_memory.save_group_profile(group_id)

                        # 攒消息到人物传记（零 LLM，条件更新）
                        if getattr(engine, "biography_manager", None) is not None:
                            try:
                                await self._feed_biography_from_candidates(
                                    group_id, candidates, cfg.model_name
                                )
                            except Exception as exc:
                                logger.warning("传记攒消息失败: %s", exc)

                if promoted_total > 0:
                    engine._log_inner_thought(
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
        engine = self._engine
        mgr = getattr(engine, "biography_manager", None)
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
                    persona_name=engine.persona.name,
                    brain=engine.brain,
                    model_name=model_name,
                )
                if distilled:
                    engine._helpers.record_subtask_tokens(
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
                    persona_name=engine.persona.name,
                    brain=engine.brain,
                    model_name=model_name,
                )
                if updated:
                    engine._helpers.record_subtask_tokens(
                        task_name="biography_update",
                        model_name=model_name,
                        group_id=group_id,
                    )
            except Exception as exc:
                logger.warning("传记更新失败 user=%s: %s", user_id, exc)

    async def _diary_consolidator(self) -> None:
        """Periodically consolidate diary entries via LLM merging."""
        engine = self._engine
        interval = engine.config.get("consolidation_interval_seconds", 600)
        while engine._bg_running:
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

        engine = self._engine
        consolidator = DiaryConsolidator(engine.diary_manager, engine.config)
        cfg = engine.model_router.resolve("memory_extract")

        for group_id in list(engine._group_last_message_at.keys()):
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
                        response_format={"type": "json_object"},
                    )
                    t0 = time.perf_counter()
                    raw = await engine.brain.raw_call(raw_request)
                    consolidate_duration_ms = round((time.perf_counter() - t0) * 1000, 2)

                    from sirius_pulse.token.utils import PromptTokenBreakdown, estimate_tokens

                    sub_bd = PromptTokenBreakdown()
                    sub_bd.output_format = estimate_tokens(system_prompt)
                    sub_bd.user_message = estimate_tokens(user_content)
                    sub_bd.output_total = estimate_tokens(raw)
                    sub_bd.total = sub_bd.output_format + sub_bd.user_message + sub_bd.output_total

                    engine._helpers.record_subtask_tokens(
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
                    engine._log_inner_thought(
                        f"整理了 {len(clusters)} 组相似日记，合并成 {len(merged_entries)} 条喵~"
                    )
            except Exception as exc:
                logger.warning("Diary consolidation failed for %s: %s", group_id, exc)

    # ==================================================================
    # 委托方法（向后兼容）
    # ==================================================================

    async def proactive_check(
        self,
        group_id: str,
        *,
        _now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Check if proactive trigger should fire for a group."""
        return await self.proactive.proactive_check(group_id, _now=_now)

    async def tick_delayed_queue(
        self,
        group_id: str,
        on_partial_reply: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Process delayed response queue for a group."""
        return await self.delayed.tick_delayed_queue(group_id, on_partial_reply)

    def pop_developer_chats(self, group_id: str) -> list[str]:
        """Pop pending proactive developer chats for a group."""
        engine = self._engine
        return engine._pending_developer_chats.pop(group_id, [])

    def pop_reminders(self, group_id: str, adapter_type: str | None = None) -> list[str]:
        """Pop pending reminder messages for a group."""
        engine = self._engine
        items = engine._pending_reminders.pop(group_id, [])
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
            engine._pending_reminders[group_id] = unmatched
        return matched
