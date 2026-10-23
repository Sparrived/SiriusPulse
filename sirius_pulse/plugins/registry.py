"""Plugin 注册表 —— 索引插件触发词与事件，提供快速查找。

数据结构：
    - _commands_index: [(pattern, pattern_type, plugin_name, command_name), ...]
    - _definitions: {plugin_name: PluginDefinition}

使用 PluginMatcher 进行文本级匹配。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sirius_pulse.plugins.models import PluginDefinition

logger = logging.getLogger(__name__)


class PluginRegistry:
    """Plugin 注册表，提供多维度索引和快速查找。"""

    def __init__(self) -> None:
        # 指令索引：所有插件的触发词汇总
        # 每个条目: (pattern, pattern_type, plugin_name, command_name)
        self._commands_index: list[tuple[str, str, str, str]] = []

        # 插件定义存储：{plugin_name: PluginDefinition}
        self._definitions: dict[str, "PluginDefinition"] = {}

        # 插件实例存储：{plugin_name: PluginBase instance}
        self._instances: dict[str, object] = {}

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

        Raises:
            ValueError: 当插件名已存在时
        """
        # 检查插件名冲突
        if definition.name in self._definitions:
            old_path = self._definitions[definition.name].source_path
            new_path = definition.source_path
            logger.warning(
                "插件名冲突: '%s' 已被 %s 注册，新路径 %s 将被跳过",
                definition.name,
                old_path,
                new_path,
            )
            raise ValueError(f"插件名 '{definition.name}' 已存在，跳过注册")

        # 存储定义
        self._definitions[definition.name] = definition

        # 存储实例
        if instance is not None:
            self._instances[definition.name] = instance

        # 构建指令索引（从 plugin.json 的 triggers.commands）
        for cmd_name, pattern, pat_type in definition.all_patterns:
            self._commands_index.append((pattern, pat_type, definition.name, cmd_name))

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
                hasattr(m, "name") and getattr(m, "name", "") == cn for m in command_metas.values()
            )
            if not is_decorator:
                kept.append((p, pt, pn, cn))
        self._commands_index = kept

        # 添加 @command 装饰器的模式（full_patterns 已包含 prefix）
        # 注意：hidden_from_intent 的指令也需要加入索引，
        # 因为显式命令（以 /、#、! 开头）应忽略 hidden_from_intent 标记
        for meta in command_metas.values():
            if not hasattr(meta, "full_patterns"):
                continue
            for pattern in getattr(meta, "full_patterns", []):
                self._commands_index.append(
                    (
                        pattern,
                        getattr(meta, "pattern_type", "prefix"),
                        plugin_name,
                        getattr(meta, "name", ""),
                    )
                )
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

    # ── 文本匹配（使用 lexer.PluginMatcher） ──

    def _is_pattern_hidden(self, plugin_name: str, cmd_name: str) -> bool:
        """检查指定插件的指令是否被标记为 hidden_from_intent。"""
        definition = self._definitions.get(plugin_name)
        if definition is None:
            return False
        # 插件级隐藏
        if definition.permissions.hidden_from_intent:
            return True
        # 指令级隐藏
        for cmd in definition.commands:
            if cmd.name == cmd_name and cmd.hidden_from_intent:
                return True
        return False

    def match_message(self, text: str, *, include_hidden: bool = False):
        """尝试将用户文本匹配到已注册的 Plugin。

        Args:
            text: 用户输入文本
            include_hidden: 是否包含 hidden_from_intent 的指令。默认 False，
                            意图识别调用时传 False，显式执行时传 True。

        Returns:
            MatchResult 或 None
        """
        from sirius_pulse.plugins.lexer import (
            Lexer,
            MatchResult,
            PluginMatcher,
            Tokenizer,
            _is_explicit_prefix_pattern,
            _prefix_pattern_matches,
        )

        # 精确指令路径：尝试 Tokenizer → Lexer
        lexer = Lexer(Tokenizer())
        tokens = lexer.tokenize(text)
        lexed = lexer.lex(tokens, raw_text=text)

        # 如果 lexed 不为 None，说明 Tokenizer 成功解析出了 CMD_HEAD，
        # 这是一个显式命令（如 /ca analyse），应忽略 hidden_from_intent 标记
        # hidden_from_intent 仅用于阻止自然语言触发，不应阻止显式命令
        is_explicit_command = lexed is not None
        effective_include_hidden = include_hidden or is_explicit_command

        stripped = text.strip()
        if lexed is not None:
            explicit_matches = [
                (pattern, pat_type, plugin_name, cmd_name)
                for pattern, pat_type, plugin_name, cmd_name in self._commands_index
                if pat_type == "prefix"
                and _is_explicit_prefix_pattern(pattern)
                and _prefix_pattern_matches(stripped, pattern)
                and plugin_name in self._definitions
            ]
            if explicit_matches:
                pattern, pat_type, plugin_name, cmd_name = max(
                    explicit_matches, key=lambda item: len(item[0])
                )
                return MatchResult(
                    plugin_name=plugin_name,
                    command_name=cmd_name,
                    pattern=pattern,
                    pattern_type=pat_type,
                    confidence=1.0,
                    lexed=lexed,
                )

        # Preserve registration-order semantics for non-explicit prefix, keyword,
        # and regex triggers. Explicit patterns are handled only by the strict path above.
        matcher = PluginMatcher()
        for pattern, pat_type, plugin_name, cmd_name in self._commands_index:
            if pat_type == "prefix" and _is_explicit_prefix_pattern(pattern):
                continue
            if not effective_include_hidden and self._is_pattern_hidden(plugin_name, cmd_name):
                continue
            if plugin_name not in self._definitions:
                continue
            from sirius_pulse.plugins.models import PluginCommandDef

            command = PluginCommandDef(
                name=cmd_name,
                patterns=[pattern],
                pattern_type=pat_type,
            )
            result = matcher.match(
                text,
                [command],
                plugin_name,
                lexer=(lexer if pat_type == "prefix" else None),
            )
            if result is not None:
                return result

        return None

    def get_plugin_prompt_injections(self, caller_is_developer: bool = False) -> list[str]:
        """收集所有已注册插件的 prompt_inject 文本。

        用于 PromptFactory 构建插件感知提示词段落，让 AI 知晓插件的能力。
        插件本身的 prompt_inject 不受 hidden_from_intent 影响——即使对意图
        识别隐藏，仍然可以向人格 prompt 注入提示词。

        Args:
            caller_is_developer: 调用者是否为开发者。

        Returns:
            所有满足条件的插件的 prompt_inject 文本列表。
        """
        injects: list[str] = []
        for definition in self._definitions.values():
            if not definition.prompt_inject:
                continue
            if definition.permissions.developer_only and not caller_is_developer:
                continue
            injects.append(definition.prompt_inject)
        return injects

    def unregister(self, name: str) -> None:
        """注销一个插件。"""
        self._definitions.pop(name, None)
        self._instances.pop(name, None)
        # 清理索引
        self._commands_index = [
            (p, pt, pn, cn) for p, pt, pn, cn in self._commands_index if pn != name
        ]
        logger.info("注销 Plugin: %s", name)

    def clear(self) -> None:
        """清空注册表。"""
        self._commands_index.clear()
        self._definitions.clear()
        self._instances.clear()
