"""Plugin 词法分析器 —— Tokenizer + Lexer + Parser。

负责将用户输入的文本解析为 CommandAST，支持：
- Unix 风格：/weather Beijing --unit=c
- 井号前缀：#roll 2d6+3
- @ 提及：@Bot 天气 北京
- 自然语言触发：查一下北京天气（关键词前缀匹配）
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from sirius_pulse.plugins.models import ArgNode, CommandAST, PluginCommandDef, PluginDefinition

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Token 定义
# ═══════════════════════════════════════════════════════════════════════

class TokenType(Enum):
    """Token 类型枚举。"""

    CMD_HEAD = auto()      # 指令头：/weather 中的 weather
    LONG_OPT = auto()      # 长选项：--unit
    SHORT_OPT = auto()     # 短选项：-u
    EQ = auto()             # = 赋值符
    ARG_VALUE = auto()     # 参数值
    WS = auto()             # 空白
    LITERAL = auto()        # 无法识别的字面量
    MENTION = auto()        # @ 提及


@dataclass(slots=True)
class Token:
    """词法分析产生的 Token。"""

    type: TokenType
    value: str
    raw: str = ""
    position: int = 0


# ═══════════════════════════════════════════════════════════════════════
# Tokenizer —— 将原始文本拆分为 Token 序列
# ═══════════════════════════════════════════════════════════════════════

class Tokenizer:
    """基于状态机的 Tokenizer。

    将输入文本拆分为 TokenType 序列，供 Lexer 进一步分析。
    """

    # 指令前缀字符
    CMD_PREFIXES = frozenset({"/", "#", "!"})

    def tokenize(self, text: str) -> list[Token]:
        """将文本拆分为 Token 序列。"""
        text = text.strip()
        if not text:
            return []

        tokens: list[Token] = []
        i = 0
        n = len(text)

        while i < n:
            ch = text[i]

            # 空白
            if ch.isspace():
                j = i
                while j < n and text[j].isspace():
                    j += 1
                tokens.append(Token(TokenType.WS, text[i:j], raw=text[i:j], position=i))
                i = j
                continue

            # 长选项：--xxx
            if ch == "-" and i + 1 < n and text[i + 1] == "-":
                j = i + 2
                while j < n and (text[j].isalnum() or text[j] in ("_", "-")):
                    j += 1
                tokens.append(Token(TokenType.LONG_OPT, text[i + 2 : j], raw=text[i:j], position=i))
                i = j
                continue

            # 短选项：-x
            if ch == "-" and i + 1 < n and text[i + 1].isalpha():
                tokens.append(Token(TokenType.SHORT_OPT, text[i + 1 : i + 2], raw=text[i:i + 2], position=i))
                i += 2
                continue

            # 等号赋值（--unit=c 中的 =）
            if ch == "=":
                tokens.append(Token(TokenType.EQ, "=", raw="=", position=i))
                i += 1
                continue

            # 指令前缀
            if ch in self.CMD_PREFIXES and (i == 0 or (i > 0 and text[i - 1].isspace())):
                # 检查后面是否跟着有效的指令名
                j = i + 1
                while j < n and (text[j].isalnum() or text[j] == "_"):
                    j += 1
                if j > i + 1:
                    tokens.append(Token(TokenType.CMD_HEAD, text[i + 1 : j], raw=text[i:j], position=i))
                    i = j
                    continue
                # 单个前缀字符后没有字母 → 当作字面量
                tokens.append(Token(TokenType.LITERAL, ch, raw=ch, position=i))
                i += 1
                continue

            # @ 提及
            if ch == "@":
                j = i + 1
                while j < n and not text[j].isspace():
                    j += 1
                tokens.append(Token(TokenType.MENTION, text[i + 1 : j], raw=text[i:j], position=i))
                i = j
                continue

            # 普通参数值
            j = i
            while j < n and not text[j].isspace() and text[j] not in ("=",):
                j += 1
            val = text[i:j]
            tokens.append(Token(TokenType.ARG_VALUE, val, raw=val, position=i))
            i = j

        return tokens


# ═══════════════════════════════════════════════════════════════════════
# Lexer —— Token 序列 → 结构化指令信息
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class LexedCommand:
    """Lexer 输出的结构化指令信息。

    支持多层嵌套指令组，如 /ca report daily 中：
    - command="ca"
    - subcommand="report"（第一级子命令，向后兼容）
    - subcommands=["report", "daily"]（完整子命令路径）
    """

    command: str                               # 标准化指令名（小写）
    raw_command: str                           # 原始指令文本
    prefix: str                                # 触发前缀 "/" / "#" / ""
    subcommand: str = ""                       # 第一级子命令名（向后兼容）
    subcommands: list[str] = field(default_factory=list)  # 完整子命令路径
    positional_args: list[str] = field(default_factory=list)  # 位置参数
    named_args: dict[str, str] = field(default_factory=dict)  # --key=value 或 -k value
    flags: set[str] = field(default_factory=set)               # 布尔标志（--verbose, -v）
    raw_text: str = ""

    @property
    def command_path(self) -> list[str]:
        """获取完整指令路径列表。"""
        path = [self.command]
        if self.subcommands:
            path.extend(self.subcommands)
        elif self.subcommand:
            path.append(self.subcommand)
        return path


class Lexer:
    """将 Token 序列解析为 LexedCommand。

    处理长/短选项、位置参数、等号赋值等。
    """

    def __init__(self, tokenizer: Tokenizer | None = None) -> None:
        self._tokenizer = tokenizer or Tokenizer()

    def tokenize(self, text: str) -> list[Token]:
        """便捷方法：直接 tokenize 文本。"""
        return self._tokenizer.tokenize(text)

    def lex(self, tokens: list[Token], raw_text: str = "") -> LexedCommand | None:
        """将 Token 序列解析为 LexedCommand。

        返回 None 表示无法识别为有效指令（无 CMD_HEAD）。
        """
        if not tokens:
            return None

        # 查找第一个 CMD_HEAD
        cmd_idx = -1
        for i, tok in enumerate(tokens):
            if tok.type == TokenType.CMD_HEAD:
                cmd_idx = i
                break

        if cmd_idx < 0:
            return None

        cmd_token = tokens[cmd_idx]
        prefix = cmd_token.raw[0] if cmd_token.raw else ""
        command = cmd_token.value.lower()
        raw_command = cmd_token.value

        result = LexedCommand(
            command=command,
            raw_command=raw_command,
            prefix=prefix,
            raw_text=raw_text,
        )

        # 解析后续参数
        i = cmd_idx + 1
        pending_opt: str | None = None

        while i < len(tokens):
            tok = tokens[i]

            if tok.type == TokenType.WS:
                i += 1
                continue

            if tok.type == TokenType.LONG_OPT:
                # 如果有上一个未完成的选项，先将其作为布尔标志
                if pending_opt:
                    result.flags.add(pending_opt)
                pending_opt = tok.value
                i += 1
                continue

            if tok.type == TokenType.SHORT_OPT:
                if pending_opt:
                    result.flags.add(pending_opt)
                pending_opt = tok.value
                i += 1
                continue

            if tok.type == TokenType.EQ:
                # --key=value 中的 =，后面跟着值
                if pending_opt and i + 1 < len(tokens) and tokens[i + 1].type in (TokenType.ARG_VALUE, TokenType.LITERAL):
                    val = tokens[i + 1].value
                    result.named_args[pending_opt] = val
                    pending_opt = None
                    i += 2
                    continue
                i += 1
                continue

            if tok.type in (TokenType.ARG_VALUE, TokenType.LITERAL):
                if pending_opt:
                    # --key value 形式
                    result.named_args[pending_opt] = tok.value
                    pending_opt = None
                else:
                    # 位置参数
                    result.positional_args.append(tok.value)
                i += 1
                continue

            # MENTION 或其他类型当作位置参数
            if tok.type == TokenType.MENTION:
                result.positional_args.append(tok.value)
                i += 1
                continue

            i += 1

        # 最后未完成的选项作为布尔标志
        if pending_opt:
            result.flags.add(pending_opt)

        return result


# ═══════════════════════════════════════════════════════════════════════
# CommandParser —— LexedCommand + PluginDefinition → CommandAST
# ═══════════════════════════════════════════════════════════════════════

class CommandParser:
    """将 LexedCommand 与 PluginDefinition 的参数定义绑定，生成 CommandAST。"""

    def parse(self, lexed: LexedCommand, plugin_def: PluginDefinition) -> CommandAST:
        """将 LexedCommand 按 Plugin 的参数定义转换为 CommandAST。

        根据参数定义的 position 和 type 进行位置映射和类型转换。
        """
        # 构建按位置排序的参数定义列表
        sorted_params = sorted(plugin_def.parameters, key=lambda p: p.position)
        # 按名称索引的参数定义
        param_by_name: dict[str, Any] = {p.name: p for p in plugin_def.parameters}

        # 映射位置参数
        args: list[ArgNode] = []
        kwargs: dict[str, ArgNode] = {}
        pos_idx = 0

        for raw_val in lexed.positional_args:
            # 查找对应的参数定义
            param_def = None
            if pos_idx < len(sorted_params):
                param_def = sorted_params[pos_idx]

            # 始终保留到 args（供 @command handler 按位置消费）
            args.append(ArgNode(value=raw_val, raw=raw_val, type_hint="str"))

            if param_def is not None:
                # 同时按插件参数定义映射到 kwargs（向后兼容）
                coerced_value = self._coerce_value(raw_val, param_def.type)
                node = ArgNode(value=coerced_value, raw=raw_val, type_hint=param_def.type)
                kwargs[param_def.name] = node

            pos_idx += 1

        # 映射命名参数
        for opt_name, raw_val in lexed.named_args.items():
            param_def = param_by_name.get(opt_name)
            if param_def:
                coerced_value = self._coerce_value(raw_val, param_def.type)
                node = ArgNode(value=coerced_value, raw=raw_val, type_hint=param_def.type)
                kwargs[param_def.name] = node
            else:
                # 未定义的选项 → 作为普通命名参数
                node = ArgNode(value=raw_val, raw=raw_val, type_hint="str")
                kwargs[opt_name] = node

        # 填充默认值
        for param_def in plugin_def.parameters:
            if param_def.name not in kwargs and param_def.default is not None:
                node = ArgNode(
                    value=param_def.default,
                    raw=str(param_def.default),
                    type_hint=param_def.type,
                )
                kwargs[param_def.name] = node

        # 映射标志
        flags: set[str] = set(lexed.flags)

        return CommandAST(
            command=lexed.command,
            raw_text=lexed.raw_text,
            prefix=lexed.prefix,
            subcommand=lexed.subcommand,
            subcommands=lexed.subcommands,
            args=args,
            kwargs=kwargs,
            flags=flags,
        )

    @staticmethod
    def _coerce_value(raw: str, type_hint: str) -> Any:
        """将字符串值按类型提示转换。"""
        if type_hint == "int":
            try:
                return int(raw)
            except ValueError:
                return raw
        if type_hint == "float":
            try:
                return float(raw)
            except ValueError:
                return raw
        if type_hint == "bool":
            return raw.lower() in ("true", "1", "yes")
        # str / list[str] / 其他 → 原样
        return raw


# ═══════════════════════════════════════════════════════════════════════
# PluginMatcher —— 在 Registry 之外做文本级匹配
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class MatchResult:
    """匹配结果。"""

    plugin_name: str
    command_name: str
    pattern: str
    pattern_type: str
    confidence: float = 1.0
    lexed: LexedCommand | None = None


class PluginMatcher:
    """在文本层面匹配 Plugin 的触发词。

    用于 Bridge 层快速拦截和 CognitionAnalyzer 内部的规则匹配。
    """

    def match(
        self,
        text: str,
        commands: list[PluginCommandDef],
        plugin_name: str,
        lexer: Lexer | None = None,
    ) -> MatchResult | None:
        """尝试将文本匹配到 Plugin 的指令定义。

        Args:
            text: 用户输入文本
            commands: 插件的指令定义列表
            plugin_name: 插件名（用于返回 MatchResult）
            lexer: 可选的 Lexer 实例（用于精确指令解析）

        Returns:
            MatchResult 或 None
        """
        text_stripped = text.strip()

        for cmd in commands:
            for pattern in cmd.patterns:
                if cmd.pattern_type == "prefix":
                    if text_stripped.startswith(pattern):
                        # 精确指令：尝试词法解析
                        if lexer:
                            tokens = lexer.tokenize(text_stripped)
                            lexed = lexer.lex(tokens, raw_text=text_stripped)
                            return MatchResult(
                                plugin_name=plugin_name,
                                command_name=cmd.name,
                                pattern=pattern,
                                pattern_type="prefix",
                                confidence=1.0,
                                lexed=lexed,
                            )
                        return MatchResult(
                            plugin_name=plugin_name,
                            command_name=cmd.name,
                            pattern=pattern,
                            pattern_type="prefix",
                            confidence=1.0,
                        )

                elif cmd.pattern_type == "keyword":
                    if pattern in text_stripped:
                        return MatchResult(
                            plugin_name=plugin_name,
                            command_name=cmd.name,
                            pattern=pattern,
                            pattern_type="keyword",
                            confidence=0.9,
                        )

                elif cmd.pattern_type == "regex":
                    try:
                        if re.search(pattern, text_stripped):
                            return MatchResult(
                                plugin_name=plugin_name,
                                command_name=cmd.name,
                                pattern=pattern,
                                pattern_type="regex",
                                confidence=0.95,
                            )
                    except re.error as exc:
                        logger.warning("Plugin %s 的正则表达式无效: %s → %s", plugin_name, pattern, exc)

        return None


# ═══════════════════════════════════════════════════════════════════════
# 模块级便捷函数
# ═══════════════════════════════════════════════════════════════════════

# 共享的单例实例
_default_tokenizer = Tokenizer()
_default_lexer = Lexer(_default_tokenizer)
_default_parser = CommandParser()
_default_matcher = PluginMatcher()


def _apply_multiword_patterns(lexed: LexedCommand, plugin_def: PluginDefinition) -> None:
    """将多词 pattern 匹配到的 positional args 归入 command，不再作为参数。

    tokenizer 只认前缀后第一个词为 CMD_HEAD，导致 /ca analyse 被拆成
    command=ca + args=[analyse]。此函数在 lex 之后、parse 之前检查插件
    定义中的多词 pattern（如 "ca analyse"），若 command + 前 N 个 positional
    args 恰好匹配某个 pattern，则消费掉这些 args。

    支持多层嵌套指令组：
    - /ca analyse → command="ca", subcommand="analyse", subcommands=["analyse"]
    - /ca report daily → command="ca", subcommand="report", subcommands=["report", "daily"]
    """
    # 收集所有含空格的 prefix pattern（降序排列，优先匹配最长）
    multi_word: list[str] = []
    logger.info(
        "多词pattern检查: plugin=%s, commands=%d, lexed_cmd=%r, args=%r",
        plugin_def.name, len(plugin_def.commands),
        lexed.command, lexed.positional_args,
    )
    for cmd_def in plugin_def.commands:
        logger.info(
            "  cmd_def name=%r, type=%r, patterns=%r",
            cmd_def.name, cmd_def.pattern_type, cmd_def.patterns,
        )
        if cmd_def.pattern_type == "prefix":
            for pattern in cmd_def.patterns:
                if " " in pattern:
                    multi_word.append(pattern.lower())
    if not multi_word:
        logger.info("  无多词pattern，跳过")
        return
    multi_word.sort(key=len, reverse=True)
    logger.info("  多词patterns: %r", multi_word)

    for pattern in multi_word:
        parts = pattern.split()
        extra = len(parts) - 1  # command 已占第一个词
        if len(lexed.positional_args) < extra:
            continue
        candidate = lexed.command + " " + " ".join(
            a.lower() for a in lexed.positional_args[:extra]
        )
        logger.info("  尝试匹配: candidate=%r vs pattern=%r", candidate, pattern)
        if candidate == pattern:
            # 设置子命令路径
            if extra >= 1:
                consumed_args = [a.lower() for a in lexed.positional_args[:extra]]
                lexed.subcommand = consumed_args[0]  # 第一级子命令（向后兼容）
                lexed.subcommands = consumed_args     # 完整子命令路径
            lexed.positional_args = lexed.positional_args[extra:]
            logger.info(
                "  ✓ 匹配成功，消费 %d 个 args，subcommand=%r，subcommands=%r，剩余: %r",
                extra, lexed.subcommand, lexed.subcommands, lexed.positional_args,
            )
            return


def parse_command(text: str, plugin_def: PluginDefinition) -> CommandAST | None:
    """便捷函数：将用户文本按 Plugin 定义解析为 CommandAST。

    Args:
        text: 用户输入文本
        plugin_def: Plugin 定义

    Returns:
        CommandAST 或 None（无法识别为有效指令）
    """
    tokens = _default_tokenizer.tokenize(text)
    lexed = _default_lexer.lex(tokens, raw_text=text)
    if lexed is None:
        return None
    _apply_multiword_patterns(lexed, plugin_def)
    return _default_parser.parse(lexed, plugin_def)


def match_plugin(text: str, plugin_def: PluginDefinition) -> MatchResult | None:
    """便捷函数：尝试将文本匹配到 Plugin 的触发词。

    Args:
        text: 用户输入文本
        plugin_def: Plugin 定义

    Returns:
        MatchResult 或 None
    """
    return _default_matcher.match(
        text, plugin_def.commands, plugin_def.name, lexer=_default_lexer
    )
