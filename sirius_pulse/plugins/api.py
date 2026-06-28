"""Plugin 开发统一 API 入口 —— 为自定义插件编写者提供一站式导入。

使用方式：

    from sirius_pulse.plugins.api import (
        PluginBase,               # 插件基类（所有插件必须继承）
        command,                  # 声明式指令注册装饰器
        command_group,            # 声明式指令组注册装饰器
        group_command,            # 声明式子命令注册装饰器
        PluginResponse,           # 返回结果
        PluginContext,            # 运行时上下文
        EngineProxy,              # 引擎安全代理
        PluginDataStore,          # 持久化 KV 存储
        CommandAST,               # 指令抽象语法树
        PluginCommandMeta,        # @command 装饰器记录的元数据
        PluginCommandGroupMeta,   # @command_group 装饰器记录的元数据
        GroupCommandMeta,         # @group_command 装饰器记录的元数据
        RenderMode,               # 输出策略枚举
        TriggerType,              # 触发方式枚举
        PatternType,              # 匹配模式枚举
        PluginDefinition,         # 插件完整定义
        PluginCommandDef,         # 指令触发器定义
        PluginCommandGroupDef,    # 指令组定义
        PluginEventDef,           # 事件触发器定义
        PluginPermissionDef,      # 权限定义
        PluginRenderDef,          # 渲染策略定义
    )

所有符号均为 re-export，不包含新的逻辑实现。
"""

from __future__ import annotations

from sirius_pulse.plugins.base import PluginBase
from sirius_pulse.plugins.context import EngineProxy, PluginContext, PluginDataStore
from sirius_pulse.plugins.decorators import (
    GroupCommandMeta,
    PluginCommandGroupMeta,
    PluginCommandMeta,
    command,
    command_group,
    group_command,
)
from sirius_pulse.plugins.dispatcher import DispatchedOutput
from sirius_pulse.plugins.models import (
    ArgNode,
    CommandAST,
    GroupMention,
    ImageAttachment,
    MessageReference,
    PatternType,
    PluginCommandDef,
    PluginCommandGroupDef,
    PluginDefinition,
    PluginEventDef,
    PluginNaturalLangDef,
    PluginParameterDef,
    PluginPermissionDef,
    PluginRenderDef,
    PluginResponse,
    RenderMode,
    TriggerType,
    UserMention,
)

__all__ = [
    "ArgNode",
    "CommandAST",
    "DispatchedOutput",
    "EngineProxy",
    "GroupCommandMeta",
    "GroupMention",
    "ImageAttachment",
    "MessageReference",
    "PatternType",
    "PluginBase",
    "PluginCommandDef",
    "PluginCommandGroupDef",
    "PluginCommandGroupMeta",
    "PluginCommandMeta",
    "PluginContext",
    "PluginDataStore",
    "PluginDefinition",
    "PluginEventDef",
    "PluginNaturalLangDef",
    "PluginParameterDef",
    "PluginPermissionDef",
    "PluginRenderDef",
    "PluginResponse",
    "RenderMode",
    "TriggerType",
    "UserMention",
    "command",
    "command_group",
    "group_command",
]
