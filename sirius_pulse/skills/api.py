"""Skill 开发统一 API 入口 —— 为自定义技能编写者提供一站式导入。

使用方式：

    from sirius_pulse.skills.api import (
        SkillResult,              # 结构化返回结果
        SkillEngineContext,       # 被动/后台技能的引擎上下文 Protocol
        SkillInvocationContext,   # 调用者身份信息
        SkillChainContext,        # Skill Chaining 上下文
        BackgroundTaskSpec,       # 后台任务规格
        TriggerSpec,              # 事件触发规格
        SkillPassiveType,         # 被动技能类型枚举
        SkillParameter,           # 技能参数定义
        SkillDataStore,           # 持久化 KV 存储
        ensure_developer_access,  # 开发者权限检查
    )

所有符号均为 re-export，不包含新的逻辑实现。
"""

from __future__ import annotations

from sirius_pulse.skills.data_store import SkillDataStore
from sirius_pulse.skills.models import (
    BackgroundTaskSpec,
    SkillChainContext,
    SkillEngineContext,
    SkillInvocationContext,
    SkillParameter,
    SkillPassiveType,
    SkillResult,
    TriggerSpec,
)
from sirius_pulse.skills.security import ensure_developer_access

__all__ = [
    "BackgroundTaskSpec",
    "SkillChainContext",
    "SkillDataStore",
    "SkillEngineContext",
    "SkillInvocationContext",
    "SkillParameter",
    "SkillPassiveType",
    "SkillResult",
    "TriggerSpec",
    "ensure_developer_access",
]
