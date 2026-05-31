"""统一记忆调度器。

纯程序调度，LLM 只做内容生成，不做流程控制。
负责所有记忆相关的调度决策：
- Layer 0: 每条消息 → BasicMemory + 磁盘归档
- Layer 1: bot 回复时 → ContextAssembler
- Layer 2: 暂冷 → SituationExtractor
- Layer 3: 冷寂 → DiaryGenerator
- Layer 4: 后台精炼 → 演化链验证 + Schema 归纳
"""

from __future__ import annotations

import logging
from typing import Any

from sirius_pulse.memory.basic.manager import BasicMemoryManager
from sirius_pulse.memory.biography.view import BiographyView
from sirius_pulse.memory.cold_detector import ColdDetector, ColdState
from sirius_pulse.memory.context_assembler import ContextAssembler
from sirius_pulse.memory.diary.generator import DiaryGenerator
from sirius_pulse.memory.diary.manager import DiaryManager
from sirius_pulse.memory.evolution.chain import EvolutionChain
from sirius_pulse.memory.situation.extractor import SituationExtractor
from sirius_pulse.memory.situation.store import SituationStore

logger = logging.getLogger(__name__)

__all__ = ["MemoryScheduler"]


class ProcessingCursor:
    """处理游标：跟踪每个群组的处理状态。"""

    def __init__(self, group_id: str) -> None:
        self.group_id = group_id
        self.last_situation_extract_at: str = ""
        self.last_diary_generate_at: str = ""
        self.last_schema_induct_at: str = ""

    def __repr__(self) -> str:
        return f"ProcessingCursor(group={self.group_id})"


class MemoryScheduler:
    """统一记忆调度器。

    纯程序驱动，不依赖 LLM 做流程控制。
    所有记忆相关的调度决策都通过此类统一管理。
    """

    def __init__(
        self,
        basic_memory: BasicMemoryManager,
        evolution_chain: EvolutionChain,
        situation_store: SituationStore,
        situation_extractor: SituationExtractor,
        diary_manager: DiaryManager,
        biography_view: BiographyView,
        context_assembler: ContextAssembler,
        cold_detector: ColdDetector,
    ) -> None:
        self._basic = basic_memory
        self._chain = evolution_chain
        self._situation_store = situation_store
        self._extractor = situation_extractor
        self._diary = diary_manager
        self._bio_view = biography_view
        self._assembler = context_assembler
        self._cold = cold_detector

        self._cursors: dict[str, ProcessingCursor] = {}

    def get_cursor(self, group_id: str) -> ProcessingCursor:
        """获取或创建群组处理游标。"""
        if group_id not in self._cursors:
            self._cursors[group_id] = ProcessingCursor(group_id)
        return self._cursors[group_id]

    # ── Layer 2: 暂冷触发 ──

    async def on_warm(
        self,
        group_id: str,
        entries: list[Any],
        brain: Any,
        model_name: str,
    ) -> bool:
        """暂冷时触发情景提取。

        Returns:
            是否成功提取
        """
        cursor = self.get_cursor(group_id)

        situation = await self._extractor.extract(
            group_id=group_id,
            entries=entries,
            brain=brain,
            model_name=model_name,
            evolution_chain=self._chain,
        )

        if situation:
            self._situation_store.save(situation)
            logger.info(
                "群 %s 情景提取完成: %d 个三元组",
                group_id, situation.validated_triple_count,
            )
            return True

        return False

    # ── Layer 3: 冷寂触发 ──

    async def on_cold(
        self,
        group_id: str,
        persona_name: str,
        persona_description: str,
        brain: Any,
        model_name: str,
    ) -> bool:
        """冷寂时触发日记生成。

        Returns:
            是否成功生成
        """
        cursor = self.get_cursor(group_id)

        # 获取当日 Situation
        situations = self._situation_store.get_today(group_id)

        if situations:
            # 从 Situation 生成日记
            parsed = await self._diary._generator.generate_from_situations(
                group_id=group_id,
                situations=situations,
                persona_name=persona_name,
                persona_description=persona_description,
                brain=brain,
                model_name=model_name,
            )
            if parsed and parsed.get("content"):
                logger.info(
                    "群 %s 从 %d 个 Situation 生成日记完成",
                    group_id, len(situations),
                )
                return True
        else:
            # fallback: 使用旧的候选消息方式
            candidates = self._basic.get_archive_candidates(group_id)
            if candidates:
                result = await self._diary.generate_from_candidates(
                    group_id=group_id,
                    candidates=candidates,
                    persona_name=persona_name,
                    persona_description=persona_description,
                    brain=brain,
                    model_name=model_name,
                )
                if result:
                    return True

        return False

    # ── Layer 4: 后台精炼 ──

    async def on_refine(
        self,
        group_id: str,
        brain: Any,
        model_name: str,
    ) -> None:
        """后台精炼：演化链 uncertain 记录重新验证。"""
        uncertain = self._chain.get_uncertain_records(limit=10)
        if not uncertain:
            return

        # TODO: 用 LLM 重新评估 uncertain 记录
        # 当前简化：直接跳过
        logger.debug("群 %s 有 %d 条待验证记录", group_id, len(uncertain))

    # ── 查询 ──

    def get_today_situations(self, group_id: str) -> list[Any]:
        """获取当日 Situation。"""
        return self._situation_store.get_today(group_id)

    def get_biography(self, user_id: str) -> Any:
        """获取用户传记。"""
        return self._bio_view.get_biography(user_id)
