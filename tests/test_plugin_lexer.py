"""用户插件命令文本的解析行为测试。"""

from __future__ import annotations

from sirius_pulse.plugins.lexer import Lexer, Tokenizer, TokenType


def _lex(text: str):
    lexer = Lexer()
    return lexer.lex(lexer.tokenize(text), text)


def test_command_parser_when_user_enters_slash_command_then_extracts_command_and_argument():
    command = _lex("/dice 100")

    assert command is not None
    assert command.command == "dice"
    assert command.prefix == "/"
    assert command.positional_args == ["100"]


def test_command_parser_when_user_passes_named_option_then_option_is_available_to_plugin():
    command = _lex("/weather Beijing --format=json --verbose")

    assert command is not None
    assert command.command == "weather"
    assert command.positional_args == ["Beijing"]
    assert command.named_args["format"] == "json"
    assert "verbose" in command.flags


def test_command_parser_when_user_uses_short_option_then_plugin_receives_named_value():
    command = _lex("/image resize -w 512 -h 256")

    assert command is not None
    assert command.command == "image"
    assert command.positional_args == ["resize"]
    assert command.named_args == {"w": "512", "h": "256"}


def test_command_parser_when_user_mentions_someone_then_mention_becomes_argument():
    command = _lex("/remind @alice 18:00")

    assert command is not None
    assert command.positional_args == ["alice", "18:00"]


def test_command_parser_when_message_is_normal_chat_then_no_plugin_command_is_created():
    tokens = Tokenizer().tokenize("今天聊点什么")
    command = Lexer().lex(tokens, "今天聊点什么")

    assert command is None


def test_tokenizer_when_user_enters_long_option_then_keeps_structured_tokens():
    tokens = [
        token.type
        for token in Tokenizer().tokenize("/deploy app --force")
        if token.type != TokenType.WS
    ]

    assert tokens == [TokenType.CMD_HEAD, TokenType.ARG_VALUE, TokenType.LONG_OPT]
