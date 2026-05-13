"""Plugin 注册表 —— 索引插件触发词与事件，提供快速查找。

数据结构：
    - _commands_index: [(pattern, pattern_type, plugin_name, command_name), ...]
    - _events_index: {event_type: [plugin_name, ...]}
    - _definitions: {plugin_name: PluginDefinition}

使用 PluginMatcher 进行文本级匹配。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sirius_chat.plugins.models import PluginDefinition

logger = logging.getLogger(__name__)


class PluginRegistry:
    """Plugin 注册表，提供多维度索引和快速查找。"""

    def __init__(self) -> None:
        # 指令索引：所有插件的触发词汇总
        # 每个条目: (pattern, pattern_type, plugin_name, command_name)
        self._commands_index: list[tuple[str, str, str, str]] = []

        # 事件索引：{event_type: [plugin_name, ...]}
        self._events_index: dict[str, list[str]] = {}

        # 插件定义存储：{plugin_name: PluginDefinition}
        self._definitions: dict[str, "PluginDefinition"] = {}

        # 插件实例存储：{plugin_name: PluginBase instance}
        self._instances: dict[str, object] = {}

        # 自然语言触发索引：用于 CognitionAnalyzer 融合
        self._nl_examples: dict[str, list[str]] = {}

    # ── 属性 ──

    @property
    def plugin_names(self) -> list[str]:
        """所有已注册插件的名称列表。"""
        return list(self._definitions.keys())

    @property
    def plugin_count(self) -> int:
        """已注册插件数量。"""
        return len(self._definitions)

    # ── 注册与查找 ──

    def register(self, definition: "PluginDefinition", instance: object | None = None) -> None:
        """注册一个插件。

        Args:
            definition: Plugin 定义
            instance: 可选的 PluginBase 实例
        """
        # 存储定义
        self._definitions[definition.name] = definition

        # 存储实例
        if instance is not None:
            self._instances[definition.name] = instance

        # 构建指令索引（从 plugin.json 的 triggers.commands）
        for cmd_name, pattern, pat_type in definition.all_patterns:
            self._commands_index.append((pattern, pat_type, definition.name, cmd_name))

        # 构建事件索引
        for evt in definition.events:
            evt_type = evt.type
            if evt_type not in self._events_index:
                self._events_index[evt_type] = []
            self._events_index[evt_type].append(definition.name)

        # 构建自然语言示例索引
        if definition.natural_language and definition.natural_language.examples:
            self._nl_examples[definition.name] = list(definition.natural_language.examples)

        logger.info(
            "注册 Plugin: %s v%s（%d 指令, %d 事件）",
            definition.name,
            definition.version,
            len(definition.commands),
            len(definition.events),
        )

    def sync_command_metas(self, plugin_name: str, command_metas: dict[str, object]) -> None:
        """从 PluginBase 实例同步 @command 装饰器元数据到指令索引。

        只更新 @command 装饰器定义的指令。plugin.json 的 triggers.commands
        已在 register() 中添加，无需重复。

        Args:
            plugin_name: Plugin 名称
            command_metas: {command_name: PluginCommandMeta} 字典
        """
        # 先移除该插件之前同步过的 @command 索引条目（保留 plugin.json 原始条目）
        # @command 条目通过 full_patterns 特征来区分（含 prefix 拼接）
        kept: list[tuple[str, str, str, str]] = []
        for p, pt, pn, cn in self._commands_index:
            if pn != plugin_name:
                kept.append((p, pt, pn, cn))
                continue
            # 保留来自 plugin.json 的条目：检查是否也存在于 command_metas
            is_decorator = any(
                hasattr(m, 'name') and getattr(m, 'name', '') == cn
                for m in command_metas.values()
            )
            if not is_decorator:
                kept.append((p, pt, pn, cn))
        self._commands_index = kept

        # 添加 @command 装饰器的模式（full_patterns 已包含 prefix）
        for meta in command_metas.values():
            if not hasattr(meta, 'full_patterns'):
                continue
            for pattern in getattr(meta, 'full_patterns', []):
                self._commands_index.append((
                    pattern,
                    getattr(meta, 'pattern_type', 'prefix'),
                    plugin_name,
                    getattr(meta, 'name', ''),
                ))
        logger.debug("同步 %s 的 @command 元数据: %d 条指令", plugin_name, len(command_metas))

    def get(self, name: str) -> "PluginDefinition | None":
        """按名称获取 PluginDefinition。"""
        return self._definitions.get(name)

    def get_instance(self, name: str) -> object | None:
        """按名称获取 PluginBase 实例。"""
        return self._instances.get(name)

    def set_instance(self, name: str, instance: object) -> None:
        """设置 PluginBase 实例。"""
        self._instances[name] = instance

    def get_all_definitions(self) -> list["PluginDefinition"]:
        """获取所有已注册的 PluginDefinition。"""
        return list(self._definitions.values())

    def get_command_plugins(self) -> list["PluginDefinition"]:
        """获取所有包含指令触发器的插件。"""
        return [d for d in self._definitions.values() if d.commands]

    def get_event_plugins(self, event_type: str) -> list["PluginDefinition"]:
        """获取所有绑定到特定事件类型的插件。"""
        names = self._events_index.get(event_type, [])
        return [self._definitions[n] for n in names if n in self._definitions]

    # ── 文本匹配（使用 lexer.PluginMatcher） ──

    def match_message(self, text: str):
        """尝试将用户文本匹配到已注册的 Plugin。

        Args:
            text: 用户输入文本

        Returns:
            MatchResult 或 None
        """
        from sirius_chat.plugins.lexer import Lexer, PluginMatcher, Tokenizer

        # 精确指令路径：尝试 Tokenizer → Lexer
        lexer = Lexer(Tokenizer())
        tokens = lexer.tokenize(text)
        lexed = lexer.lex(tokens, raw_text=text)

        if lexed is not None:
            # 按指令名查找 Plugin
            for pattern, pat_type, plugin_name, cmd_name in self._commands_index:
                if pat_type == "prefix" and text.strip().startswith(pattern):
                    definition = self._definitions.get(plugin_name)
                    if definition is not None:
                        from sirius_chat.plugins.lexer import MatchResult

                        return MatchResult(
                            plugin_name=plugin_name,
                            command_name=cmd_name,
                            pattern=pattern,
                            pattern_type=pat_type,
                            confidence=1.0,
                            lexed=lexed,
                        )

        # 关键词/正则路径：遍历索引
        matcher = PluginMatcher()
        for pattern, pat_type, plugin_name, cmd_name in self._commands_index:
            definition = self._definitions.get(plugin_name)
            if definition is None:
                continue
            # 构建临时 PluginCommandDef 列表
            for cmd in definition.commands:
                result = matcher.match(
                    text, [cmd], plugin_name, lexer=(lexer if pat_type == "prefix" else None)
                )
                if result is not None:
                    return result

        return None

    def get_plugin_descriptions(self) -> str:
        """生成 Plugin 指令描述文本（用于 LLM Cognition Prompt）。

        注意：LLM 兜底只在规则匹配未命中时触发，因此不输出触发词（前缀/关键词/正则），
        只输出语义理解所需信息：插件名、功能描述、参数和 NL 示例。

        Returns:
            格式化的 Plugin 描述文本，如：
            - weather: 查询城市天气
              参数: city (str, 必填) - 城市名称
              NL示例: "帮我查一下{city}的天气"
        """
        lines: list[str] = []
        for definition in self._definitions.values():
            if not definition.commands:
                continue
            desc = definition.description or definition.display_name
            lines.append(f"- {definition.name}: {desc}")

            # 参数信息（帮助 LLM 知道有哪些参数及类型）
            if definition.parameters:
                param_parts: list[str] = []
                for p in definition.parameters:
                    required = "必填" if p.required else "可选"
                    param_info = f"{p.name} ({p.type}, {required})"
                    if p.description:
                        param_info += f" - {p.description}"
                    param_parts.append(param_info)
                lines.append(f"  参数: {'; '.join(param_parts)}")

            # NL 示例（帮助 LLM 理解自然语言如何映射到参数）
            if definition.natural_language and definition.natural_language.examples:
                nl_text = "、".join(
                    f'"{e}"' for e in definition.natural_language.examples[:3]
                )
                lines.append(f"  NL示例: {nl_text}")

        return "\n".join(lines)

    def get_nl_examples(self) -> dict[str, list[str]]:
        """获取所有插件的自然语言触发示例。"""
        return dict(self._nl_examples)

    def unregister(self, name: str) -> None:
        """注销一个插件。"""
        self._definitions.pop(name, None)
        self._instances.pop(name, None)
        self._nl_examples.pop(name, None)
        # 清理索引
        self._commands_index = [
            (p, pt, pn, cn) for p, pt, pn, cn in self._commands_index if pn != name
        ]
        for evt_type in list(self._events_index.keys()):
            self._events_index[evt_type] = [
                n for n in self._events_index[evt_type] if n != name
            ]
            if not self._events_index[evt_type]:
                del self._events_index[evt_type]
        logger.info("注销 Plugin: %s", name)

    def clear(self) -> None:
        """清空注册表。"""
        self._commands_index.clear()
        self._events_index.clear()
        self._definitions.clear()
        self._instances.clear()
        self._nl_examples.clear()
