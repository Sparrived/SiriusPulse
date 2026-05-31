"""情景压缩数据模型。

包含：
- Situation：一次暂冷时的结构化压缩
- SituationSource：情景来源（复用自 evolution.models）
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sirius_pulse.memory.evolution.models import Triple


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id() -> str:
    return str(uuid.uuid4())[:8]


@dataclass
class Situation:
    """情景：一次暂冷时的结构化压缩。

    暂冷（5分钟无消息）时，从当天消息中提取三元组并生成自然语言摘要。
    摘要注入 bot 回复的上下文，替代原始消息。
    """

    situation_id: str = field(default_factory=_short_id)
    group_id: str = ""
    created_at: str = field(default_factory=_now_iso)

    # ── 内容 ──
    triples: list[Triple] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    summary: str = ""

    # ── 来源 ──
    source_entry_ids: list[str] = field(default_factory=list)
    time_range_start: str = ""
    time_range_end: str = ""

    # ── 验证状态 ──
    validated_triple_count: int = 0
    rejected_triple_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "situation_id": self.situation_id,
            "group_id": self.group_id,
            "created_at": self.created_at,
            "triples": [t.to_dict() for t in self.triples],
            "participants": list(self.participants),
            "topics": list(self.topics),
            "summary": self.summary,
            "source_entry_ids": list(self.source_entry_ids),
            "time_range_start": self.time_range_start,
            "time_range_end": self.time_range_end,
            "validated_triple_count": self.validated_triple_count,
            "rejected_triple_count": self.rejected_triple_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Situation:
        return cls(
            situation_id=data.get("situation_id", _short_id()),
            group_id=data.get("group_id", ""),
            created_at=data.get("created_at", _now_iso()),
            triples=[Triple.from_dict(t) for t in data.get("triples", [])],
            participants=list(data.get("participants", [])),
            topics=list(data.get("topics", [])),
            summary=data.get("summary", ""),
            source_entry_ids=list(data.get("source_entry_ids", [])),
            time_range_start=data.get("time_range_start", ""),
            time_range_end=data.get("time_range_end", ""),
            validated_triple_count=int(data.get("validated_triple_count", 0)),
            rejected_triple_count=int(data.get("rejected_triple_count", 0)),
        )
