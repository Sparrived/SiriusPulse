"""Plugin 装饰器系统 —— @command 声明式指令注册。

提供比覆写 execute() 更具可扩展性的指令定义方式：
    - 每个 @command 装饰一个方法，对应一个 Plugin 指令
    - 方法参数的类型注解自动用于参数类型校验
    - 方法参数的默认值自动成为可选参数的缺省值
    - 框架自动从 CommandAST 提取参数并注入

使用示例：

    from sirius_chat.plugins import PluginBase, command, PluginResult

    class WeatherPlugin(PluginBase):
        @command("weather", patterns=["/天气", "查天气"], render_mode="llm",
                 description="查询城市天气")
        async def query_weather(self, city: str, unit: str = "celsius") -> PluginResult:
            data = await self._fetch_weather(city, unit)
            return PluginResult.ok(data=data, mood_hint="温暖关心")

        @command("forecast", patterns=["/预报"], render_mode="direct")
        def forecast(self, city: str, days: int = 3) -> PluginResult:
            result = self._get_forecast(city, days)
            return PluginResult.ok(text=result)

    # 也支持不覆写 execute()，框架自动按 cmd.command 路由
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar, overload, TYPE_CHECKING

if TYPE_CHECKING:
    from sirius_chat.plugins.models import CommandAST, PluginResult

logger = logging.getLogger(__name__)

# ── 泛型别名 ──
F = TypeVar("F", bound=Callable[..., Any])


# ═══════════════════════════════════════════════════════════════════════
# PluginCommandMeta —— 装饰器记录的指令元数据
# ═══════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class PluginCommandMeta:
    """由 @command 装饰器记录的指令元数据。

    包含路由信息（patterns）、渲染策略（render_mode）、
    以及方法引用（handler）。
    """

    name: str                              # 指令名（对应 CommandAST.command）
    patterns: list[str] = field(default_factory=list)          # 触发词列表
    pattern_type: str = "prefix"           # prefix | keyword | regex
    render_mode: str = "direct"            # direct | llm | silent
    description: str = ""                  # 人类可读的描述
    examples: list[str] = field(default_factory=list)          # 使用示例
    # LLM 渲染参数
    system_prompt_suffix: str = ""
    max_tokens: int = 500
    temperature: float = 0.8
    mood_hint: str = ""
    # 内部引用
    handler: Callable[..., Any] | None = None   # 绑定的方法
    handler_is_async: bool = False              # 是否为异步方法

    @property
    def is_async(self) -> bool:
        """是否为异步 handler。"""
        return self.handler_is_async


# ═══════════════════════════════════════════════════════════════════════
# @command 装饰器
# ═══════════════════════════════════════════════════════════════════════

def command(
    name: str,
    *,
    patterns: list[str] | None = None,
    pattern_type: str = "prefix",
    render_mode: str = "direct",
    description: str = "",
    examples: list[str] | None = None,
    system_prompt_suffix: str = "",
    max_tokens: int = 500,
    temperature: float = 0.8,
    mood_hint: str = "",
) -> Callable[[F], F]:
    """声明式指令注册装饰器。

    将 PluginBase 子类的方法注册为一个 Plugin 指令处理器。

    Args:
        name: 指令名（如 "weather"），映射到 CommandAST.command
        patterns: 触发词列表（如 ["/天气", "查天气"]）
        pattern_type: 匹配模式类型 ("prefix" | "keyword" | "regex")
        render_mode: 输出策略 ("direct" | "llm" | "silent")
        description: 指令描述文本
        examples: 使用示例列表
        system_prompt_suffix: LLM 模式下追加到 system prompt 的文本
        max_tokens: LLM 模式最大 token 数
        temperature: LLM 模式生成温度
        mood_hint: 情绪提示文本

    Returns:
        装饰后的方法（不改变方法本身，仅添加 _plugin_command_meta 属性）

    示例:

        class MyPlugin(PluginBase):
            @command("roll", patterns=["#roll", "/roll"], render_mode="direct")
            def do_roll(self, expression: str) -> PluginResult:
                result = roll_dice(expression)
                return PluginResult.ok(text=result)

    ── 类型映射规则 ──
    框架根据方法的类型注解和默认值自动进行参数校验与注入：

    | 注解类型    | 对应 CommandAST 参数 | 行为                       |
    |------------|---------------------|---------------------------|
    | str        | kwargs["name"]      | 原样传递（默认）             |
    | int        | kwargs["name"]      | int(value) 转换             |
    | float      | kwargs["name"]      | float(value) 转换           |
    | bool       | kwargs["name"]      | value.lower() in true/1/yes|
    | list[str]  | kwargs["name"]      | 原样传递                    |

    方法参数名自动映射到 CommandAST.kwargs 的键名。
    带有默认值的参数是可选的，缺少时使用默认值。
    """
    meta = PluginCommandMeta(
        name=name,
        patterns=patterns or [name],
        pattern_type=pattern_type,
        render_mode=render_mode,
        description=description,
        examples=examples or [],
        system_prompt_suffix=system_prompt_suffix,
        max_tokens=max_tokens,
        temperature=temperature,
        mood_hint=mood_hint,
    )

    def decorator(func: F) -> F:
        # 将元数据附着到函数对象上
        setattr(func, "_plugin_command_meta", meta)
        # 记录是否为异步函数
        meta.handler_is_async = asyncio.iscoroutinefunction(func)
        return func

    return decorator


def _get_command_meta(func: Callable[..., Any]) -> PluginCommandMeta | None:
    """从函数对象上读取 @command 装饰器附着元数据。"""
    return getattr(func, "_plugin_command_meta", None)


# ═══════════════════════════════════════════════════════════════════════
# _discover_commands —— 扫描 PluginBase 子类的 @command 方法
# ═══════════════════════════════════════════════════════════════════════

def discover_commands(instance: object) -> dict[str, PluginCommandMeta]:
    """扫描 PluginBase 实例上所有带 @command 装饰器的方法。

    遍历 instance 的类及其所有父类（在 PluginBase 之前停止），
    收集所有被 @command 装饰过的方法。

    Args:
        instance: PluginBase 子类实例

    Returns:
        {command_name: PluginCommandMeta} 字典
    """
    from sirius_chat.plugins.base import PluginBase

    handlers: dict[str, PluginCommandMeta] = {}
    # 遍历 MRO，在 PluginBase 之前停止（不扫描基类的 execute 等）
    for cls in type(instance).__mro__:
        if cls is PluginBase:
            break
        for attr_name in dir(cls):
            # 跳过内置和私有属性
            if attr_name.startswith("_"):
                continue
            try:
                attr = getattr(cls, attr_name, None)
            except Exception:
                continue
            if not callable(attr):
                continue
            meta = _get_command_meta(attr)
            if meta is None:
                continue
            # 将 handler 绑定到实例
            bound = getattr(instance, attr_name)
            meta = PluginCommandMeta(
                name=meta.name,
                patterns=list(meta.patterns),
                pattern_type=meta.pattern_type,
                render_mode=meta.render_mode,
                description=meta.description,
                examples=list(meta.examples),
                system_prompt_suffix=meta.system_prompt_suffix,
                max_tokens=meta.max_tokens,
                temperature=meta.temperature,
                mood_hint=meta.mood_hint,
                handler=bound,
                handler_is_async=meta.handler_is_async,
            )
            handlers[meta.name] = meta
            logger.debug("发现 @command: %s → %s.%s", meta.name, cls.__name__, attr_name)

    return handlers


# ═══════════════════════════════════════════════════════════════════════
# _dispatch_command —— 类型感知的命令调度
# ═══════════════════════════════════════════════════════════════════════

# 类型注解 → 转换函数 映射
_TYPE_COERCERS: dict[type, Any] = {
    int: lambda v: int(v) if v is not None else 0,
    float: lambda v: float(v) if v is not None else 0.0,
    bool: lambda v: str(v).lower() in ("true", "1", "yes") if v is not None else False,
    str: lambda v: str(v) if v is not None else "",
}


def _coerce_param(raw_value: Any, annotation: Any) -> Any:
    """根据类型注解将 CommandAST 中的参数值转换为 Python 类型。

    Args:
        raw_value: 来自 CommandAST.kwargs[name].value 的原始值
        annotation: 方法的类型注解

    Returns:
        转换后的值
    """
    if raw_value is None:
        return None
    # 已经是正确类型 → 直通
    if isinstance(annotation, type) and isinstance(raw_value, annotation):
        return raw_value
    # 查找转换器
    if annotation in _TYPE_COERCERS:
        try:
            return _TYPE_COERCERS[annotation](raw_value)
        except (ValueError, TypeError):
            return raw_value
    # 未知类型 → 原样返回
    return raw_value


async def dispatch_command(
    instance: object,
    cmd: "CommandAST",
    command_handlers: dict[str, "PluginCommandMeta"],
) -> "PluginResult":
    """根据 CommandAST.command 路由到对应的 @command 方法并调用。

    Args:
        instance: PluginBase 子类实例
        cmd: 用户命令 AST
        command_handlers: {command_name: PluginCommandMeta} 映射

    Returns:
        PluginResult

    如果找不到匹配的 handler，返回错误 PluginResult。
    """
    from sirius_chat.plugins.models import PluginResult

    meta = command_handlers.get(cmd.command)
    if meta is None or meta.handler is None:
        return PluginResult.fail(
            f"Plugin '{getattr(instance, '_name', '?')}' 未定义指令 '{cmd.command}' 的处理器"
        )

    handler = meta.handler

    # ── 类型感知的参数映射 ──
    try:
        sig = inspect.signature(handler)
    except (ValueError, TypeError) as exc:
        return PluginResult.fail(f"无法解析处理器签名: {exc}")

    bound_args: dict[str, Any] = {}
    for param_name, param in sig.parameters.items():
        # 跳过 self / cls
        if param_name in ("self", "cls"):
            continue

        annotation = param.annotation
        if annotation is inspect.Parameter.empty:
            annotation = str  # 默认当作字符串

        # 从 CommandAST 提取值
        if param_name in cmd.kwargs:
            # 优先使用命名参数
            raw = cmd.kwargs[param_name].value
            bound_args[param_name] = _coerce_param(raw, annotation)
        elif param_name in cmd.flags:
            # 布尔标志
            bound_args[param_name] = True
        elif param.default is not inspect.Parameter.empty:
            # 使用默认值
            bound_args[param_name] = param.default
        else:
            # 必填参数缺失 → 错误
            return PluginResult.fail(
                f"指令 '{cmd.command}' 缺少必填参数 '{param_name}'"
            )

    # 也注入位置参数作为 _0, _1, _2...
    for i, arg in enumerate(cmd.args):
        slot_name = f"_{i}"
        if slot_name not in bound_args:
            bound_args[slot_name] = arg.value

    # ── 调用 handler ──
    try:
        if meta.is_async:
            result = await handler(**bound_args)
        else:
            result = handler(**bound_args)
    except TypeError as exc:
        return PluginResult.fail(f"指令 '{cmd.command}' 参数类型错误: {exc}")
    except Exception as exc:
        logger.error(
            "Plugin 指令处理器异常 [%s.%s]: %s",
            getattr(instance, '_name', '?'),
            meta.name,
            exc,
            exc_info=True,
        )
        return PluginResult.fail(f"指令执行异常: {exc}")

    # ── 结果处理 ──
    if result is None:
        return PluginResult.ok(text="", data=None)
    if isinstance(result, PluginResult):
        # 如果 handler 未指定 render_mode，使用装饰器中的设置
        if not result.render_mode:
            result.render_mode = meta.render_mode
        if not result.mood_hint and meta.mood_hint:
            result.mood_hint = meta.mood_hint
        return result
    # 非 PluginResult 返回值 → 包装
    if isinstance(result, str):
        return PluginResult.ok(text=result, render_mode=meta.render_mode)
    return PluginResult.ok(data=result, render_mode=meta.render_mode)
