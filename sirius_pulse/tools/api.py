"""Tool 开发统一 API 入口 —— 为自定义工具编写者提供一站式导入。

使用方式：

    from sirius_pulse.tools.api import (
        ToolResult,              # 结构化返回结果
        ToolEngineContext,       # 被动/后台工具的引擎上下文 Protocol（各领域接口的合集）
        WorkspaceAccess,         # 按领域的小接口：工具只依赖用到的那几个
        ToolInvocationContext,   # 调用者身份信息
        ToolChainContext,        # Tool Chaining 上下文
        BackgroundTaskSpec,       # 后台任务规格
        TriggerSpec,              # 事件触发规格
        ToolPassiveType,         # 被动工具类型枚举
        ToolParameter,           # 工具参数定义
        ToolDataStore,           # 持久化 KV 存储
        ensure_developer_access,  # 开发者权限检查
    )

所有符号均为 re-export，不包含新的逻辑实现。
"""

from __future__ import annotations

from sirius_pulse.extension_runtime import BackgroundTaskSpec
from sirius_pulse.tools.data_store import ToolDataStore
from sirius_pulse.tools.engine_context import (
    AgentTurnAccess,
    ConversationAccess,
    DeliveryAccess,
    GenerationAccess,
    LifeAccess,
    PeopleAccess,
    ToolingAccess,
    UserLookupAccess,
    WorkspaceAccess,
)
from sirius_pulse.tools.models import (
    ToolChainContext,
    ToolEngineContext,
    ToolInvocationContext,
    ToolParameter,
    ToolPassiveType,
    ToolResult,
    TriggerSpec,
)
from sirius_pulse.tools.security import ensure_developer_access

__all__ = [
    "AgentTurnAccess",
    "ConversationAccess",
    "DeliveryAccess",
    "GenerationAccess",
    "LifeAccess",
    "PeopleAccess",
    "ToolingAccess",
    "UserLookupAccess",
    "WorkspaceAccess",
    "BackgroundTaskSpec",
    "ToolChainContext",
    "ToolDataStore",
    "ToolEngineContext",
    "ToolInvocationContext",
    "ToolParameter",
    "ToolPassiveType",
    "ToolResult",
    "TriggerSpec",
    "ensure_developer_access",
]
