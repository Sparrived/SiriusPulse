"""
Token估算工具的单元测试
"""

from __future__ import annotations

import pytest

from sirius_pulse.token.utils import (
    estimate_tokens,
    estimate_tokens_heuristic,
    get_token_estimation_stats,
)


def test_estimate_tokens_empty_string() -> None:
    """空字符串应返回0"""
    assert estimate_tokens("") == 0
    assert estimate_tokens("   ") == 0


def test_estimate_tokens_english() -> None:
    """英文文本估算"""
    # "Hello world" ≈ 2-3 tokens
    tokens = estimate_tokens("Hello world")
    assert tokens > 0
    assert tokens <= 5


def test_estimate_tokens_chinese() -> None:
    """中文文本估算"""
    # "你好世界" ≈ 4 tokens (汉字)
    tokens = estimate_tokens("你好世界")
    assert tokens >= 4
    assert tokens <= 6


def test_estimate_tokens_mixed() -> None:
    """混合英文和中文文本"""
    tokens = estimate_tokens("Hello 世界")
    assert tokens > 0


def test_estimate_tokens_heuristic_consistency() -> None:
    """启发式估算的一致性"""
    text = "This is a test. 这是一个测试。"
    tokens1 = estimate_tokens_heuristic(text)
    tokens2 = estimate_tokens_heuristic(text)
    assert tokens1 == tokens2


def test_get_token_estimation_stats() -> None:
    """获取token估算统计信息"""
    text = "Hello 世界"
    stats = get_token_estimation_stats(text)

    assert "total" in stats
    assert "characters" in stats
    assert "chinese_count" in stats
    assert "english_count" in stats

    # 验证统计数据
    assert stats["characters"] == len(text)
    assert stats["chinese_count"] == 2  # 世界 (两个中文字）
    assert stats["english_count"] == 5  # Hello


def test_estimate_tokens_with_different_models() -> None:
    """不同模型的估算应该一致（目前使用相同参数）"""
    text = "The quick brown fox"
    tokens_generic = estimate_tokens(text, model="generic")
    tokens_gpt4 = estimate_tokens(text, model="gpt-4")

    # 目前使用相同参数，所以应该相同
    assert tokens_generic == tokens_gpt4


def test_estimate_tokens_numbers_and_punctuation() -> None:
    """数字和标点符号的估算"""
    # 测试包含数字和标点
    tokens = estimate_tokens("Hello, world! 123")
    assert tokens > 0


def test_estimate_tokens_long_text() -> None:
    """长文本的估算"""
    long_text = "Hello world " * 100
    tokens = estimate_tokens(long_text)

    # 应该与字符数成比例
    short_tokens = estimate_tokens("Hello world")
    assert tokens > short_tokens * 50  # 至少是短文本的50倍



