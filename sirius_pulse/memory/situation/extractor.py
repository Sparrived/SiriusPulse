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


_TRIPLE_EXTRACTION_PROMPT = """你是事实提取助手。从群聊片段中提取结构化事实。

严格规则：
- 每条事实用 (主语, 谓语, 宾语) 表示
- 主语必须是具体人名，绝对不要用代词（"他说" → "小明说"）
- 只提取明确说出来的事实，不要推测、不要脑补
- 如果一句话没有可提取的事实，跳过它
- 不要提取情绪、语气、感叹等主观信息
- 不要提取常识性内容
- 每条事实附带置信度 (0.0-1.0)

示例输入：
小明(123): 我搬到深圳了，北京的房子退了
小红(456): 什么时候的事？怎么不早说
小明(123): 上周刚搬的

示例输出：
{
  "triples": [
    {"subject": "小明", "predicate": "搬到", "obj": "深圳", "confidence": 0.95, "meta_tag": "stated"},
    {"subject": "小明", "predicate": "退了", "obj": "北京的房子", "confidence": 0.95, "meta_tag": "stated"},
    {"subject": "小明", "predicate": "搬家时间", "obj": "上周", "confidence": 0.9, "meta_tag": "stated"}
  ],
  "summary": "小明上周搬到深圳，退了北京的房子，小红之前不知道这件事"
}

注意：
- 小红的问句没有可提取的事实，不要强行提取
- 置信度反映事实的确定程度：明确陈述>=0.8，暗示>=0.5，推测<0.5

现在提取以下内容：
{conversation}
"""


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
    ) -> Situation | None:
        """从消息中提取三元组并生成 Situation。

        Args:
            group_id: 群组 ID
            entries: 候选消息列表
            brain: Brain 实例（用于 LLM 调用）
            model_name: 模型名称（轻量模型）
            evolution_chain: 演化链实例

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
        raw_summary = raw_result.get("summary", "")

        if not raw_triples:
            return None

        # Step 2: 结构化校验（不信任 LLM 输出）
        validated_raw = []
        for t in raw_triples:
            error = self._validate_triple_structure(t)
            if error:
                logger.debug("三元组结构校验失败: %s -> %s", t, error)
                continue
            validated_raw.append(t)

        if not validated_raw:
            return None

        # Step 3: 转换为 Triple 对象
        triples = [
            Triple(
                subject=t["subject"].strip(),
                predicate=t["predicate"].strip(),
                obj=t["obj"].strip(),
                confidence=float(t.get("confidence", 0.5)),
                meta_tag=t.get("meta_tag", MetaTag.STATED),
            )
            for t in validated_raw
        ]

        # Step 4: 演化链验证（核心：不信任 LLM 输出）
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
        if not raw_summary or len(raw_summary) < 10:
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

        conversation = self._build_conversation_text(entries)
        user_prompt = _TRIPLE_EXTRACTION_PROMPT.format(conversation=conversation)

        raw_request = RawRequest(
            model=model_name,
            system_prompt="你是精确的事实提取助手。只输出 JSON，不要其他文字。",
            messages=[{"role": "user", "content": user_prompt}],
            temperature=0.1,
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
    def _validate_triple_structure(triple_dict: dict) -> str | None:
        """校验三元组结构完整性，返回错误信息或 None。

        不信任 LLM 输出：检查字段完整性、类型、禁止代词。
        """
        subject = triple_dict.get("subject", "").strip()
        predicate = triple_dict.get("predicate", "").strip()
        obj = triple_dict.get("obj", "").strip()

        if not subject:
            return "主语为空"
        if not predicate:
            return "谓语为空"
        if not obj:
            return "宾语为空"

        # 禁止代词作为主语
        pronouns = {"他", "她", "它", "他们", "她们", "它们", "我", "你", "我们", "你们"}
        if subject in pronouns:
            return f"主语是代词: {subject}"

        # 置信度范围校验
        confidence = triple_dict.get("confidence", 0.5)
        try:
            confidence = float(confidence)
            if confidence < 0 or confidence > 1:
                return f"置信度超出范围: {confidence}"
        except (ValueError, TypeError):
            return f"置信度格式错误: {confidence}"

        return None

    # ── 工具方法 ──

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
