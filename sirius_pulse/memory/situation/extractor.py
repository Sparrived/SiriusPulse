"""情景提取器：从消息中提取三元组，生成 Situation。

暂冷时触发，使用轻量模型提取三元组，所有结果经过演化链验证。
不信任 LLM 输出：提取结果必须通过矛盾检测和置信度评估。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.memory.evolution.chain import EvolutionChain
from sirius_pulse.memory.evolution.models import (
    MetaTag,
    SituationSource,
    Triple,
)
from sirius_pulse.memory.situation.models import Situation

logger = logging.getLogger(__name__)

__all__ = ["SituationExtractor"]

# 最少消息数才触发提取
MIN_CANDIDATES = 5


_TRIPLE_EXTRACTION_PROMPT = (
    "你是一个性能优异的事实提取助手，能准确从群聊片段中提取结构化的事实和别称。\n"
    "\n"
    "人物列表（格式：显示名(ID)）：\n"
    "{entities}\n"
    "\n"
    "严格规则：\n"
    '- 每条事实用三元组表示：["ID", "关系", "属性/ID"]\n'
    "- 主语必须使用人物列表中的 ID（如 qq_123），不要用显示名\n"
    "- 将代词替换为对应实体的 ID（如 '他说' → ['qq_123', '说', '...']）\n"
    "- 每条三元组必须包含至少一个实体 ID\n"
    "- 涉及多人的关系要拆分为多条\n"
    "- 只提取明确说出来的事实，不要推测\n"
    "- 如果一句话没有可提取的事实，跳过它\n"
    "- 不要提取情绪、语气、感叹等主观信息\n"
    "- 不要提取常识性内容\n"
    "- 不要提取无意义的三元组（如 ['ID', '说', 'Y']、['ID', '是', '人']）\n"
    "\n"
    "别称提取规则：\n"
    "- 如果有人说'叫我XX'、'大家叫我XX'，提取为别称\n"
    '- 别称格式：["ID", "别称"]（ID 是实体列表中的 ID）\n'
    "- 不要把正式姓名当作别称\n"
    "\n"
    "输出格式（严格 JSON）：\n"
    "{{\n"
    '  "triples": [\n'
    '    ["qq_123", "搬到", "深圳"],\n'
    '    ["qq_123", "搬家时间", "上周"]\n'
    "  ],\n"
    '  "aliases": [\n'
    '    ["qq_123", "深漂"]\n'
    "  ],\n"
    '  "summary": "小明上周搬到深圳，现在自称深漂"\n'
    "}}\n"
    "\n"
    "注意：\n"
    "- triples 中主语用 ID，宾语如果是人也用 ID，否则用原文\n"
    "- aliases 中第一个元素是 ID\n"
    "- summary 用简练且准确的语言概括内容\n"
    "- aliases 可以为空列表 []\n"
    "\n"
    "现在提取以下内容：\n"
    "{conversation}"
)


class SituationExtractor:
    """从消息中提取三元组，生成 Situation。

    提取流程：
    1. 检查消息数量
    2. LLM 三元组提取（轻量模型 + 高结构化 prompt）
    3. 结构化校验（字段完整性、禁止代词）
    4. 演化链验证（矛盾检测 + 置信度评估）
    5. 过滤被拒绝的三元组（LLM 幻觉）
    6. 生成 Situation（自然语言摘要）
    """

    async def extract(
        self,
        group_id: str,
        entries: list[BasicMemoryEntry],
        brain: Any,
        model_name: str,
        evolution_chain: EvolutionChain,
        storage: Any | None = None,
        user_manager: Any | None = None,
    ) -> Situation | None:
        """从消息中提取三元组并生成 Situation。

        Args:
            group_id: 群组 ID
            entries: 候选消息列表
            brain: Brain 实例（用于 LLM 调用）
            model_name: 模型名称（轻量模型）
            evolution_chain: 演化链实例（三元组验证）
            storage: MemoryStorage 实例
            user_manager: UnifiedUserManager 实例（别称注册）

        Returns:
            Situation 或 None（消息不足/提取失败/全部被拒绝）
        """
        if len(entries) < MIN_CANDIDATES:
            logger.debug(
                "群 %s 情景提取消息不足 %d 条（当前 %d 条）",
                group_id, MIN_CANDIDATES, len(entries),
            )
            return None

        # Step 1: LLM 提取三元组
        raw_result = await self._llm_extract(entries, brain, model_name)
        if not raw_result:
            return None

        raw_triples = raw_result.get("triples", [])
        raw_aliases = raw_result.get("aliases", [])
        raw_summary = raw_result.get("summary", "")

        if not raw_triples and not raw_aliases:
            return None

        # Step 1.5: 构建实体集合（user_id → display_name）
        known_entities: dict[str, str] = {}  # user_id → display_name
        for e in entries:
            uid = (e.user_id or "").strip()
            name = (e.speaker_name or "").strip()
            if uid:
                known_entities[uid] = name or uid

        # Step 2: 结构化校验 + 实体校验 + 质量过滤（不信任 LLM 输出）
        validated_raw = []
        for t in raw_triples:
            if not isinstance(t, list) or len(t) != 3:
                logger.debug("三元组格式错误（非三元数组）: %s", t)
                continue
            subject, predicate, obj = str(t[0]).strip(), str(t[1]).strip(), str(t[2]).strip()
            if not subject or not predicate or not obj:
                logger.debug("三元组包含空字段: %s", t)
                continue
            # 代词检查
            pronouns = {"他", "她", "它", "他们", "她们", "它们", "我", "你", "我们", "你们"}
            if subject in pronouns:
                logger.debug("三元组主语是代词: %s", subject)
                continue
            # 主语必须是已知实体 ID
            if subject not in known_entities:
                logger.debug("三元组主语不在实体列表中: %s", subject)
                continue
            validated_raw.append((subject, predicate, obj))

        # Step 3: 别称处理（别称格式：["ID", "别称"]）→ 写入 aliases 表
        if raw_aliases and user_manager is not None:
            for alias_entry in raw_aliases:
                if not isinstance(alias_entry, list) or len(alias_entry) != 2:
                    continue
                alias_user_id = str(alias_entry[0]).strip()
                alias = str(alias_entry[1]).strip()
                if not alias_user_id or not alias:
                    continue
                if alias_user_id not in known_entities:
                    continue
                if alias in ("他", "她", "我", "你", "我们", "他们") or len(alias) < 2:
                    continue
                if alias == alias_user_id:
                    continue
                user_display = known_entities.get(alias_user_id, alias_user_id)
                user_manager.register_alias(
                    alias, alias_user_id, user_display, group_id, source="llm_discovery",
                )
                logger.debug("别称发现: %s → %s (%s)", alias, alias_user_id, user_display)

        # Step 4: 转换为 Triple 对象（主语已是 user_id，无需映射）
        triples = [
            Triple(
                subject=s,
                predicate=p,
                obj=o,
                confidence=0.7,
                meta_tag=MetaTag.STATED,
                subject_user_id=s,
            )
            for s, p, o in validated_raw
        ]

        # Step 5: 演化链验证（核心：不信任 LLM 输出）
        source = SituationSource(
            type="situation_extraction",
            group_id=group_id,
            model=model_name,
            message_ids=[e.entry_id for e in entries],
        )
        validation = await evolution_chain.validate_and_commit(triples, source)

        # Step 5: 只保留通过验证的三元组
        accepted_records = [
            r for r in validation.records
            if r.status in ("active", "uncertain")
        ]
        rejected_count = validation.rejected_count

        if not accepted_records:
            logger.info(
                "群 %s 情景提取：所有三元组被拒绝（%d 个）",
                group_id, len(raw_triples),
            )
            return None

        # Step 6: 生成自然语言摘要（如果 LLM 未提供，用三元组拼接）
        if not raw_summary:
            raw_summary = self._build_summary_from_records(accepted_records)

        # 提取参与者和话题
        participants = list({e.user_id for e in entries if e.user_id})
        topics = self._extract_topics(accepted_records)

        situation = Situation(
            situation_id=str(uuid.uuid4())[:8],
            group_id=group_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            triples=[self._record_to_triple(r) for r in accepted_records],
            participants=participants,
            topics=topics,
            summary=raw_summary,
            source_entry_ids=[e.entry_id for e in entries],
            time_range_start=entries[0].timestamp if entries else "",
            time_range_end=entries[-1].timestamp if entries else "",
            validated_triple_count=len(accepted_records),
            rejected_triple_count=rejected_count,
        )

        logger.info(
            "群 %s 情景提取完成: %d 个三元组通过验证, %d 个被拒绝",
            group_id, len(accepted_records), rejected_count,
        )
        return situation

    # ── LLM 调用 ──

    async def _llm_extract(
        self,
        entries: list[BasicMemoryEntry],
        brain: Any,
        model_name: str,
    ) -> dict[str, Any] | None:
        """调用 LLM 提取三元组。"""
        from sirius_pulse.core.brain import RawRequest

        # 从消息中提取参与者实体列表（显示名(ID) 格式）
        entity_map: dict[str, str] = {}  # user_id → display_name
        for e in entries:
            uid = (e.user_id or "").strip()
            name = (e.speaker_name or "").strip()
            if uid and uid not in entity_map:
                entity_map[uid] = name or uid
        entities_text = "\n".join(
            f"- {name}({uid})" for uid, name in entity_map.items()
        ) if entity_map else "（无）"

        conversation = self._build_conversation_text(entries)
        user_prompt = _TRIPLE_EXTRACTION_PROMPT.format(
            conversation=conversation,
            entities=entities_text,
        )

        raw_request = RawRequest(
            model=model_name,
            system_prompt="你是精确的事实提取助手。只输出 JSON，不要其他文字。",
            messages=[{"role": "user", "content": user_prompt}],
            temperature=0.4,
            max_tokens=1024,
            purpose="situation_extract",
            response_format={"type": "json_object"},
        )

        try:
            raw = await brain.raw_call(raw_request)
        except Exception as exc:
            logger.error("情景提取 LLM 调用失败 (group=%s): %s", "", exc)
            return None

        return self._parse_response(raw)

    # ── 结构化校验 ──

    @staticmethod
    def _build_conversation_text(entries: list[BasicMemoryEntry]) -> str:
        """将消息列表转为 LLM 可读的对话文本。"""
        lines = []
        for e in entries:
            name = e.speaker_name if e.speaker_name else e.user_id
            lines.append(f"{name}({e.user_id}): {e.content}")
        return "\n".join(lines)

    @staticmethod
    def _parse_response(raw: str) -> dict[str, Any] | None:
        """解析 LLM 返回的 JSON。"""
        text = raw.strip()
        if "```" in text:
            parts = text.split("```")
            if len(parts) >= 3:
                text = parts[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
        try:
            result = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("情景提取响应 JSON 解析失败")
            return None
        if isinstance(result, dict):
            return result
        return None

    @staticmethod
    def _record_to_triple(record: Any) -> Triple:
        """将 EvolutionRecord 转换为 Triple。"""
        return Triple(
            subject=record.subject,
            predicate=record.predicate,
            obj=record.obj,
            confidence=record.confidence,
            meta_tag=record.source_type,
            source_message_id=record.source_message_ids[0]
            if record.source_message_ids
            else "",
        )

    @staticmethod
    def _build_summary_from_records(records: list[Any]) -> str:
        """从演化链记录拼接摘要。"""
        parts = []
        for r in records:
            parts.append(f"{r.subject}{r.predicate}{r.obj}")
        return "；".join(parts) if parts else ""

    @staticmethod
    def _extract_topics(records: list[Any]) -> list[str]:
        """从记录中提取话题标签。"""
        topics: set[str] = set()
        for r in records:
            # 用宾语作为话题关键词
            if len(r.obj) >= 2 and len(r.obj) <= 10:
                topics.add(r.obj)
        return list(topics)[:5]
