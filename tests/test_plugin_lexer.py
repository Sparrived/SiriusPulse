"""命令解析（Tokenizer + Lexer）关键路径测试。"""
from __future__ import annotations

from sirius_pulse.plugins.lexer import Tokenizer, Lexer, TokenType


def test_tokenize_simple_command():
    """简单命令 Tokenize。"""
    tokenizer = Tokenizer()
    tokens = tokenizer.tokenize("/dice 100")
    types = [t.type for t in tokens if t.type != TokenType.WS]
    assert types == [TokenType.CMD_HEAD, TokenType.ARG_VALUE]


def test_tokenize_with_options():
    """带选项命令 Tokenize。"""
    tokenizer = Tokenizer()
    tokens = tokenizer.tokenize("/weather Beijing --format=json")
    types = [t.type for t in tokens if t.type != TokenType.WS]
    assert types == [
        TokenType.CMD_HEAD,
        TokenType.ARG_VALUE,
        TokenType.LONG_OPT,
        TokenType.EQ,
        TokenType.ARG_VALUE,
    ]


def test_tokenize_with_flags():
    """带布尔标志命令 Tokenize。"""
    tokenizer = Tokenizer()
    tokens = tokenizer.tokenize("/deploy app --force")
    types = [t.type for t in tokens if t.type != TokenType.WS]
    assert TokenType.LONG_OPT in types
    assert TokenType.ARG_VALUE in types


def test_lex_simple_command():
    """简单命令 Lex。"""
    lexer = Lexer()
    tokenizer = Tokenizer()
    tokens = tokenizer.tokenize("/dice 100")
    lexed = lexer.lex(tokens, "/dice 100")
    assert lexed is not None
    assert lexed.command == "dice"
    assert lexed.positional_args == ["100"]
    assert lexed.prefix == "/"


def test_lex_with_named_args():
    """命名参数 Lex。"""
    lexer = Lexer()
    tokenizer = Tokenizer()
    tokens = tokenizer.tokenize("/weather Beijing --format=json --verbose")
    lexed = lexer.lex(tokens, "/weather Beijing --format=json --verbose")
    assert lexed is not None
    assert lexed.command == "weather"
    assert lexed.positional_args == ["Beijing"]
    assert lexed.named_args.get("format") == "json"
    assert "verbose" in lexed.flags


def test_no_prefix_no_match():
    """无前缀的消息不生成 LexedCommand。"""
    lexer = Lexer()
    tokenizer = Tokenizer()
    tokens = tokenizer.tokenize("你好")
    lexed = lexer.lex(tokens, "你好")
    assert lexed is None
