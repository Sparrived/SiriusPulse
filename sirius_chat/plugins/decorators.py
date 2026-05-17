"""Plugin 装饰器系统 —— @command 声明式指令注册。

提供比覆写 execute() 更具可扩展性的指令定义方式：
    - 每个 @command 装饰一个方法，对应一个 Plugin 指令
    - 方法参数的类型注解自动用于参数类型校验
    - 方法参数的默认值自动成为可选参数的缺省值
    - 框架自动从 CommandAST 提取参数并注入

使用示例：

    from sirius_chat.plugins import PluginBase, command, PluginResponse

    class WeatherPlugin(PluginBase):
        @command("weather", patterns=["/天气", "查天气"], render_mode="llm",
                 description="查询城市天气")
        async def query_weather(self, city: str, unit: str = "celsius") -> PluginResponse:
            data = await self._fetch_weather(city, unit)
            return PluginResponse.ok(data=data, mood_hint="温暖关心")

        @command("forecast", patterns=["/预报"], render_mode="direct")
        def forecast(self, city: str, days: int = 3) -> PluginResponse:
            result = self._get_forecast(city, days)
            return PluginResponse.ok(text=result)

    # 也支持不覆写 execute()，框架自动按 cmd.command 路由
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar, overload, TYPE_CHECKING

if TYPE_CHECKING:
    from sirius_chat.plugins.models import CommandAST, PluginResponse

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
    prefix: str = field(default="")        # 指令前缀（如 "/"、"#"），空串表示无前缀
    patterns: list[str] = field(default_factory=list)          # 触发词列表（不含前缀）
    pattern_type: str = "prefix"           # prefix | keyword | regex
    render_mode: str = "direct"            # direct | llm | silent
    description: str = ""                  # 人类可读的描述
    examples: list[str] = field(default_factory=list)          # 使用示例
    # LLM 渲染参数
    system_prompt_suffix: str = ""
    max_tokens: int = 500
    temperature: float = 0.8
    mood_hint: str = ""
    # 执行超时
    timeout: float = 0.0                   # 单次执行超时秒数，0 使用默认值
    # 内部引用
    handler: Callable[..., Any] | None = None   # 绑定的方法
    handler_is_async: bool = False              # 是否为异步方法

    @property
    def is_async(self) -> bool:
        """是否为异步 handler。"""
        return self.handler_is_async

    @property
    def full_patterns(self) -> list[str]:
        """返回带前缀的完整触发词列表。

        如果设置了 prefix，将其自动拼接到每个 pattern 前面。
        例如 prefix="/" + patterns=["天气", "weather"] → ["/天气", "/weather"]
        """
        if not self.prefix:
            return list(self.patterns)
        return [self.prefix + p for p in self.patterns]


# ═══════════════════════════════════════════════════════════════════════
# @command 装饰器
# ═══════════════════════════════════════════════════════════════════════

def command(
    name: str,
    *,
    prefix: str = "",
    patterns: list[str] | None = None,
    pattern_type: str = "prefix",
    render_mode: str = "direct",
    description: str = "",
    examples: list[str] | None = None,
    system_prompt_suffix: str = "",
    max_tokens: int = 500,
    temperature: float = 0.8,
    mood_hint: str = "",
    timeout: float = 0.0,
) -> Callable[[F], F]:
    """声明式指令注册装饰器。

    将 PluginBase 子类的方法注册为一个 Plugin 指令处理器。

    Args:
        name: 指令名（如 "weather"），映射到 CommandAST.command
        prefix: 指令前缀（如 "/"、"#"、"!" 等），默认 "" 表示无前缀。
                设置后会自动拼接到每个 pattern 前面，
                例如 prefix="/" + patterns=["天气"] → 实际匹配 "/天气"
        patterns: 触发词列表（如 ["天气", "查天气"]），不含前缀
        pattern_type: 匹配模式类型 ("prefix" | "keyword" | "regex")
        render_mode: 输出策略 ("direct" | "llm" | "silent")。
                     单个 handler 执行过程中可通过 PluginResponse.render_mode 覆写。
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
            def do_roll(self, expression: str) -> PluginResponse:
                result = roll_dice(expression)
                return PluginResponse.ok(text=result)

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
        prefix=prefix,
        patterns=patterns or [name],
        pattern_type=pattern_type,
        render_mode=render_mode,
        description=description,
        examples=examples or [],
        system_prompt_suffix=system_prompt_suffix,
        max_tokens=max_tokens,
        temperature=temperature,
        mood_hint=mood_hint,
        timeout=timeout,
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
                prefix=meta.prefix,
                patterns=list(meta.patterns),
                pattern_type=meta.pattern_type,
                render_mode=meta.render_mode,
                description=meta.description,
                examples=list(meta.examples),
                system_prompt_suffix=meta.system_prompt_suffix,
                max_tokens=meta.max_tokens,
                temperature=meta.temperature,
                mood_hint=meta.mood_hint,
                timeout=meta.timeout,
                handler=bound,
                handler_is_async=meta.handler_is_async,
            )
            handlers[meta.name] = meta
            logger.debug("发现 @command: %s → %s.%s", meta.name, cls.__name__, attr_name)

    return handlers


# ═══════════════════════════════════════════════════════════════════════
# dispatch_command_stream —— 类型感知的命令调度（支持流式输出）
# ═══════════════════════════════════════════════════════════════════════

# 类型注解 → 转换函数 映射
_TYPE_COERCERS: dict[type, Any] = {
    int: lambda v: int(v) if v not in (None, "") else 0,
    float: lambda v: float(v) if v not in (None, "") else 0.0,
    bool: lambda v: str(v).lower() in ("true", "1", "yes") if v not in (None, "") else False,
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


async def dispatch_command_stream(
    instance: object,
    cmd: "CommandAST",
    command_handlers: dict[str, "PluginCommandMeta"],
) -> list["PluginResponse"]:
    """根据 CommandAST.command 路由到对应的 @command 方法并调用。

    支持两种 handler 模式：
        1. 常规 async 函数 → 返回单元素 list[PluginResponse]
        2. 流式 async generator → 遍历 yield，收集所有 PluginResponse

    流式 handler 可以中途 `yield` 字符串/PluginResponse 做即时输出：
        @command("search", prefix="/", patterns=["search"])
        async def search(self, query: str):
            yield "正在搜索..."           # 立即发送（direct 模式）
            data = await self._fetch(query)
            yield PluginResponse.ok(data=data, render_mode="llm")  # 人格化

    Args:
        instance: PluginBase 子类实例
        cmd: 用户命令 AST
        command_handlers: {command_name: PluginCommandMeta} 映射

    Returns:
        list[PluginResponse]（至少一个元素）
    """
    from sirius_chat.plugins.models import PluginResponse

    meta = command_handlers.get(cmd.command)
    if meta is None or meta.handler is None:
        return [PluginResponse.fail(
            f"Plugin '{getattr(instance, '_name', '?')}' 未定义指令 '{cmd.command}' 的处理器"
        )]

    handler = meta.handler

    # ── 类型感知的参数映射 ──
    try:
        sig = inspect.signature(handler)
    except (ValueError, TypeError) as exc:
        return [PluginResponse.fail(f"无法解析处理器签名: {exc}")]

    # 解析字符串注解（兼容 from __future__ import annotations 导致的字符串化）
    try:
        from typing import get_type_hints
        resolved_hints = get_type_hints(handler, include_extras=False)
    except Exception:
        resolved_hints = {}

    # 打印注解解析结果（调试用）
    if any(isinstance(p.annotation, str) for p in sig.parameters.values() if p.name not in ("self", "cls")):
        logger.info(
            "插件 %s 指令 %s 字符串注解已解析：%s",
            getattr(instance, '_name', '?'),
            cmd.command,
            {k: v for k, v in resolved_hints.items()},
        )

    bound_args: dict[str, Any] = {}
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        annotation = param.annotation
        if annotation is inspect.Parameter.empty:
            annotation = str
        # 如果注解是字符串（from __future__ import annotations 导致），用 get_type_hints 解析
        if isinstance(annotation, str) and param_name in resolved_hints:
            annotation = resolved_hints[param_name]
        if param_name in cmd.kwargs:
            raw = cmd.kwargs[param_name].value
            coerced = _coerce_param(raw, annotation)
            # 类型校验：如果转换后的值与注解类型不匹配，回退到参数默认值
            if isinstance(annotation, type) and not isinstance(coerced, annotation):
                logger.info(
                    "插件 %s 指令 %s 参数 %s 类型不匹配: 值=%r(%s) 期望=%s，使用默认值",
                    getattr(instance, '_name', '?'),
                    cmd.command,
                    param_name,
                    coerced,
                    type(coerced).__name__,
                    annotation.__name__,
                )
                if param.default is not inspect.Parameter.empty:
                    bound_args[param_name] = param.default
                elif annotation is int:
                    bound_args[param_name] = 0
                elif annotation is float:
                    bound_args[param_name] = 0.0
                elif annotation is bool:
                    bound_args[param_name] = False
                elif annotation is str:
                    bound_args[param_name] = ""
                else:
                    bound_args[param_name] = None
            else:
                bound_args[param_name] = coerced
        elif param_name in cmd.flags:
            bound_args[param_name] = True
        else:
            # 非名称匹配的参数延迟到位置回退阶段处理
            pass

    # ── 位置参数回退 ──
    # 对于未通过名称匹配到 cmd.kwargs / cmd.flags 的 handler 参数，
    # 按位置从 cmd.args 中消费。最后一个 string 参数合并所有剩余位置参数。
    pos_idx = 0
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        if param_name in bound_args:
            continue

        annotation = param.annotation
        if annotation is inspect.Parameter.empty:
            annotation = str
        if isinstance(annotation, str) and param_name in resolved_hints:
            annotation = resolved_hints[param_name]

        if pos_idx >= len(cmd.args):
            # 没有更多位置参数，回退到默认值或报错
            if param.default is not inspect.Parameter.empty:
                bound_args[param_name] = param.default
            else:
                return [PluginResponse.fail(
                    f"指令 '{cmd.command}' 缺少必填参数 '{param_name}'"
                )]
            continue

        # rest args 模式：只剩最后一个未绑定 string 参数时，合并剩余位置参数
        remaining_unbound = [
            pn for pn, p in sig.parameters.items()
            if pn not in ("self", "cls") and pn not in bound_args
            and pn != param_name
        ]
        if not remaining_unbound and annotation is str:
            joined = " ".join(str(a.value) for a in cmd.args[pos_idx:])
            bound_args[param_name] = joined
            pos_idx = len(cmd.args)
        else:
            raw = cmd.args[pos_idx].value
            bound_args[param_name] = _coerce_param(raw, annotation)
            pos_idx += 1

    # ── 打印参数解析结果（调试用）──
    logger.info(
        "插件 %s 指令 %s 参数绑定结果：%s",
        getattr(instance, '_name', '?'),
        cmd.command,
        {k: (v, type(v).__name__) for k, v in bound_args.items()},
    )

    # ── 调用 handler（支持流式）──
    return await _invoke_handler(handler, bound_args, meta, instance)


async def _invoke_handler(
    handler: Callable[..., Any],
    bound_args: dict[str, Any],
    meta: "PluginCommandMeta",
    instance: object,
) -> list["PluginResponse"]:
    """调用 handler 并归一化为 list[PluginResponse]。

    自动检测 handler 类型：
        - async generator → 遍历 yield，打包每个产出
        - async function → 单次调用，打包返回
        - sync function → asyncio.to_thread 执行，打包返回
    """
    from sirius_chat.plugins.models import PluginResponse

    results: list[PluginResponse] = []

    try:
        if inspect.isasyncgenfunction(handler):
            # ══ 流式 async generator ══
            gen = handler(**bound_args)
            async for raw in gen:
                pr = _normalize_stream_item(raw, meta)
                results.append(pr)
            if not results:
                results.append(PluginResponse.ok(text="", data=None))
            return results

        if meta.is_async:
            raw = await handler(**bound_args)
        else:
            raw = await asyncio.to_thread(handler, **bound_args)

        pr = _normalize_stream_item(raw, meta)
        results.append(pr)
        return results

    except TypeError as exc:
        return [PluginResponse.fail(f"指令 '{meta.name}' 参数类型错误: {exc}")]
    except Exception as exc:
        logger.error(
            "Plugin 指令处理器异常 [%s.%s]: %s",
            getattr(instance, '_name', '?'),
            meta.name,
            exc,
            exc_info=True,
        )
        return [PluginResponse.fail(f"指令执行异常: {exc}")]


def _normalize_stream_item(raw: Any, meta: "PluginCommandMeta") -> "PluginResponse":
    """将 handler 产出归一化为 PluginResponse。

    - None → 空成功
    - str → PluginResponse.ok(text=str, render_mode="direct")
    - PluginResponse → 原样（补齐 render_mode/mood_hint）
    - 其他 → PluginResponse.ok(data=raw)
    """
    from sirius_chat.plugins.models import PluginResponse

    if raw is None:
        return PluginResponse.ok(text="", data=None)
    if isinstance(raw, str):
        return PluginResponse.ok(text=raw, render_mode="direct")
    if isinstance(raw, PluginResponse):
        if not raw.render_mode:
            raw.render_mode = meta.render_mode
        if not raw.mood_hint and meta.mood_hint:
            raw.mood_hint = meta.mood_hint
        return raw
    return PluginResponse.ok(data=raw, render_mode=meta.render_mode)


# 保持旧函数名兼容
dispatch_command = dispatch_command_stream
