"""人物传记系统。

包含：
- BiographyView：从演化链自动派生传记（新架构）
- UserBiography：传记数据模型
- 向后兼容导入：UnifiedUser, RelationshipAnchor, AliasEntry
"""

from __future__ import annotations

from sirius_pulse.memory.user.unified_models import (
    AliasEntry,
    RelationshipAnchor,
    UnifiedUser,
)
from sirius_pulse.memory.biography.models import UserBiography
from sirius_pulse.memory.biography.view import BiographyView

__all__ = [
    "UnifiedUser",
    "RelationshipAnchor",
    "AliasEntry",
    "UserBiography",
    "BiographyView",
]
