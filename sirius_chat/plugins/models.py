"""Plugin 系统核心数据模型。

定义 Plugin 的元数据、指令 AST、执行结果、渲染模式等核心契约。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class RenderMode(enum.Enum):
    """Plugin 输出策略。

    - direct: 直接使用 PluginResult.text 作为最终回复，不经过人格风格化。
    - llm: 将 PluginResult 的结构化数据委托给引擎做人格化生成。
    - silent: 无输出，仅执行副作用（如踢人、设置管理等）。
    """

    DIRECT = "direct"
    LLM = "llm"
    SILENT = "silent"


class TriggerType(enum.Enum):
    """Plugin 触发方式。"""

    COMMAND = "command"          # 用户指令触发（关键词/前缀/正则）
    EVENT_TIMER = "timer"        # 定时事件（cron/interval）
    EVENT_WEBHOOK = "webhook"    # Webhook 事件
    EVENT_ENGINE = "engine"      # 引擎生命周期事件
    EVENT_FILESYSTEM = "fs"      # 文件系统事件


class PatternType(enum.Enum):
    """指令匹配模式类型。"""

    PREFIX = "prefix"            # 前缀匹配（如 "/天气"）
    REGEX = "regex"              # 正则匹配
    KEYWORD = "keyword"          # 关键词包含匹配


# ═══════════════════════════════════════════════════════════════════════
# 指令 AST
# ═══════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class ArgNode:
    """指令参数节点。"""

    value: str | int | float | bool
    raw: str                       # 原始字符串
    type_hint: str = "str"         # 来自 Plugin 参数定义的类型提示


@dataclass(slots=True)
class CommandAST:
    """Plugin 指令的抽象语法树。

    由 Lexer/Parser 从用户输入中解析生成。
    """

    command: str                          # 指令名，如 "weather"
    raw_text: str                         # 原始完整文本
    prefix: str = ""                      # 触发前缀，如 "/"、"#"
    args: list[ArgNode] = field(default_factory=list)           # 位置参数列表
    kwargs: dict[str, ArgNode] = field(default_factory=dict)    # 命名参数
    flags: set[str] = field(default_factory=set)                # 布尔开关

    def get_positional(self, index: int) -> str | None:
        """按位置获取参数的原始字符串值。"""
        if 0 <= index < len(self.args):
            return self.args[index].raw
        return None

    def get_str(self, name: str, default: str = "") -> str:
        """获取命名 / 位置参数的字符串值。"""
        if name in self.kwargs:
            return str(self.kwargs[name].value)
        return default

    def get_int(self, name: str, default: int = 0) -> int:
        """获取命名 / 位置参数的整数值。"""
        if name in self.kwargs:
            try:
                return int(self.kwargs[name].value)
            except (ValueError, TypeError):
                return default
        return default

    def get_float(self, name: str, default: float = 0.0) -> float:
        """获取命名 / 位置参数的浮点值。"""
        if name in self.kwargs:
            try:
                return float(self.kwargs[name].value)
            except (ValueError, TypeError):
                return default
        return default

    def get_bool(self, name: str, default: bool = False) -> bool:
        """获取布尔标志或命名参数。"""
        if name in self.flags:
            return True
        if name in self.kwargs:
            val = self.kwargs[name].value
            if isinstance(val, bool):
                return val
            return str(val).lower() in ("true", "1", "yes")
        return default

    def to_dict(self) -> dict[str, Any]:
        """序列化为可读字典。"""
        return {
            "command": self.command,
            "raw_text": self.raw_text,
            "prefix": self.prefix,
            "args": [{"value": a.value, "raw": a.raw, "type_hint": a.type_hint} for a in self.args],
            "kwargs": {k: {"value": v.value, "raw": v.raw, "type_hint": v.type_hint} for k, v in self.kwargs.items()},
            "flags": sorted(self.flags),
        }


# ═══════════════════════════════════════════════════════════════════════
# Plugin 定义
# ═══════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class PluginCommandDef:
    """Plugin 指令触发器定义。"""

    name: str                              # 指令名（对应 CommandAST.command）
    patterns: list[str] = field(default_factory=list)          # 触发词列表
    pattern_type: str = "prefix"           # prefix | regex | keyword
    description: str = ""
    examples: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PluginEventDef:
    """Plugin 事件触发器定义。"""

    type: str                              # "timer.daily" / "webhook" / "engine.xxx"
    cron: str = ""                         # cron 表达式（定时事件）
    interval_seconds: float = 0.0          # 间隔秒数（interval 事件）
    description: str = ""


@dataclass(slots=True)
class PluginParameterDef:
    """Plugin 参数定义。"""

    name: str
    type: str = "str"                      # str | int | float | bool | list[str]
    description: str = ""
    required: bool = False
    default: Any = None
    position: int = 0                      # 位置参数序号
    choices: list[str] | None = None       # 可选值限制


@dataclass(slots=True)
class PluginPermissionDef:
    """Plugin 权限定义。"""

    developer_only: bool = False
    adapter_types: list[str] = field(default_factory=list)      # 允许的平台列表
    group_whitelist: list[str] = field(default_factory=list)    # 群白名单
    group_blacklist: list[str] = field(default_factory=list)    # 群黑名单
    user_whitelist: list[str] = field(default_factory=list)     # 用户白名单
    # 速率限制
    rate_limit_calls_per_minute: int = 60
    rate_limit_calls_per_hour: int = 1000


@dataclass(slots=True)
class PluginRenderDef:
    """Plugin 渲染策略定义。"""

    mode: str = "direct"                   # direct | llm | silent
    system_prompt_suffix: str = ""         # llm 模式下追加的 system prompt
    max_tokens: int = 500
    temperature: float = 0.8


@dataclass(slots=True)
class PluginNaturalLangDef:
    """自然语言触发定义（用于 CognitionAnalyzer 融合识别）。"""

    examples: list[str] = field(default_factory=list)           # 示例语料，如 "帮我查一下{city}的天气"
    slots: dict[str, dict[str, Any]] = field(default_factory=dict)  # 槽位定义


@dataclass(slots=True)
class PluginDefinition:
    """Plugin 完整定义，由 plugin.json 解析生成。

    这是 Plugin 系统的核心数据契约，包含了从元数据到运行时所需的所有信息。
    """

    # ── 基本信息 ──
    name: str                              # 内部标识名
    display_name: str = ""                 # 显示名称
    description: str = ""
    version: str = "1.0.0"
    author: str = ""
    min_framework_version: str = "1.2.0"

    # ── 触发器 ──
    commands: list[PluginCommandDef] = field(default_factory=list)
    events: list[PluginEventDef] = field(default_factory=list)

    # ── 参数 ──
    parameters: list[PluginParameterDef] = field(default_factory=list)
    natural_language: PluginNaturalLangDef | None = None

    # ── 权限与渲染 ──
    permissions: PluginPermissionDef = field(default_factory=PluginPermissionDef)
    render: PluginRenderDef = field(default_factory=PluginRenderDef)

    # ── 依赖与资源 ──
    dependencies: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)

    # ── 内部字段 ──
    source_path: Path | None = None        # 插件文件夹路径
    _plugin_class: type | None = field(default=None, repr=False)  # PluginBase 子类

    @property
    def all_patterns(self) -> list[tuple[str, str, str]]:
        """返回所有指令的 (指令名, 触发词, 匹配类型) 三元组列表。"""
        result: list[tuple[str, str, str]] = []
        for cmd in self.commands:
            for pat in cmd.patterns:
                result.append((cmd.name, pat, cmd.pattern_type))
        return result

    @property
    def is_passive(self) -> bool:
        """是否仅由事件触发（无指令触发器）。"""
        return len(self.commands) == 0 and len(self.events) > 0

    @staticmethod
    def from_dict(data: dict[str, Any], source_path: Path | None = None) -> PluginDefinition:
        """从 plugin.json 字典构建 PluginDefinition。"""
        # 解析触发器
        commands: list[PluginCommandDef] = []
        for cmd_raw in data.get("triggers", {}).get("commands", []):
            commands.append(PluginCommandDef(
                name=cmd_raw.get("name", ""),
                patterns=cmd_raw.get("patterns", []),
                pattern_type=cmd_raw.get("pattern_type", "prefix"),
                description=cmd_raw.get("description", ""),
                examples=cmd_raw.get("examples", []),
            ))
        events: list[PluginEventDef] = []
        for evt_raw in data.get("triggers", {}).get("events", []):
            events.append(PluginEventDef(
                type=evt_raw.get("type", ""),
                cron=evt_raw.get("cron", ""),
                interval_seconds=float(evt_raw.get("interval_seconds", 0)),
                description=evt_raw.get("description", ""),
            ))

        # 解析参数
        parameters: list[PluginParameterDef] = []
        for name, param_raw in data.get("parameters", {}).items():
            parameters.append(PluginParameterDef(
                name=name,
                type=param_raw.get("type", "str"),
                description=param_raw.get("description", ""),
                required=param_raw.get("required", False),
                default=param_raw.get("default"),
                position=param_raw.get("position", 0),
                choices=param_raw.get("choices"),
            ))

        # 解析自然语言触发
        nl_raw = data.get("natural_language")
        nl_def: PluginNaturalLangDef | None = None
        if nl_raw:
            nl_def = PluginNaturalLangDef(
                examples=nl_raw.get("examples", []),
                slots=nl_raw.get("slots", {}),
            )

        # 解析权限
        perm_raw = data.get("permissions", {})
        permissions = PluginPermissionDef(
            developer_only=perm_raw.get("developer_only", False),
            adapter_types=perm_raw.get("adapter_types", []),
            group_whitelist=perm_raw.get("group_whitelist", []),
            group_blacklist=perm_raw.get("group_blacklist", []),
            user_whitelist=perm_raw.get("user_whitelist", []),
            rate_limit_calls_per_minute=perm_raw.get("rate_limit", {}).get("calls_per_minute", 60),
            rate_limit_calls_per_hour=perm_raw.get("rate_limit", {}).get("calls_per_hour", 1000),
        )

        # 解析渲染
        render_raw = data.get("render", {})
        render = PluginRenderDef(
            mode=render_raw.get("mode", "direct"),
            system_prompt_suffix=render_raw.get("system_prompt_suffix", ""),
            max_tokens=render_raw.get("max_tokens", 500),
            temperature=render_raw.get("temperature", 0.8),
        )

        return PluginDefinition(
            name=data.get("name", ""),
            display_name=data.get("display_name", ""),
            description=data.get("description", ""),
            version=data.get("version", "1.0.0"),
            author=data.get("author", ""),
            min_framework_version=data.get("min_framework_version", "1.2.0"),
            commands=commands,
            events=events,
            parameters=parameters,
            natural_language=nl_def,
            permissions=permissions,
            render=render,
            dependencies=data.get("dependencies", []),
            resources=data.get("resources", []),
            source_path=source_path,
        )

    def get_render_mode(self) -> RenderMode:
        """将字符串渲染模式转换为枚举。"""
        mode = self.render.mode.lower()
        if mode == "llm":
            return RenderMode.LLM
        if mode == "silent":
            return RenderMode.SILENT
        return RenderMode.DIRECT


# ═══════════════════════════════════════════════════════════════════════
# Plugin 执行结果
# ═══════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class PluginResult:
    """Plugin 执行结果。

    Plugin 的 run() 返回此结构，由 OutputDispatcher 根据 render_mode 处理。
    """

    success: bool = True
    data: Any = None                     # 结构化数据（llm 模式下用于风格化生成）
    text: str = ""                        # 纯文本输出（direct 模式下直接发送）
    error: str = ""                       # 错误信息
    render_mode: str = ""                 # 覆盖 plugin.json 中的 render.mode
    mood_hint: str = ""                   # 情绪提示（用于 llm 风格化）
    tone_override: str = ""               # 语气覆写
    image_urls: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def ok(text: str = "", data: Any = None, **kwargs: Any) -> PluginResult:
        """快捷构造成功的 PluginResult。"""
        return PluginResult(success=True, text=text, data=data, **kwargs)

    @staticmethod
    def fail(error: str) -> PluginResult:
        """快捷构造失败的 PluginResult。"""
        return PluginResult(success=False, error=error)


# ═══════════════════════════════════════════════════════════════════════
# 平台感知数据类型
# ═══════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class UserMention:
    """被 @ 的用户。"""

    user_id: str
    nickname: str = ""
    group_card: str | None = None


@dataclass(slots=True)
class GroupMention:
    """群聊上下文。"""

    group_id: str
    group_name: str | None = None


@dataclass(slots=True)
class MessageReference:
    """回复的消息引用。"""

    message_id: str
    sender_id: str = ""
    original_content: str = ""


@dataclass(slots=True)
class ImageAttachment:
    """消息中的图片。"""

    url: str
    local_path: str | None = None
    is_sticker: bool = False
