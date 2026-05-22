"""Sirius Chat Plugin 系统 —— 用户/事件驱动的指令响应系统。

提供精确的词法路由、可选的人格风格化生成、以及丰富的平台 Adapter 接口。

公开 API:
    - PluginBase: 所有插件的基类
    - PluginContext / PluginContext: Plugin 执行上下文
    - PluginDefinition / PluginResponse / CommandAST: 核心数据模型
    - PluginRegistry: 插件注册表
    - PluginLoader: 插件加载器
    - PluginExecutor: 插件执行器
    - OutputDispatcher: 输出调度器
    - Tokenizer / Lexer / CommandParser: 词法分析工具
    - PluginMatcher / MatchResult: 文本匹配工具
"""

from __future__ import annotations

from sirius_pulse.plugins.base import PluginBase
from sirius_pulse.plugins.context import (
    EngineProxy,
    MessageContext,
    PluginContext,
    PluginDataStore,
)
from sirius_pulse.plugins.decorators import command, PluginCommandMeta, discover_commands, dispatch_command
from sirius_pulse.plugins.dispatcher import DispatchedOutput, OutputDispatcher
from sirius_pulse.plugins.events import PluginEvent, PluginEventType, TimerEvent, EngineEvent
from sirius_pulse.plugins.executor import PluginExecutor
from sirius_pulse.plugins.lexer import (
    CommandParser,
    LexedCommand,
    Lexer,
    MatchResult,
    PluginMatcher,
    Tokenizer,
    parse_command,
    match_plugin,
)
from sirius_pulse.plugins.loader import PluginLoadError, PluginLoader
from sirius_pulse.plugins.models import (
    ArgNode,
    CommandAST,
    PatternType,
    PluginCommandDef,
    PluginDefinition,
    PluginEventDef,
    PluginParameterDef,
    PluginPermissionDef,
    PluginRenderDef,
    PluginResponse,
    PluginNaturalLangDef,
    RenderMode,
    TriggerType,
    UserMention,
    GroupMention,
    MessageReference,
    ImageAttachment,
)
from sirius_pulse.plugins.registry import PluginRegistry
from sirius_pulse.plugins.scheduler import PluginScheduler, ScheduledTask

__all__ = [
    # 核心类
    "PluginBase",
    "PluginContext",
    "PluginDefinition",
    "PluginResponse",
    "CommandAST",
    # 装饰器系统
    "command",
    "PluginCommandMeta",
    "discover_commands",
    "dispatch_command",
    # 注册与加载
    "PluginRegistry",
    "PluginLoader",
    "PluginLoadError",
    "PluginExecutor",
    # 输出调度
    "OutputDispatcher",
    # 事件系统
    "PluginEvent",
    "PluginEventType",
    "TimerEvent",
    "EngineEvent",
    # 调度器
    "PluginScheduler",
    "ScheduledTask",
    # 词法分析
    "Tokenizer",
    "Lexer",
    "LexedCommand",
    "CommandParser",
    "PluginMatcher",
    "MatchResult",
    "parse_command",
    "match_plugin",
    # 上下文
    "EngineProxy",
    "MessageContext",
    "PluginDataStore",
    # 数据模型
    "ArgNode",
    "RenderMode",
    "TriggerType",
    "PatternType",
    "PluginCommandDef",
    "PluginEventDef",
    "PluginParameterDef",
    "PluginPermissionDef",
    "PluginRenderDef",
    "PluginNaturalLangDef",
    # 平台感知类型
    "UserMention",
    "GroupMention",
    "MessageReference",
    "ImageAttachment",
]
