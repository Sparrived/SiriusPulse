"""Token 估算工具测试。"""
from __future__ import annotations

from sirius_pulse.token.utils import (
    PromptTokenBreakdown,
    estimate_tokens,
    estimate_tokens_heuristic,
    get_token_estimation_stats,
)


class TestEstimateTokensHeuristic:
    """启发式 Token 估算测试。"""

    def test_empty_string_returns_zero(self):
        assert estimate_tokens_heuristic("") == 0

    def test_whitespace_only_returns_zero(self):
        assert estimate_tokens_heuristic("   ") == 0

    def test_pure_chinese_text(self):
        tokens = estimate_tokens_heuristic("你好世界")
        assert tokens >= 4

    def test_pure_english_text(self):
        tokens = estimate_tokens_heuristic("hello world")
        assert tokens >= 1

    def test_mixed_text(self):
        tokens = estimate_tokens_heuristic("Hello 你好 World 世界")
        assert tokens > 0

    def test_longer_text_has_more_tokens(self):
        short = estimate_tokens_heuristic("短")
        long = estimate_tokens_heuristic("这是一段比较长的中文文本，用于测试token估算的准确性")
        assert long > short

    def test_chinese_is_roughly_one_token_per_char(self):
        text = "测试文本"
        tokens = estimate_tokens_heuristic(text)
        assert abs(tokens - len(text)) <= 2

    def test_english_is_roughly_four_chars_per_token(self):
        text = "abcdefgh"
        tokens = estimate_tokens_heuristic(text)
        assert abs(tokens - 2) <= 1

    def test_japanese_kana_counted(self):
        tokens = estimate_tokens_heuristic("こんにちは")
        assert tokens >= 5

    def test_numbers_counted_as_other(self):
        tokens = estimate_tokens_heuristic("12345678")
        assert tokens >= 1


class TestEstimateTokens:
    """主入口 Token 估算测试（tiktoken 或 fallback）。"""

    def test_empty_string_returns_zero(self):
        assert estimate_tokens("") == 0

    def test_pure_whitespace_returns_zero(self):
        assert estimate_tokens("   ") == 0

    def test_chinese_text_positive(self):
        assert estimate_tokens("你好世界") > 0

    def test_english_text_positive(self):
        assert estimate_tokens("hello world") > 0

    def test_consistency_with_heuristic(self):
        text = "这是一段测试文本 hello world"
        main = estimate_tokens(text)
        heuristic = estimate_tokens_heuristic(text)
        assert abs(main - heuristic) <= max(5, heuristic * 0.2)


class TestTokenEstimationStats:
    """统计信息测试。"""

    def test_stats_keys(self):
        stats = get_token_estimation_stats("你好 hello")
        assert "total" in stats
        assert "heuristic" in stats
        assert "characters" in stats
        assert "chinese_count" in stats
        assert "english_count" in stats
        assert "other_count" in stats

    def test_stats_character_count(self):
        text = "abc你好"
        stats = get_token_estimation_stats(text)
        assert stats["characters"] == len(text)

    def test_stats_chinese_count(self):
        stats = get_token_estimation_stats("你好世界abc")
        assert stats["chinese_count"] == 4

    def test_stats_english_count(self):
        stats = get_token_estimation_stats("hello world 你好")
        assert stats["english_count"] == len("hello") + len("world")


class TestPromptTokenBreakdown:
    """PromptTokenBreakdown 序列化测试。"""

    def test_default_values(self):
        bd = PromptTokenBreakdown()
        assert bd.persona == 0
        assert bd.total == 0

    def test_to_dict(self):
        bd = PromptTokenBreakdown(persona=100, total=500)
        d = bd.to_dict()
        assert d["persona"] == 100
        assert d["total"] == 500
        assert isinstance(d, dict)

    def test_to_json_roundtrip(self):
        bd = PromptTokenBreakdown(persona=100, identity=50, total=800)
        json_str = bd.to_json()
        restored = PromptTokenBreakdown.from_json(json_str)
        assert restored.persona == 100
        assert restored.identity == 50
        assert restored.total == 800

    def test_to_dict_has_all_sections(self):
        bd = PromptTokenBreakdown()
        d = bd.to_dict()
        expected_keys = {
            "persona", "identity", "output_constraint", "emotion",
            "empathy", "relationship", "memory", "interests",
            "group_style", "participants", "cross_group", "skills",
            "glossary", "output_format", "diary", "history_xml",
            "cross_group_xml", "user_message", "output_total",
            "system_prompt_total", "total",
        }
        assert expected_keys.issubset(set(d.keys()))
