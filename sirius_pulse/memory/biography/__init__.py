"""人物传记系统 — 已合并到 UnifiedUserManager。

此模块保留向后兼容的导入，实际功能已迁移到：
- sirius_pulse.memory.user.unified_models (UnifiedUser, RelationshipAnchor, AliasEntry)
- sirius_pulse.memory.user.unified_manager (UnifiedUserManager)
"""

from __future__ import annotations

from sirius_pulse.memory.user.unified_models import (
    AliasEntry,
    RelationshipAnchor,
    UnifiedUser,
)

__all__ = [
    "UnifiedUser",
    "RelationshipAnchor",
    "AliasEntry",
]
