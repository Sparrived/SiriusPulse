"""后台任务管理组件。

重构为组合模式，将延迟队列任务拆分到独立文件。
核心类负责任务调度和生命周期管理。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from sirius_pulse.memory.cold_detector import ColdState

if TYPE_CHECKING:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

logger = logging.getLogger(__name__)


class BackgroundTasks:
    """后台任务管理组件。

    通过引擎实例访问属性，实现组合模式。
    子任务委托给 DelayedQueueTasks。
    """

    def __init__(self, engine: _EmotionalGroupChatEngineBase) -> None:
        self._engine = engine
        # 延迟初始化子任务组件
        self._delayed: Any = None

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
        """Start periodic background tasks for delayed queue and memory promotion.
        Idempotent: safe to call multiple times.
        """
        engine = self._engine
        if engine._bg_running:
            return
        engine._bg_running = True

        tasks = [
            asyncio.create_task(self.delayed.delayed_queue_ticker(), name="delayed_queue"),
            asyncio.create_task(self._diary_promoter(), name="diary_promote"),
            asyncio.create_task(self._diary_consolidator(), name="diary_consolidator"),
            asyncio.create_task(self._background_refiner(), name="background_refiner"),
            asyncio.create_task(self._sticker_cache_warmup(), name="sticker_cache_warmup"),
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

    async def _sticker_cache_warmup(self) -> None:
        """启动后异步预热表情包二元对立缓存。"""
        engine = self._engine
        try:
            sticker = getattr(engine, "_sticker", None)
            if sticker is None:
                return
            await sticker.warmup_opposition_cache()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("表情包缓存预热失败: %s", exc)

    # ==================================================================
    # 日记相关任务
    # ==================================================================

    async def _diary_promoter(self) -> None:
        """Periodically promote basic memory entries to diary summaries.

        Trigger conditions:
        1. Group is cold (heat < threshold AND silence >= threshold).
        2. The group has enough undiarized archive candidates.
        """
        engine = self._engine
        interval = engine.config.get("memory_promote_interval_seconds", 180)
        volume_threshold = engine.config.get("diary_volume_threshold", 8)
        idle_consolidation_seconds = engine.config.get("memory_idle_consolidation_seconds", 3600)
        while engine._bg_running:
            await asyncio.sleep(interval)
            try:
                if engine.provider_async is None:
                    continue

                promoted_total = 0
                for group_id in list(engine.basic_memory.list_groups()):
                    heat, seconds_since_last = engine.basic_memory.get_cold_params(group_id)
                    cold_state = engine.cold_detector.check(heat, seconds_since_last)

                    # ── Layer 3: 冷寂 → 日记生成 ──
                    if cold_state == ColdState.COLD:
                        candidates = engine.basic_memory.get_consolidation_candidates(
                            group_id,
                            include_context=seconds_since_last >= idle_consolidation_seconds,
                        )
                        if not candidates:
                            continue
                        candidates = [
                            c
                            for c in candidates
                            if not engine.diary_manager.is_source_diarized(group_id, c.entry_id)
                        ]
                        if not candidates:
                            continue

                        cfg = engine.model_router.resolve("memory_extract")
                        # Use topic clustering when there are many candidates
                        # to avoid information loss from a single diary call.
                        if len(candidates) > volume_threshold * 2:
                            cluster_cfg = engine.model_router.resolve("topic_cluster")
                            results = await engine.diary_manager.generate_topic_clustered(
                                group_id=group_id,
                                candidates=candidates,
                                persona_name=engine.persona.name,
                                persona_description=(
                                    engine.persona.persona_summary or engine.persona.backstory or ""
                                ),
                                brain=engine.brain,
                                model_name=cfg.model_name,
                                min_candidate_count=volume_threshold,
                                topic_cluster_model=cluster_cfg.model_name,
                            )
                            promoted_total += len(results)
                        else:
                            result = await engine.diary_manager.generate_from_candidates(
                                group_id=group_id,
                                candidates=candidates,
                                persona_name=engine.persona.name,
                                persona_description=(
                                    engine.persona.persona_summary or engine.persona.backstory or ""
                                ),
                                brain=engine.brain,
                                model_name=cfg.model_name,
                                min_candidate_count=volume_threshold,
                            )
                            if result:
                                promoted_total += 1

                if promoted_total > 0:
                    engine._log_inner_thought(f"整理了 {promoted_total} 个群的对话日记，过去的回忆又清晰了一点～")
            except Exception as exc:
                logger.warning("Diary promotion failed: %s", exc)

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
        from sirius_pulse.core.brain import RawRequest
        from sirius_pulse.memory.diary.consolidator import DiaryConsolidator

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
                    raw = await engine.brain.raw_call(raw_request)
                    entry = consolidator.parse_merge_result(raw, cluster)
                    if entry:
                        merged_entries.append(entry)

                if merged_entries:
                    await asyncio.to_thread(
                        consolidator.append_merged_entries, group_id, clusters, merged_entries
                    )
                    engine._log_inner_thought(
                        f"整理了 {len(clusters)} 组相似日记，合并成 {len(merged_entries)} 条喵~"
                    )
            except Exception as exc:
                logger.warning("Diary consolidation failed for %s: %s", group_id, exc)

    # ==================================================================
    # Layer 4: 后台精炼（Schema 归纳 + 知识缺口检测）
    # ==================================================================

    async def _background_refiner(self) -> None:
        """Layer 4: 后台精炼任务。

        运行频率较低（默认每小时），对已有记忆进行深度加工：
        1. Schema 归纳：从演化链归纳行为模式
        2. 知识缺口检测：检测传记中的缺失信息
        """
        engine = self._engine
        interval = engine.config.get("refine_interval_seconds", 3600)
        while engine._bg_running:
            await asyncio.sleep(interval)
            try:
                await self._run_refinement()
            except Exception as exc:
                logger.warning("Background refinement failed: %s", exc)

    async def _run_refinement(self) -> None:
        """执行后台精炼。"""
        engine = self._engine
        cfg = engine.model_router.resolve("memory_extract")

        # 获取所有有记录的用户
        all_subjects = engine.evolution_chain._store.get_all_subjects()

        for subject in all_subjects[:20]:  # 限制每次处理的用户数
            try:
                # 1. Schema 归纳（如果该用户有足够的 active 记录）
                active_records = engine.evolution_chain.get_active_by_subject(subject)
                if len(active_records) >= 5:
                    from sirius_pulse.memory.schema import SchemaInductor, SchemaStore

                    schema_store = SchemaStore(conn=engine._persona_db_conn)
                    inductor = SchemaInductor(store=schema_store)
                    schemas = await inductor.induct(
                        subject,
                        engine.evolution_chain,
                        engine.brain,
                        cfg.model_name,
                    )
                    if schemas:
                        logger.info(
                            "用户 %s 归纳了 %d 个行为模式",
                            subject,
                            len(schemas),
                        )

                # 2. 知识缺口检测
                bio = engine.biography_view.get_biography(subject)
                if bio:
                    from sirius_pulse.memory.gap_detector import GapDetector

                    gaps = GapDetector.detect(bio)
                    if gaps:
                        hint = GapDetector.build_prompt_hint(gaps)
                        logger.debug(
                            "用户 %s 存在 %d 个知识缺口: %s",
                            subject,
                            len(gaps),
                            hint[:50],
                        )

            except Exception as exc:
                logger.warning("Refinement failed for %s: %s", subject, exc)

    # ==================================================================
    # 委托方法（向后兼容）
    # ==================================================================

    async def tick_delayed_queue(
        self,
        group_id: str,
        on_partial_reply: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Process delayed response queue for a group."""
        return await self.delayed.tick_delayed_queue(group_id, on_partial_reply)

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
