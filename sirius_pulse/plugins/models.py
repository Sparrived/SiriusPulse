"""Plugin 系统核心数据模型。

定义 Plugin 的元数据、指令 AST、执行结果、渲染模式等核心契约。
"""

from __future__ import annotations

import enum
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

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
        /ca report daily           → command="ca", subcommand="report",
                                     subcommands=["report", "daily"]
        /tools image resize        → command="tools", subcommand="image",
                                     subcommands=["image", "resize"]
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


def _validate_command_patterns(patterns: object) -> None:
    """Reject command pattern collections that could match empty input."""
    if not isinstance(patterns, list):
        raise ValueError("Plugin 指令 patterns 必须是字符串列表")
    if any(not isinstance(pattern, str) for pattern in patterns):
        raise ValueError("Plugin 指令 patterns 必须是字符串列表")
    if any(not pattern.strip() for pattern in patterns):
        raise ValueError("Plugin 指令 pattern 不能为空或仅包含空白")


@dataclass(slots=True)
class PluginCommandDef:
    """Plugin 指令触发器定义。"""

    name: str  # 指令名（对应 CommandAST.command）
    patterns: list[str] = field(default_factory=list)  # 触发词列表
    pattern_type: str = "prefix"  # prefix | regex | keyword
    description: str = ""
    examples: list[str] = field(default_factory=list)
    hidden_from_intent: bool = False  # 是否对意图识别隐藏（v1.3+）

    def __post_init__(self) -> None:
        """Validate trigger patterns at metadata construction time."""
        _validate_command_patterns(self.patterns)


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

    def __post_init__(self) -> None:
        """Validate trigger patterns at metadata construction time."""
        _validate_command_patterns(self.patterns)


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


_UI_SCHEMA_UNSAFE_NAMES = {"__proto__", "prototype", "constructor"}
_PARAMETER_CONTRACT_MAX_BYTES = 256 * 1024
_PARAMETER_CONTRACT_MAX_DEPTH = 16
_PARAMETER_CONTRACT_MAX_NODES = 4096
_PARAMETER_CONTRACT_MAX_CONTAINER_ITEMS = 256
_PARAMETER_CONTRACT_KEYS = (
    "name",
    "type",
    "description",
    "required",
    "default",
    "choices",
    "fields",
    "minimum",
    "maximum",
    "group",
    "position",
)
_UI_SCHEMA_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_UI_SCHEMA_MAX_BYTES = 64 * 1024
_UI_SCHEMA_MAX_SECTIONS = 16
_UI_SCHEMA_MAX_PARAMETERS = 128
_UI_SCHEMA_WIDGET_TYPES = {"text", "url", "path", "code", "textarea", "switch"}
_UI_SCHEMA_SECRET_NAMES = {
    "password",
    "passwords",
    "secret",
    "secrets",
    "token",
    "tokens",
    "key",
    "keys",
    "api_key",
    "api_keys",
    "access_token",
    "refresh_token",
    "authorization",
    "auth",
    "authentication",
    "bearer",
    "client_secret",
    "credential",
    "credentials",
    "session",
    "session_id",
}
_UI_SCHEMA_SECRET_SUFFIXES = (
    "_token",
    "_tokens",
    "_key",
    "_keys",
    "_secret",
    "_secrets",
    "_password",
    "_passwords",
    "_credential",
    "_credentials",
    "_auth",
    "_session",
)
_UI_SCHEMA_TOP_KEYS = {"version", "layout", "title", "description", "sections", "parameters"}
_UI_SCHEMA_SECTION_KEYS = {
    "id",
    "title",
    "description",
    "parameters",
    "columns",
    "collapsed",
    "tone",
}
_UI_SCHEMA_FIELDSET_KEYS = {"id", "title", "description", "fields", "collapsed"}
_UI_SCHEMA_BASE_PRESENTATION_KEYS = {"label", "help", "span"}
_UI_SCHEMA_OBJECT_ARRAY_KEYS = {
    "add_label",
    "item_placeholder",
    "empty_title",
    "empty_description",
    "item_title_field",
    "item_fallback_field",
    "item_subtitle_field",
    "item_badge_field",
    "item_status_field",
    "fields",
    "fieldsets",
}
_UI_SCHEMA_TEXT_LIMITS = {
    "title": 120,
    "description": 600,
    "label": 120,
    "help": 600,
    "placeholder": 240,
    "unit": 32,
    "true_label": 64,
    "false_label": 64,
    "add_label": 120,
    "item_placeholder": 160,
    "empty_title": 120,
    "empty_description": 600,
}


def _validate_bounded_json_tree(
    raw: Any,
    *,
    label: str,
    max_depth: int,
    max_nodes: int,
    max_container_items: int,
    max_bytes: int,
) -> bytes:
    """Validate and encode a finite, bounded JSON tree without recursive descent."""
    stack = [(raw, 0)]
    seen_containers: set[int] = set()
    nodes = 0
    estimated_bytes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise ValueError(f"{label} 节点数量超过安全上限")
        if depth > max_depth:
            raise ValueError(f"{label} 嵌套过深")
        if value is None:
            estimated_bytes += 4
        elif type(value) is bool:
            estimated_bytes += 5
        elif type(value) is int:
            estimated_bytes += len(str(value))
        elif type(value) is float:
            if not math.isfinite(value):
                raise ValueError(f"{label} 不能包含非有限数字")
            estimated_bytes += len(repr(value))
        elif type(value) is str:
            estimated_bytes += len(value.encode("utf-8")) + 2
        elif type(value) in {dict, list}:
            value_id = id(value)
            if value_id in seen_containers:
                raise ValueError(f"{label} 不能包含循环或共享容器引用")
            seen_containers.add(value_id)
            if len(value) > max_container_items:
                raise ValueError(f"{label} 单个容器项目过多")
            estimated_bytes += 2 + len(value)
            if type(value) is dict:
                for key, child in value.items():
                    if type(key) is not str:
                        raise ValueError(f"{label} 对象字段名必须是字符串")
                    estimated_bytes += len(key.encode("utf-8")) + 3
                    stack.append((child, depth + 1))
            else:
                stack.extend((child, depth + 1) for child in value)
        else:
            raise ValueError(f"{label} 只能包含 JSON 数据")
        if estimated_bytes > max_bytes:
            raise ValueError(f"{label} 超过安全大小上限")
    try:
        encoded = json.dumps(
            raw,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} 必须是有限 JSON 数据") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} 超过安全大小上限")
    return encoded


def plugin_parameter_contract_digest(parameters: Any) -> str:
    """Return a deterministic integrity digest for the complete parameter contract."""
    if type(parameters) is not list or len(parameters) > _UI_SCHEMA_MAX_PARAMETERS:
        raise ValueError("Plugin 参数契约必须是最多 128 项的列表")
    payload: list[dict[str, Any]] = []
    for parameter in parameters:
        if not isinstance(parameter, PluginParameterDef):
            raise ValueError("Plugin 参数契约包含无效参数对象")
        payload.append({key: getattr(parameter, key) for key in _PARAMETER_CONTRACT_KEYS})
    encoded = _validate_bounded_json_tree(
        payload,
        label="Plugin 参数契约",
        max_depth=_PARAMETER_CONTRACT_MAX_DEPTH,
        max_nodes=_PARAMETER_CONTRACT_MAX_NODES,
        max_container_items=_PARAMETER_CONTRACT_MAX_CONTAINER_ITEMS,
        max_bytes=_PARAMETER_CONTRACT_MAX_BYTES,
    )
    return hashlib.sha256(encoded).hexdigest()


_UI_SCHEMA_INLINE_SECRET_RE = re.compile(
    r"(?i)(?:authorization\s*:\s*\S+|bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"(?:password|passphrase|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|credential)\s*[=:]\s*\S+)"
)


def _ui_schema_text_contains_secret(value: str) -> bool:
    """Reject credentials embedded in otherwise harmless presentation text."""
    if _UI_SCHEMA_INLINE_SECRET_RE.search(value):
        return True
    if not any(marker in value for marker in ("://", "//", "?", "#")):
        return False
    authority_starts = [match.end() for match in re.finditer(r"://", value)]
    if value.startswith("//"):
        authority_starts.append(2)
    if any(
        "@" in (authority := re.split(r"[/\\?#]", value[start:], maxsplit=1)[0])
        and bool(authority.rsplit("@", 1)[0])
        for start in authority_starts
    ):
        return True
    components: list[str] = []
    if "?" in value:
        query_and_fragment = value.split("?", 1)[1]
        query, separator, fragment = query_and_fragment.partition("#")
        components.append(query)
        if separator:
            components.append(fragment)
    elif "#" in value:
        components.append(value.split("#", 1)[1])
    if any(
        _ui_schema_is_secret(key, "str") and bool(query_value)
        for component in components
        for key, query_value in parse_qsl(component, keep_blank_values=True)
    ):
        return True
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.username is not None or parsed.password is not None


def _ui_schema_text(value: Any, *, path: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Plugin ui_schema {path} 必须是字符串")
    if (
        len(value) > limit
        or any(ord(char) < 32 and char not in "\n\t" for char in value)
        or "<" in value
        or ">" in value
        or "${" in value
        or "{{" in value
        or "}}" in value
        or _ui_schema_text_contains_secret(value)
    ):
        raise ValueError(f"Plugin ui_schema {path} 超出安全文本限制")
    return value


def _ui_schema_fields(parameter: Any) -> dict[str, dict[str, Any]]:
    raw_fields = getattr(parameter, "fields", None)
    if not isinstance(raw_fields, list):
        return {}
    if len(raw_fields) > _UI_SCHEMA_MAX_PARAMETERS:
        raise ValueError("Plugin ui_schema 所引用的子字段数量超过安全上限")
    result: dict[str, dict[str, Any]] = {}
    for raw_field in raw_fields:
        if not isinstance(raw_field, dict):
            continue
        name = raw_field.get("name")
        if (
            not isinstance(name, str)
            or not name
            or name in _UI_SCHEMA_UNSAFE_NAMES
            or name in result
        ):
            raise ValueError("Plugin ui_schema 所引用的子字段声明存在歧义")
        result[name] = raw_field
    return result


def _ui_schema_is_secret(name: Any, parameter_type: str) -> bool:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized.casefold()).strip("_")
    return (
        parameter_type in {"password", "secret"}
        or normalized in _UI_SCHEMA_SECRET_NAMES
        or any(normalized.endswith(suffix) for suffix in _UI_SCHEMA_SECRET_SUFFIXES)
    )


def _ui_schema_presentation_keys(
    parameter_type: str,
    *,
    object_array: bool,
    secret: bool,
) -> set[str]:
    allowed = set(_UI_SCHEMA_BASE_PRESENTATION_KEYS)
    if secret:
        return allowed
    if parameter_type in {"str", "string"}:
        allowed.update({"placeholder", "widget"})
    elif parameter_type in {"int", "float", "number"}:
        allowed.add("unit")
    elif parameter_type in {"bool", "boolean"}:
        allowed.update({"widget", "true_label", "false_label"})
    elif parameter_type in {"list", "array"}:
        allowed.update({"add_label", "item_placeholder"})
    elif parameter_type == "object_array" and object_array:
        allowed.update(_UI_SCHEMA_OBJECT_ARRAY_KEYS)
    return allowed


def _normalize_ui_field_presentation(
    raw: Any,
    *,
    parameter_type: str,
    path: str,
    object_array: bool = False,
    secret: bool = False,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"Plugin ui_schema {path} 必须是对象")
    allowed = _ui_schema_presentation_keys(
        parameter_type,
        object_array=object_array,
        secret=secret,
    )
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"Plugin ui_schema {path} 包含不支持字段：{sorted(unknown)[0]}")
    result: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _UI_SCHEMA_TEXT_LIMITS:
            result[key] = _ui_schema_text(
                value,
                path=f"{path}.{key}",
                limit=_UI_SCHEMA_TEXT_LIMITS[key],
            )
    if "span" in raw:
        span = raw["span"]
        if type(span) is not int or not 1 <= span <= 12:
            raise ValueError(f"Plugin ui_schema {path}.span 必须是 1 到 12 的整数")
        result["span"] = span
    if "widget" in raw:
        widget = raw["widget"]
        compatible = (widget == "switch" and parameter_type in {"bool", "boolean"}) or (
            widget != "switch"
            and widget in _UI_SCHEMA_WIDGET_TYPES
            and parameter_type in {"str", "string"}
        )
        if not compatible:
            raise ValueError(f"Plugin ui_schema {path}.widget 与参数类型不兼容")
        result["widget"] = widget
    return result


def normalize_plugin_ui_schema(
    raw: Any,
    parameters: list[PluginParameterDef],
) -> dict[str, Any]:
    """Return a bounded presentation-only Plugin UI schema.

    The schema may change labels and layout, but it cannot redefine parameter
    types, defaults, requirements, identities, choices, or secret semantics.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("Plugin ui_schema 必须是对象")
    if not raw:
        return {}
    encoded = _validate_bounded_json_tree(
        raw,
        label="Plugin ui_schema",
        max_depth=8,
        max_nodes=1024,
        max_container_items=_UI_SCHEMA_MAX_PARAMETERS,
        max_bytes=_UI_SCHEMA_MAX_BYTES,
    )
    schema = json.loads(encoded)
    if any(
        depth > 8
        or (
            isinstance(node, dict)
            and any(not isinstance(key, str) or key in _UI_SCHEMA_UNSAFE_NAMES for key in node)
        )
        for node, depth in _walk_ui_schema(schema)
    ):
        raise ValueError("Plugin ui_schema 包含不安全字段名称或嵌套过深")
    unknown_top = set(schema) - _UI_SCHEMA_TOP_KEYS
    if unknown_top:
        raise ValueError(f"Plugin ui_schema 包含不支持字段：{sorted(unknown_top)[0]}")
    version = schema.get("version")
    if type(version) is not int or version != 1:
        raise ValueError("Plugin ui_schema.version 目前只支持 1")
    layout = schema.get("layout", "standard")
    if layout not in {"standard", "wide"}:
        raise ValueError("Plugin ui_schema.layout 必须是 standard 或 wide")

    parameter_names = [parameter.name for parameter in parameters]
    if (
        len(parameters) > _UI_SCHEMA_MAX_PARAMETERS
        or len(parameter_names) != len(set(parameter_names))
        or any(
            not isinstance(name, str) or not name or name in _UI_SCHEMA_UNSAFE_NAMES
            for name in parameter_names
        )
    ):
        raise ValueError("Plugin ui_schema 参数声明存在歧义或超过安全上限")
    parameter_map = {parameter.name: parameter for parameter in parameters}
    result: dict[str, Any] = {"version": 1, "layout": layout}
    for key in ("title", "description"):
        if key in schema:
            result[key] = _ui_schema_text(
                schema[key],
                path=key,
                limit=_UI_SCHEMA_TEXT_LIMITS[key],
            )

    raw_sections = schema.get("sections", [])
    if not isinstance(raw_sections, list) or len(raw_sections) > _UI_SCHEMA_MAX_SECTIONS:
        raise ValueError("Plugin ui_schema.sections 必须是最多 16 项的列表")
    section_ids: set[str] = set()
    section_parameters: set[str] = set()
    sections: list[dict[str, Any]] = []
    for index, raw_section in enumerate(raw_sections):
        path = f"sections[{index}]"
        if not isinstance(raw_section, dict):
            raise ValueError(f"Plugin ui_schema {path} 必须是对象")
        unknown = set(raw_section) - _UI_SCHEMA_SECTION_KEYS
        if unknown:
            raise ValueError(f"Plugin ui_schema {path} 包含不支持字段：{sorted(unknown)[0]}")
        section_id = raw_section.get("id")
        if not isinstance(section_id, str) or not _UI_SCHEMA_ID_RE.fullmatch(section_id):
            raise ValueError(f"Plugin ui_schema {path}.id 无效")
        if section_id in section_ids:
            raise ValueError(f"Plugin ui_schema 包含重复分区 ID：{section_id}")
        section_ids.add(section_id)
        references = raw_section.get("parameters", [])
        if not isinstance(references, list) or any(
            not isinstance(item, str) for item in references
        ):
            raise ValueError(f"Plugin ui_schema {path}.parameters 必须是字符串列表")
        if len(references) != len(set(references)):
            raise ValueError(f"Plugin ui_schema {path}.parameters 包含重复参数")
        for reference in references:
            if reference not in parameter_map:
                raise ValueError(f"Plugin ui_schema 引用了未声明参数：{reference}")
            if reference in section_parameters:
                raise ValueError(f"Plugin ui_schema 参数重复出现在分区中：{reference}")
            section_parameters.add(reference)
        columns = raw_section.get("columns", 1)
        if type(columns) is not int or columns not in {1, 2}:
            raise ValueError(f"Plugin ui_schema {path}.columns 必须是 1 或 2")
        collapsed = raw_section.get("collapsed", False)
        if type(collapsed) is not bool:
            raise ValueError(f"Plugin ui_schema {path}.collapsed 必须是布尔值")
        tone = raw_section.get("tone", "default")
        if tone not in {"default", "accent", "muted"}:
            raise ValueError(f"Plugin ui_schema {path}.tone 无效")
        section: dict[str, Any] = {
            "id": section_id,
            "parameters": list(references),
            "columns": columns,
            "collapsed": collapsed,
            "tone": tone,
        }
        for key in ("title", "description"):
            if key in raw_section:
                section[key] = _ui_schema_text(
                    raw_section[key],
                    path=f"{path}.{key}",
                    limit=_UI_SCHEMA_TEXT_LIMITS[key],
                )
        sections.append(section)
    result["sections"] = sections

    raw_presentations = schema.get("parameters", {})
    if (
        not isinstance(raw_presentations, dict)
        or len(raw_presentations) > _UI_SCHEMA_MAX_PARAMETERS
    ):
        raise ValueError("Plugin ui_schema.parameters 必须是有界对象")
    presentations: dict[str, Any] = {}
    for parameter_name, raw_presentation in raw_presentations.items():
        parameter = parameter_map.get(parameter_name)
        if parameter is None:
            raise ValueError(f"Plugin ui_schema 引用了未声明参数：{parameter_name}")
        parameter_type = str(parameter.type or "str").casefold()
        path = f"parameters.{parameter_name}"
        presentation = _normalize_ui_field_presentation(
            raw_presentation,
            parameter_type=parameter_type,
            path=path,
            object_array=parameter_type == "object_array",
            secret=_ui_schema_is_secret(parameter_name, parameter_type),
        )
        if parameter_type == "object_array":
            field_map = _ui_schema_fields(parameter)
            scalar_types = {"str", "string", "int", "float", "number", "bool", "boolean"}
            for key in (
                "item_title_field",
                "item_fallback_field",
                "item_subtitle_field",
                "item_badge_field",
                "item_status_field",
            ):
                if key not in raw_presentation:
                    continue
                reference = raw_presentation[key]
                field = field_map.get(reference) if isinstance(reference, str) else None
                field_type = str(field.get("type", "str")).casefold() if field else ""
                if (
                    field is None
                    or field_type not in scalar_types
                    or _ui_schema_is_secret(reference, field_type)
                ):
                    raise ValueError(f"Plugin ui_schema {path}.{key} 引用了无效子字段")
                if key == "item_status_field" and field_type not in {"bool", "boolean"}:
                    raise ValueError(f"Plugin ui_schema {path}.{key} 必须引用 bool 子字段")
                presentation[key] = reference
            raw_fields = raw_presentation.get("fields", {})
            if not isinstance(raw_fields, dict):
                raise ValueError(f"Plugin ui_schema {path}.fields 必须是对象")
            field_presentations: dict[str, Any] = {}
            for field_name, raw_field_ui in raw_fields.items():
                field = field_map.get(field_name)
                if field is None:
                    raise ValueError(f"Plugin ui_schema {path}.fields 引用了未知子字段：{field_name}")
                field_presentations[field_name] = _normalize_ui_field_presentation(
                    raw_field_ui,
                    parameter_type=str(field.get("type", "str")).casefold(),
                    path=f"{path}.fields.{field_name}",
                    secret=_ui_schema_is_secret(
                        field_name,
                        str(field.get("type", "str")).casefold(),
                    ),
                )
            presentation["fields"] = field_presentations
            raw_fieldsets = raw_presentation.get("fieldsets", [])
            if not isinstance(raw_fieldsets, list) or len(raw_fieldsets) > 16:
                raise ValueError(f"Plugin ui_schema {path}.fieldsets 必须是最多 16 项的列表")
            fieldset_ids: set[str] = set()
            assigned_fields: set[str] = set()
            fieldsets: list[dict[str, Any]] = []
            for index, raw_fieldset in enumerate(raw_fieldsets):
                fieldset_path = f"{path}.fieldsets[{index}]"
                if not isinstance(raw_fieldset, dict):
                    raise ValueError(f"Plugin ui_schema {fieldset_path} 必须是对象")
                unknown = set(raw_fieldset) - _UI_SCHEMA_FIELDSET_KEYS
                if unknown:
                    raise ValueError(
                        f"Plugin ui_schema {fieldset_path} 包含不支持字段：{sorted(unknown)[0]}"
                    )
                fieldset_id = raw_fieldset.get("id")
                if not isinstance(fieldset_id, str) or not _UI_SCHEMA_ID_RE.fullmatch(fieldset_id):
                    raise ValueError(f"Plugin ui_schema {fieldset_path}.id 无效")
                if fieldset_id in fieldset_ids:
                    raise ValueError(f"Plugin ui_schema 包含重复字段组 ID：{fieldset_id}")
                fieldset_ids.add(fieldset_id)
                references = raw_fieldset.get("fields", [])
                if not isinstance(references, list) or any(
                    not isinstance(item, str) for item in references
                ):
                    raise ValueError(f"Plugin ui_schema {fieldset_path}.fields 必须是字符串列表")
                if len(references) != len(set(references)):
                    raise ValueError(f"Plugin ui_schema {fieldset_path}.fields 包含重复字段")
                for reference in references:
                    if reference not in field_map:
                        raise ValueError(f"Plugin ui_schema 引用了未知子字段：{reference}")
                    if reference in assigned_fields:
                        raise ValueError(f"Plugin ui_schema 子字段重复出现在字段组中：{reference}")
                    assigned_fields.add(reference)
                collapsed = raw_fieldset.get("collapsed", False)
                if type(collapsed) is not bool:
                    raise ValueError(f"Plugin ui_schema {fieldset_path}.collapsed 必须是布尔值")
                fieldset: dict[str, Any] = {
                    "id": fieldset_id,
                    "fields": list(references),
                    "collapsed": collapsed,
                }
                for key in ("title", "description"):
                    if key in raw_fieldset:
                        fieldset[key] = _ui_schema_text(
                            raw_fieldset[key],
                            path=f"{fieldset_path}.{key}",
                            limit=_UI_SCHEMA_TEXT_LIMITS[key],
                        )
                fieldsets.append(fieldset)
            presentation["fieldsets"] = fieldsets
        presentations[parameter_name] = presentation
    result["parameters"] = presentations
    return result


def _walk_ui_schema(value: Any) -> list[tuple[Any, int]]:
    nodes = [(value, 0)]
    for node, depth in nodes:
        if isinstance(node, dict):
            nodes.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            nodes.extend((child, depth + 1) for child in node)
    return nodes


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
    """Plugin 渲染策略定义。

    采样参数（``max_tokens`` / ``temperature``）不在此处：它们由 AMKR 的任务
    定义持有。
    """

    mode: str = "direct"  # direct | llm | silent
    system_prompt_suffix: str = ""  # llm 模式下追加的 system prompt


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
    # Class/manifest authors can declare a stricter minimum; PluginLoader
    # validates it before importing executable Plugin code.
    min_framework_version: str = "1.2.0"

    # ── 触发器 ──
    commands: list[PluginCommandDef] = field(default_factory=list)
    command_groups: list[PluginCommandGroupDef] = field(default_factory=list)  # 指令组（v1.4+）
    events: list[PluginEventDef] = field(default_factory=list)

    # ── 参数 ──
    parameters: list[PluginParameterDef] = field(default_factory=list)
    ui_schema: dict[str, Any] = field(default_factory=dict, kw_only=True)
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
    _parameter_contract_digest: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        """Seal the data contract and fail closed for invalid presentation metadata."""
        self.seal_parameter_contract()
        try:
            self.ui_schema = normalize_plugin_ui_schema(self.ui_schema, self.parameters)
        except (TypeError, ValueError, OverflowError, RecursionError):
            # A broken optional presentation declaration must not make an
            # otherwise usable plugin disappear.  The WebUI will use its
            # generic parameter form instead.
            self.ui_schema = {}

    def seal_parameter_contract(self) -> None:
        """Record the canonical parameter contract after a trusted loader merge."""
        self._parameter_contract_digest = plugin_parameter_contract_digest(self.parameters)

    def parameter_contract_is_intact(self) -> bool:
        """Return whether cached parameter metadata still matches its sealed contract."""
        try:
            current = plugin_parameter_contract_digest(self.parameters)
        except (TypeError, ValueError, OverflowError):
            return False
        return bool(self._parameter_contract_digest) and current == self._parameter_contract_digest

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
                    hidden_from_intent=cmd_raw.get("hidden_from_intent", False),
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
                    fields=param_raw.get("fields"),
                    minimum=param_raw.get("minimum", param_raw.get("min")),
                    maximum=param_raw.get("maximum", param_raw.get("max")),
                    group=param_raw.get("group", ""),
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
            ui_schema=data.get("ui_schema", {}),
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
                        fields=p.get("fields"),
                        minimum=p.get("minimum", p.get("min")),
                        maximum=p.get("maximum", p.get("max")),
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
            min_framework_version=(
                getattr(plugin_cls, "_plugin_min_framework_version", "") or "1.2.0"
            ),
            commands=commands,
            command_groups=command_groups,
            events=events,
            parameters=parameters,
            ui_schema=getattr(plugin_cls, "_plugin_ui_schema", {}) or {},
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
