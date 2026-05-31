"""日记切片数据模型。

DiarySlice 是长期记忆的检索单元。
长日记按 Situation 主题切片后，每个切片独立索引、独立检索。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


def _short_id() -> str:
    return str(uuid.uuid4())[:8]


@dataclass
class DiarySlice:
    """日记切片：长日记的一个主题片段。

    每个切片携带：
    - 叙事性正文（注入上下文用）
    - 三元组索引（精确匹配检索用）
    - embedding（语义检索用）
    - 关键词（降级检索用）
    """

    slice_id: str = field(default_factory=_short_id)
    diary_id: str = ""
    group_id: str = ""

    # ── 内容 ──
    content: str = ""
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)

    # ── 三元组索引（用于精确匹配）──
    triple_subjects: list[str] = field(default_factory=list)
    triple_predicates: list[str] = field(default_factory=list)
    source_record_ids: list[str] = field(default_factory=list)

    # ── 参与者 ──
    participants: list[str] = field(default_factory=list)

    # ── 时间 ──
    time_range_start: str = ""
    time_range_end: str = ""
    index: int = 0

    # ── 检索 ──
    embedding: list[float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slice_id": self.slice_id,
            "diary_id": self.diary_id,
            "group_id": self.group_id,
            "content": self.content,
            "summary": self.summary,
            "keywords": list(self.keywords),
            "topics": list(self.topics),
            "triple_subjects": list(self.triple_subjects),
            "triple_predicates": list(self.triple_predicates),
            "source_record_ids": list(self.source_record_ids),
            "participants": list(self.participants),
            "time_range_start": self.time_range_start,
            "time_range_end": self.time_range_end,
            "index": self.index,
            "embedding": self.embedding,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiarySlice:
        return cls(
            slice_id=data.get("slice_id", _short_id()),
            diary_id=data.get("diary_id", ""),
            group_id=data.get("group_id", ""),
            content=data.get("content", ""),
            summary=data.get("summary", ""),
            keywords=list(data.get("keywords", [])),
            topics=list(data.get("topics", [])),
            triple_subjects=list(data.get("triple_subjects", [])),
            triple_predicates=list(data.get("triple_predicates", [])),
            source_record_ids=list(data.get("source_record_ids", [])),
            participants=list(data.get("participants", [])),
            time_range_start=data.get("time_range_start", ""),
            time_range_end=data.get("time_range_end", ""),
            index=int(data.get("index", 0)),
            embedding=data.get("embedding"),
        )
