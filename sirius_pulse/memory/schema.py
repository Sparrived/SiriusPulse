"""Schema 行为模式归纳。

从演化链的 active 三元组中归纳出反复出现的行为模式。
不使用简单关键词识别，通过 LLM 分析三元组模式。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sirius_pulse.memory.evolution.chain import EvolutionChain
from sirius_pulse.memory.evolution.models import EvolutionRecord

logger = logging.getLogger(__name__)

__all__ = ["BehaviorSchema", "SchemaInductor"]


@dataclass
class BehaviorSchema:
    """行为模式：从多条三元组中归纳出的抽象模式。"""
    schema_id: str = ""
    central_proposition: str = ""     # 核心命题
    supporting_evidence: list[str] = field(default_factory=list)
    expected_inferences: list[str] = field(default_factory=list)
    confidence: float = 0.0
    formed_at: str = ""
    last_validated: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "central_proposition": self.central_proposition,
            "supporting_evidence": list(self.supporting_evidence),
            "expected_inferences": list(self.expected_inferences),
            "confidence": self.confidence,
            "formed_at": self.formed_at,
            "last_validated": self.last_validated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BehaviorSchema:
        return cls(
            schema_id=data.get("schema_id", ""),
            central_proposition=data.get("central_proposition", ""),
            supporting_evidence=list(data.get("supporting_evidence", [])),
            expected_inferences=list(data.get("expected_inferences", [])),
            confidence=float(data.get("confidence", 0.0)),
            formed_at=data.get("formed_at", ""),
            last_validated=data.get("last_validated", ""),
        )


_SCHEMA_PROMPT = """基于以下用户事实，归纳 2-3 个核心行为模式。

事实列表：
{facts_text}

要求：
- 每个模式是一句话描述的反复出现的行为倾向
- 不是简单列举事实，而是抽象出底层模式
- 每个模式附带 1-2 个预期推断

输出 JSON：
{{
  "schemas": [
    {{
      "central_proposition": "核心命题",
      "supporting_evidence": ["证据1", "证据2"],
      "expected_inferences": ["预期推断1"],
      "confidence": 0.8
    }}
  ]
}}
"""


class SchemaInductor:
    """Schema 归纳器：从演化链三元组中归纳行为模式。"""

    MIN_FACTS = 5  # 最少事实数才触发归纳

    async def induct(
        self,
        user_id: str,
        chain: EvolutionChain,
        brain: Any,
        model_name: str,
    ) -> list[BehaviorSchema]:
        """归纳用户的行为模式。

        Args:
            user_id: 用户 ID
            chain: 演化链
            brain: Brain 实例
            model_name: 模型名称

        Returns:
            行为模式列表
        """
        # 获取用户的 active 三元组
        records = chain.get_active_by_user_id(user_id)
        if not records:
            records = chain.get_active_by_subject(user_id)

        if len(records) < self.MIN_FACTS:
            return []

        # 构建事实文本
        facts_text = "\n".join(
            f"- {r.subject}{r.predicate}{r.obj} (置信度: {r.confidence:.2f})"
            for r in records[:20]
        )

        # 调用 LLM 归纳
        from sirius_pulse.core.brain import RawRequest

        prompt = _SCHEMA_PROMPT.format(facts_text=facts_text)
        raw_request = RawRequest(
            model=model_name,
            system_prompt="你是行为模式分析助手。只输出 JSON。",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=512,
            purpose="schema_induct",
            response_format={"type": "json_object"},
        )

        try:
            raw = await brain.raw_call(raw_request)
        except Exception as exc:
            logger.error("Schema 归纳 LLM 调用失败: %s", exc)
            return []

        # 解析结果
        parsed = self._parse_response(raw)
        if not parsed:
            return []

        schemas = []
        for s in parsed.get("schemas", []):
            if not s.get("central_proposition"):
                continue
            schemas.append(BehaviorSchema(
                central_proposition=s["central_proposition"],
                supporting_evidence=s.get("supporting_evidence", []),
                expected_inferences=s.get("expected_inferences", []),
                confidence=float(s.get("confidence", 0.5)),
            ))

        return schemas[:3]

    @staticmethod
    def _parse_response(raw: str) -> dict[str, Any] | None:
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
            return None
        return result if isinstance(result, dict) else None
