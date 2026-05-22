"""人物传记系统 — 全局跨群人物认知锚点。

与日记系统互补：
- 日记："发生过什么" → 按群隔离，embedding RAG 检索
- 传记："谁是什么样的" → 全局一张卡，直接注入 prompt

核心 API：BiographyManager（通过 sirius_pulse.memory.biography 导入）。
"""

from __future__ import annotations

from sirius_pulse.memory.biography.manager import BiographyManager
from sirius_pulse.memory.biography.models import (
    AliasEntry,
    RelationshipAnchor,
    UserPersonaCard,
)
from sirius_pulse.memory.biography.store import BiographyStore

__all__ = [
    "UserPersonaCard",
    "RelationshipAnchor",
    "AliasEntry",
    "BiographyStore",
    "BiographyManager",
]
