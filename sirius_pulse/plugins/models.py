"""Plugin 系统核心数据模型。

定义 Plugin 的元数据、指令 AST、执行结果、渲染模式等核心契约。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sirius_pulse.config.models import ConfigParameter


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

    COMMAND = "command"  # 用户指令触发（关键词/前缀/正则）
    EVENT_TIMER = "timer"  # 定时事件（cron/interval）
    EVENT_WEBHOOK = "webhook"  # Webhook 事件
    EVENT_ENGINE = "engine"  # 引擎生命周期事件
    EVENT_FILESYSTEM = "fs"  # 文件系统事件


class PatternType(enum.Enum):
    """指令匹配模式类型。"""

    PREFIX = "prefix"  # 前缀匹配（如 "/天气"）
    REGEX = "regex"  # 正则匹配
    KEYWORD = "keyword"  # 关键词包含匹配


# ═══════════════════════════════════════════════════════════════════════
# 指令 AST
# ═══════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class ArgNode:
    """指令参数节点。"""

    value: str | int | float | bool
    raw: str  # 原始字符串
    type_hint: str = "str"  # 来自 Plugin 参数定义的类型提示


@dataclass(slots=True)
class CommandAST:
    """Plugin 指令的抽象语法树。

    由 Lexer/Parser 从用户输入中解析生成。
    支持指令组（command group）和嵌套子命令（subcommand）。

    示例路径结构：
        /ca analyse                → command="ca", subcommand="analyse"
        /ca report daily           → command="ca", subcommand="report", subcommands=["report", "daily"]
        /tools image resize        → command="tools", subcommand="image", subcommands=["image", "resize"]
    """

    command: str  # 指令名，如 "weather" 或指令组名 "ca"
    raw_text: str  # 原始完整文本
    prefix: str = ""  # 触发前缀，如 "/"、"#"
    subcommand: str = ""  # 第一级子命令名，如 "analyse"（向后兼容）
    subcommands: list[str] = field(default_factory=list)  # 完整子命令路径，如 ["report", "daily"]
    args: list[ArgNode] = field(default_factory=list)  # 位置参数列表
    kwargs: dict[str, ArgNode] = field(default_factory=dict)  # 命名参数
    flags: set[str] = field(default_factory=set)  # 布尔开关

    @property
    def command_path(self) -> list[str]:
        """获取完整指令路径列表。

        返回 [command, subcommand1, subcommand2, ...] 格式的路径。
        """
        path = [self.command]
        if self.subcommands:
            path.extend(self.subcommands)
        elif self.subcommand:
            path.append(self.subcommand)
        return path

    @property
    def full_command(self) -> str:
        """获取完整指令路径字符串。

        返回 "command subcommand1 subcommand2 ..." 格式的路径。
        """
        return " ".join(self.command_path)

    @property
    def leaf_command(self) -> str:
        """获取叶子命令名（最终要执行的命令）。

        如果有子命令，返回最后一个子命令，否则返回 command。
        """
        if self.subcommands:
            return self.subcommands[-1]
        if self.subcommand:
            return self.subcommand
        return self.command

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
        result = {
            "command": self.command,
            "raw_text": self.raw_text,
            "prefix": self.prefix,
            "args": [{"value": a.value, "raw": a.raw, "type_hint": a.type_hint} for a in self.args],
            "kwargs": {
                k: {"value": v.value, "raw": v.raw, "type_hint": v.type_hint}
                for k, v in self.kwargs.items()
            },
            "flags": sorted(self.flags),
        }
        if self.subcommands:
            result["subcommands"] = self.subcommands
        elif self.subcommand:
            result["subcommand"] = self.subcommand
        return result


# ═══════════════════════════════════════════════════════════════════════
# Plugin 定义
# ═══════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class PluginCommandDef:
    """Plugin 指令触发器定义。"""

    name: str  # 指令名（对应 CommandAST.command）
    patterns: list[str] = field(default_factory=list)  # 触发词列表
    pattern_type: str = "prefix"  # prefix | regex | keyword
    description: str = ""
    examples: list[str] = field(default_factory=list)
    hidden_from_intent: bool = False  # 是否对意图识别隐藏（v1.3+）


@dataclass(slots=True)
class PluginCommandGroupDef:
    """Plugin 指令组定义。

    指令组用于将相关的子命令组织在一起，形成层级结构。
    例如：/ca analyse、/ca summary、/ca export
    """

    name: str  # 指令组名（如 "ca"）
    patterns: list[str] = field(default_factory=list)  # 触发词列表（不含前缀）
    pattern_type: str = "prefix"  # prefix | regex | keyword
    description: str = ""  # 指令组描述
    examples: list[str] = field(default_factory=list)  # 使用示例
    hidden_from_intent: bool = False  # 是否对意图识别隐藏


@dataclass(slots=True)
class PluginEventDef:
    """Plugin 事件触发器定义。"""

    type: str  # "timer.daily" / "webhook" / "engine.xxx"
    cron: str = ""  # cron 表达式（定时事件）
    interval_seconds: float = 0.0  # 间隔秒数（interval 事件）
    description: str = ""


@dataclass(slots=True)
class PluginParameterDef(ConfigParameter):
    """Plugin 参数定义 —— 继承 ConfigParameter，新增命令行特有字段。"""

    position: int = 0  # 位置参数序号
    choices: list[str] | None = None  # 可选值限制


@dataclass(slots=True)
class PluginPermissionDef:
    """Plugin 权限定义。

    Plugin 默认可在所有引擎活跃群使用，由主引擎白名单管控；
    group_blacklist 用于按群遮蔽特定 Plugin。
    """

    developer_only: bool = False
    hidden_from_intent: bool = False  # 是否对意图识别隐藏（v1.3+）
    adapter_types: list[str] = field(default_factory=list)
    group_blacklist: list[str] = field(default_factory=list)  # 群黑名单
    rate_limit_calls_per_minute: int = 60
    rate_limit_calls_per_hour: int = 1000


@dataclass(slots=True)
class PluginRenderDef:
    """Plugin 渲染策略定义。"""

    mode: str = "direct"  # direct | llm | silent
    system_prompt_suffix: str = ""  # llm 模式下追加的 system prompt
    max_tokens: int = 500
    temperature: float = 0.8


@dataclass(slots=True)
class PluginNaturalLangDef:
    """自然语言触发定义（用于 CognitionAnalyzer 融合识别）。"""

    examples: list[str] = field(default_factory=list)  # 示例语料，如 "帮我查一下{city}的天气"
    slots: dict[str, dict[str, Any]] = field(default_factory=dict)  # 槽位定义


@dataclass(slots=True)
class PluginDefinition:
    """Plugin 完整定义，由 plugin.json 解析生成。

    这是 Plugin 系统的核心数据契约，包含了从元数据到运行时所需的所有信息。
    """

    # ── 基本信息 ──
    name: str  # 内部标识名
    display_name: str = ""  # 显示名称
    description: str = ""
    version: str = "1.0.0"
    author: str = ""
    min_framework_version: str = "1.2.0"

    # ── 触发器 ──
    commands: list[PluginCommandDef] = field(default_factory=list)
    command_groups: list[PluginCommandGroupDef] = field(default_factory=list)  # 指令组（v1.4+）
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

    # ── 提示注入（v1.3+）──
    prompt_inject: str = ""  # 注入到人格 prompt 的额外提示词，让模型知晓插件能力

    # ── 内部字段 ──
    source_path: Path | None = None  # 插件文件夹路径
    _plugin_class: type | None = field(default=None, repr=False)  # PluginBase 子类
    user_settings: dict[str, Any] = field(default_factory=dict, repr=False)  # 运行时用户配置

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
        """从 plugin.json 字典构建 PluginDefinition（兼容旧格式）。"""
        # 解析触发器
        commands: list[PluginCommandDef] = []
        for cmd_raw in data.get("triggers", {}).get("commands", []):
            commands.append(
                PluginCommandDef(
                    name=cmd_raw.get("name", ""),
                    patterns=cmd_raw.get("patterns", []),
                    pattern_type=cmd_raw.get("pattern_type", "prefix"),
                    description=cmd_raw.get("description", ""),
                    examples=cmd_raw.get("examples", []),
                )
            )

        # 解析指令组（v1.4+）
        command_groups: list[PluginCommandGroupDef] = []
        for group_raw in data.get("triggers", {}).get("command_groups", []):
            command_groups.append(
                PluginCommandGroupDef(
                    name=group_raw.get("name", ""),
                    patterns=group_raw.get("patterns", []),
                    pattern_type=group_raw.get("pattern_type", "prefix"),
                    description=group_raw.get("description", ""),
                    examples=group_raw.get("examples", []),
                )
            )

        events: list[PluginEventDef] = []
        for evt_raw in data.get("triggers", {}).get("events", []):
            events.append(
                PluginEventDef(
                    type=evt_raw.get("type", ""),
                    cron=evt_raw.get("cron", ""),
                    interval_seconds=float(evt_raw.get("interval_seconds", 0)),
                    description=evt_raw.get("description", ""),
                )
            )

        # 解析参数
        parameters: list[PluginParameterDef] = []
        for name, param_raw in data.get("parameters", {}).items():
            parameters.append(
                PluginParameterDef(
                    name=name,
                    type=param_raw.get("type", "str"),
                    description=param_raw.get("description", ""),
                    required=param_raw.get("required", False),
                    default=param_raw.get("default"),
                    position=param_raw.get("position", 0),
                    choices=param_raw.get("choices"),
                )
            )

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
            hidden_from_intent=perm_raw.get("hidden_from_intent", False),
            adapter_types=perm_raw.get("adapter_types", []),
            group_blacklist=perm_raw.get("group_blacklist", []),
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
            command_groups=command_groups,
            events=events,
            parameters=parameters,
            natural_language=nl_def,
            permissions=permissions,
            render=render,
            dependencies=data.get("dependencies", []),
            resources=data.get("resources", []),
            prompt_inject=data.get("prompt_inject", ""),
            source_path=source_path,
        )

    @staticmethod
    def _schedule_to_events(schedule: list[dict[str, Any]]) -> list[PluginEventDef]:
        """将 _plugin_schedule 格式转换为 PluginEventDef 列表。

        _plugin_schedule 格式:
            [{"time": "08:00", "duration": 1440}, ...]

        每个条目转换为一个定时事件（timer.schedule），
        time 字段映射为 daily cron 表达式，duration 作为间隔秒数。
        """
        events: list[PluginEventDef] = []
        for entry in schedule:
            time_str = str(entry.get("time", "")).strip()
            if not time_str or ":" not in time_str:
                continue
            try:
                hour, minute = time_str.split(":", 1)
                hour = hour.strip().zfill(2)
                minute = minute.strip().zfill(2)
                # "08:00" → cron "0 8 * * *"
                cron = f"{minute} {hour} * * *"
            except (ValueError, TypeError):
                continue

            duration_minutes = int(entry.get("duration", 1440))
            events.append(
                PluginEventDef(
                    type="timer.schedule",
                    cron=cron,
                    interval_seconds=float(duration_minutes * 60),
                    description=f"每日 {time_str} 定时触发（持续 {duration_minutes} 分钟）",
                )
            )
        return events

    @classmethod
    def from_class(cls, plugin_cls: type, source_path: Path | None = None) -> PluginDefinition:
        """从 PluginBase 子类的类属性构建 PluginDefinition。

        读取子类的 _plugin_* 类属性 + @command 装饰器元数据，
        无需 plugin.json。
        """
        # 指令：从 @command 装饰器读取
        commands: list[PluginCommandDef] = []
        # 指令组：从 @command_group 装饰器读取
        command_groups: list[PluginCommandGroupDef] = []
        # 通过实例化临时对象来发现 @command（discover_commands 需要实例）
        try:
            instance = plugin_cls()
        except Exception:
            instance = object.__new__(plugin_cls)
        from sirius_pulse.plugins.decorators import discover_command_groups, discover_commands

        cmd_metas = discover_commands(instance)
        for cmd_name, meta in cmd_metas.items():
            commands.append(
                PluginCommandDef(
                    name=cmd_name,
                    patterns=meta.full_patterns,
                    pattern_type=meta.pattern_type,
                    description=meta.description,
                    examples=meta.examples,
                    hidden_from_intent=getattr(meta, "hidden_from_intent", False),
                )
            )

        # 发现指令组
        group_metas = discover_command_groups(instance)
        for group_name, (group_meta, sub_metas) in group_metas.items():
            # 将子命令添加到 commands 列表，使用 "group subcommand" 格式
            for sub_name, sub_meta in sub_metas.items():
                # 构建完整的 patterns，如 ["ca analyse", "ca analyze"]
                full_patterns = []
                for group_pattern in group_meta.full_patterns:
                    for sub_pattern in sub_meta.patterns:
                        full_patterns.append(f"{group_pattern} {sub_pattern}")

                commands.append(
                    PluginCommandDef(
                        name=group_name,  # 指令组名
                        patterns=full_patterns,
                        pattern_type=group_meta.pattern_type,
                        description=sub_meta.description or group_meta.description,
                        examples=sub_meta.examples or group_meta.examples,
                        hidden_from_intent=getattr(sub_meta, "hidden_from_intent", False)
                        or getattr(group_meta, "hidden_from_intent", False),
                    )
                )

            command_groups.append(
                PluginCommandGroupDef(
                    name=group_name,
                    patterns=group_meta.full_patterns,
                    pattern_type=group_meta.pattern_type,
                    description=group_meta.description,
                    examples=group_meta.examples,
                )
            )

        # 事件：合并 _plugin_events 和 _plugin_schedule
        events: list[PluginEventDef] = []
        for evt_raw in getattr(plugin_cls, "_plugin_events", []) or []:
            events.append(
                PluginEventDef(
                    type=evt_raw.get("type", ""),
                    cron=evt_raw.get("cron", ""),
                    interval_seconds=float(evt_raw.get("interval_seconds", 0)),
                    description=evt_raw.get("description", ""),
                )
            )
        # _plugin_schedule 是 _plugin_events 的声明式简写（v1.3+）
        schedule_raw = getattr(plugin_cls, "_plugin_schedule", []) or []
        if schedule_raw:
            events.extend(PluginDefinition._schedule_to_events(schedule_raw))

        # 自然语言触发
        nl_examples = getattr(plugin_cls, "_plugin_nl_examples", []) or []
        nl_slots = getattr(plugin_cls, "_plugin_nl_slots", {}) or {}
        nl_def: PluginNaturalLangDef | None = None
        if nl_examples or nl_slots:
            nl_def = PluginNaturalLangDef(examples=list(nl_examples), slots=dict(nl_slots))

        # 参数：从 _plugin_parameters 类属性读取（优先于 NL slots 构建）
        parameters: list[PluginParameterDef] = []
        params_from_class = getattr(plugin_cls, "_plugin_parameters", None) or []
        if params_from_class:
            for i, p in enumerate(params_from_class):
                parameters.append(
                    PluginParameterDef(
                        name=p.get("name", ""),
                        type=p.get("type", "str"),
                        description=p.get("description", ""),
                        required=p.get("required", False),
                        default=p.get("default"),
                        position=p.get("position", i),
                        choices=p.get("choices"),
                        group=p.get("group", ""),
                    )
                )
        elif nl_slots:
            for i, (slot_name, slot_info) in enumerate(nl_slots.items()):
                parameters.append(
                    PluginParameterDef(
                        name=slot_name,
                        type=slot_info.get("type", "str"),
                        description=slot_info.get("description", ""),
                        required=slot_info.get("required", True),
                        default=slot_info.get("default"),
                        position=i,
                    )
                )

        # 权限
        perm_raw = getattr(plugin_cls, "_plugin_permissions", None) or {}
        permissions = PluginPermissionDef(
            developer_only=perm_raw.get("developer_only", False),
            hidden_from_intent=perm_raw.get("hidden_from_intent", False),
            adapter_types=perm_raw.get("adapter_types", []),
            group_blacklist=perm_raw.get("group_blacklist", []),
            rate_limit_calls_per_minute=perm_raw.get("rate_limit", {}).get("calls_per_minute", 60),
            rate_limit_calls_per_hour=perm_raw.get("rate_limit", {}).get("calls_per_hour", 1000),
        )

        return PluginDefinition(
            name=getattr(plugin_cls, "_plugin_name", "") or plugin_cls.__name__,
            display_name=getattr(plugin_cls, "_plugin_display_name", "") or "",
            description=getattr(plugin_cls, "_plugin_description", "") or "",
            version=getattr(plugin_cls, "_plugin_version", "") or "1.0.0",
            author=getattr(plugin_cls, "_plugin_author", "") or "",
            commands=commands,
            command_groups=command_groups,
            events=events,
            parameters=parameters,
            natural_language=nl_def,
            permissions=permissions,
            dependencies=getattr(plugin_cls, "_plugin_dependencies", []) or [],
            prompt_inject=getattr(plugin_cls, "_plugin_prompt_inject", "") or "",
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
# Plugin 响应 —— handler 返回给框架的输出契约
# ═══════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class PluginResponse:
    """Plugin 处理器返回给框架的响应。

    这是 handler 与框架之间的核心输出契约。根据 render_mode：
        - direct: text 直接作为最终回复发送给用户
        - llm: data 委托给人格引擎做风格化生成
        - silent: 无输出，仅执行副作用

    Plugin 也可以通过 ctx.adapter.send_xxx() 直接调用平台 API，
    此时仍应返回 PluginResponse 告知框架指令已处理完毕。
    """

    success: bool = True
    data: Any = None  # 结构化数据（llm 模式下用于人格化生成）
    text: str = ""  # 纯文本输出（direct 模式下直接发送）
    error: str = ""  # 错误信息
    render_mode: str = ""  # 覆盖 plugin.json / @command 中的 render.mode
    mood_hint: str = ""  # 情绪提示（用于 llm 风格化）
    tone_override: str = ""  # 语气覆写
    image_urls: list[str] = field(default_factory=list)
    message_group: Any = None  # MessageGroup | None（多模态输出：图片/语音/文件等）
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def ok(text: str = "", data: Any = None, **kwargs: Any) -> PluginResponse:
        """快捷构造成功的 PluginResponse。"""
        return PluginResponse(success=True, text=text, data=data, **kwargs)

    @staticmethod
    def fail(error: str) -> PluginResponse:
        """快捷构造失败的 PluginResponse。"""
        return PluginResponse(success=False, error=error)


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
