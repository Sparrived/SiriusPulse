"""演化链数据模型。

包含：
- Triple：原子事实 (主语, 谓语, 宾语)
- EvolutionRecord：演化链记录，追踪每条信息的完整生命周期
- ValidationResult：验证结果
- EvolutionAction：演化动作枚举
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id() -> str:
    return str(uuid.uuid4())[:8]


class EvolutionAction(str, Enum):
    """演化链决策动作。"""
    ADD = "add"                     # 全新信息，直接添加
    SUPERSEDE = "supersede"         # 矛盾信息，取代旧记录
    UPDATE = "update"               # 补充信息，合并到现有记录
    REJECT = "reject"               # 拒绝（低置信度或幻觉）
    MARK_UNCERTAIN = "mark_uncertain"  # 无法确定，标记待验证


class RecordStatus(str, Enum):
    """演化链记录状态。"""
    ACTIVE = "active"               # 当前有效
    SUPERSEDED = "superseded"       # 被新信息取代（保留历史）
    SHADOW = "shadow"               # 阴影状态（不参与召回，保留可追溯）
    UNCERTAIN = "uncertain"         # 待验证
    REJECTED = "rejected"           # 被拒绝（幻觉/低质量）


class MetaTag(str, Enum):
    """元认知标记：信息来源类型。"""
    STATED = "stated"               # 明确陈述
    INFERRED = "inferred"           # 有直接证据暗示
    UNCERTAIN = "uncertain"         # 不确定
    USER_CORRECTED = "user_corrected"  # 用户显式纠正
    MIGRATION = "migration"         # 从旧系统迁移


@dataclass(slots=True)
class Triple:
    """原子事实：(主语, 谓语, 宾语)。

    三元组是整个记忆系统的原子单位。
    - subject 必须是具体名称，禁止代词
    - subject_user_id 关联别名系统解析的 user_id
    - confidence 表示提取置信度 [0, 1]
    - meta_tag 标记信息来源类型
    """

    subject: str
    predicate: str
    obj: str
    confidence: float = 0.5
    meta_tag: str = MetaTag.STATED
    source_message_id: str = ""
    source_record_id: str = ""
    subject_user_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "subject_user_id": self.subject_user_id,
            "predicate": self.predicate,
            "obj": self.obj,
            "confidence": self.confidence,
            "meta_tag": self.meta_tag,
            "source_message_id": self.source_message_id,
            "source_record_id": self.source_record_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Triple:
        return cls(
            subject=data.get("subject", ""),
            predicate=data.get("predicate", ""),
            obj=data.get("obj", ""),
            confidence=float(data.get("confidence", 0.5)),
            meta_tag=data.get("meta_tag", MetaTag.STATED),
            source_message_id=data.get("source_message_id", ""),
            source_record_id=data.get("source_record_id", ""),
            subject_user_id=data.get("subject_user_id", ""),
        )

    @property
    def content_key(self) -> str:
        """用于去重和比较的内容键。"""
        return f"{self.subject}|{self.predicate}|{self.obj}"


@dataclass
class EvolutionRecord:
    """演化链记录：每条信息的完整生命周期。

    演化链是独立的、永久的验证结构。
    每条记录都带有完整的来源追溯、验证历史和纠正历史。
    """

    record_id: str = field(default_factory=_short_id)

    # ── 三元组内容 ──
    subject: str = ""              # 显示名（LLM 提取的原始名称）
    subject_user_id: str = ""      # 关联的 user_id（别名系统解析）
    predicate: str = ""
    obj: str = ""

    # ── 状态 ──
    status: str = RecordStatus.ACTIVE
    confidence: float = 0.5
    initial_confidence: float = 0.5

    # ── 演化关系 ──
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str | None = None

    # ── 来源追溯 ──
    source_type: str = MetaTag.STATED
    source_situation_id: str = ""
    source_group_id: str = ""
    source_message_ids: list[str] = field(default_factory=list)  # 关联 BasicMemory 消息 ID
    extracted_at: str = field(default_factory=_now_iso)
    extracted_by_model: str = ""

    # ── 验证历史 ──
    verifications: list[dict[str, Any]] = field(default_factory=list)

    # ── 纠正历史 ──
    corrections: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "subject": self.subject,
            "subject_user_id": self.subject_user_id,
            "predicate": self.predicate,
            "obj": self.obj,
            "status": self.status,
            "confidence": self.confidence,
            "initial_confidence": self.initial_confidence,
            "supersedes": list(self.supersedes),
            "superseded_by": self.superseded_by,
            "source_type": self.source_type,
            "source_situation_id": self.source_situation_id,
            "source_group_id": self.source_group_id,
            "source_message_ids": list(self.source_message_ids),
            "extracted_at": self.extracted_at,
            "extracted_by_model": self.extracted_by_model,
            "verifications": list(self.verifications),
            "corrections": list(self.corrections),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvolutionRecord:
        return cls(
            record_id=data.get("record_id", _short_id()),
            subject=data.get("subject", ""),
            subject_user_id=data.get("subject_user_id", ""),
            predicate=data.get("predicate", ""),
            obj=data.get("obj", ""),
            status=data.get("status", RecordStatus.ACTIVE),
            confidence=float(data.get("confidence", 0.5)),
            initial_confidence=float(data.get("initial_confidence", 0.5)),
            supersedes=list(data.get("supersedes", [])),
            superseded_by=data.get("superseded_by"),
            source_type=data.get("source_type", MetaTag.STATED),
            source_situation_id=data.get("source_situation_id", ""),
            source_group_id=data.get("source_group_id", ""),
            source_message_ids=list(data.get("source_message_ids", [])),
            extracted_at=data.get("extracted_at", _now_iso()),
            extracted_by_model=data.get("extracted_by_model", ""),
            verifications=list(data.get("verifications", [])),
            corrections=list(data.get("corrections", [])),
        )

    @property
    def content_key(self) -> str:
        """用于去重和比较的内容键。"""
        return f"{self.subject}|{self.predicate}|{self.obj}"

    @property
    def is_active(self) -> bool:
        return self.status == RecordStatus.ACTIVE

    @property
    def is_shadowed(self) -> bool:
        return self.status in (RecordStatus.SUPERSEDED, RecordStatus.SHADOW)

    def add_verification(
        self,
        verification_type: str,
        details: str,
        confidence_delta: float = 0.0,
    ) -> None:
        """记录一次验证事件。"""
        self.verifications.append({
            "verified_at": _now_iso(),
            "type": verification_type,
            "details": details,
            "confidence_delta": confidence_delta,
        })
        self.confidence = max(0.0, min(1.0, self.confidence + confidence_delta))

    def add_correction(
        self,
        old_value: str,
        new_value: str,
        reason: str,
        cascade_affected: list[str] | None = None,
    ) -> None:
        """记录一次纠正事件。"""
        self.corrections.append({
            "corrected_at": _now_iso(),
            "old_value": old_value,
            "new_value": new_value,
            "reason": reason,
            "cascade_affected": cascade_affected or [],
        })


@dataclass
class SituationSource:
    """情景来源：记录信息提取的上下文。"""
    type: str = "situation_extraction"
    situation_id: str = ""
    group_id: str = ""
    model: str = ""
    message_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "situation_id": self.situation_id,
            "group_id": self.group_id,
            "model": self.model,
            "message_ids": list(self.message_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SituationSource:
        return cls(
            type=data.get("type", "situation_extraction"),
            situation_id=data.get("situation_id", ""),
            group_id=data.get("group_id", ""),
            model=data.get("model", ""),
            message_ids=list(data.get("message_ids", [])),
        )


@dataclass
class ValidationResult:
    """验证结果：一次 validate_and_commit 的返回。"""
    records: list[EvolutionRecord] = field(default_factory=list)
    actions: list[EvolutionAction] = field(default_factory=list)
    rejected_triples: list[Triple] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return len(self.records)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected_triples)

    @property
    def superseded_count(self) -> int:
        return sum(1 for a in self.actions if a == EvolutionAction.SUPERSEDE)
