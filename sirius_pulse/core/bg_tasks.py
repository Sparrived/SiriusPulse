"""后台任务管理组件。

重构为组合模式，将延迟队列任务拆分到独立文件。
核心类负责任务调度和生命周期管理。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sirius_pulse.memory.cold_detector import ColdState
from sirius_pulse.utils.json_io import atomic_write_json

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
            asyncio.create_task(self._memory_unit_checkpointer(), name="memory_checkpoint"),
            asyncio.create_task(self._sticker_cache_warmup(), name="sticker_cache_warmup"),
            asyncio.create_task(self._memory_dedupe_job_worker(), name="memory_dedupe"),
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
    # Memory checkpoint tasks
    # ==================================================================

    async def _memory_unit_checkpointer(self) -> None:
        """Periodically checkpoint basic memory entries into memory units.

        Trigger conditions:
        1. Group is cold (heat < threshold AND silence >= threshold).
        2. The group has enough uncheckpointed archive candidates.
        """
        engine = self._engine
        interval = engine.config.get("memory_promote_interval_seconds", 180)
        volume_threshold = engine.config.get(
            "memory_unit_volume_threshold",
            engine.config.get("diary_volume_threshold", 8),
        )
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

                    # Layer 3: cold group -> memory checkpoint
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
                            if not engine.memory_unit_manager.is_source_checkpointed(
                                group_id, c.entry_id
                            )
                        ]
                        if not candidates:
                            continue

                        cfg = engine.model_router.resolve("memory_extract")
                        result = await engine.memory_unit_manager.generate_from_candidates(
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
                            promoted_total += len(result.units)
                            covered_source_ids: set[str] = set()
                            for unit in result.units:
                                covered_source_ids.update(unit.source_ids)
                            removed = engine.basic_memory.remove_entries_by_ids(
                                group_id, covered_source_ids
                            )
                            if removed:
                                logger.info(
                                    "Pruned %d checkpointed raw memory entries for group %s",
                                    removed,
                                    group_id,
                                )

                if promoted_total > 0:
                    engine._log_inner_thought(f"整理了 {promoted_total} 条结构化记忆单元。")
            except Exception as exc:
                logger.warning("Memory checkpoint failed: %s", exc)

    async def _diary_promoter(self) -> None:
        """Backward-compatible alias for the old diary promotion task."""
        await self._memory_unit_checkpointer()

    async def _memory_dedupe_job_worker(self) -> None:
        while self._engine._bg_running:
            try:
                await self._process_memory_dedupe_request_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Memory dedupe job worker failed: %s", exc)
            await asyncio.sleep(1)

    async def _process_memory_dedupe_request_once(self) -> None:
        engine = self._engine
        job_dir = Path(engine.work_path) / "engine_state" / "memory_dedupe"
        job_dir.mkdir(parents=True, exist_ok=True)
        request_path = job_dir / "request.json"
        if request_path.exists():
            claimed = job_dir / "request.processing.json"
            try:
                request_path.replace(claimed)
            except FileNotFoundError:
                claimed = None
            if claimed is not None:
                try:
                    request = json.loads(claimed.read_text(encoding="utf-8"))
                    job_id = str(request.get("job_id") or "")
                    action = str(request.get("action") or "")
                    status_path = job_dir / "status.json"
                    report_path = Path(engine.work_path) / "logs" / "memory-dedupe" / f"{job_id}.json"
                    cfg = engine.model_router.resolve("memory_extract")
                    if action == "scan":
                        atomic_write_json(status_path, {"job_id": job_id, "status": "scanning", "progress": 0})

                        def update_progress(done: int, total: int) -> None:
                            atomic_write_json(
                                status_path,
                                {"job_id": job_id, "status": "scanning", "progress": int(done * 100 / total) if total else 100},
                            )

                        report = await engine.memory_unit_manager.scan_duplicates(
                            brain=engine.brain, model_name=cfg.model_name, progress=update_progress
                        )
                        report_path.parent.mkdir(parents=True, exist_ok=True)
                        atomic_write_json(report_path, report)
                        atomic_write_json(status_path, {"job_id": job_id, "status": "ready", "progress": 100, "report_path": str(report_path)})
                    elif action == "apply":
                        atomic_write_json(status_path, {"job_id": job_id, "status": "applying", "progress": 0})
                        report = json.loads(report_path.read_text(encoding="utf-8"))
                        result = await engine.memory_unit_manager.apply_duplicate_report(report)
                        atomic_write_json(
                            status_path,
                            {"job_id": job_id, "status": result["status"], "progress": 100, "report_path": str(report_path), **{key: value for key, value in result.items() if key != "status"}},
                        )
                    else:
                        raise ValueError(f"unknown memory dedupe action: {action}")
                except Exception as exc:
                    atomic_write_json(job_dir / "status.json", {"job_id": locals().get("job_id", ""), "status": "failed", "error": str(exc)})
                finally:
                    claimed.unlink(missing_ok=True)

        reconcile_path = job_dir / "reconcile.json"
        if not reconcile_path.exists():
            return
        claimed_reconcile = job_dir / "reconcile.processing.json"
        try:
            reconcile_path.replace(claimed_reconcile)
        except FileNotFoundError:
            return
        try:
            payload = json.loads(claimed_reconcile.read_text(encoding="utf-8"))
            cfg = engine.model_router.resolve("memory_extract")
            await engine.memory_unit_manager.reconcile_persisted_units(
                [str(value) for value in payload.get("group_ids", [])],
                [str(value) for value in payload.get("unit_ids", [])],
                brain=engine.brain,
                model_name=cfg.model_name,
            )
        finally:
            claimed_reconcile.unlink(missing_ok=True)

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
