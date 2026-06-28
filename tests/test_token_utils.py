"""Token 预算估算在 Prompt 构建中的业务行为测试。"""

from __future__ import annotations

from sirius_pulse.token.utils import (
    PromptTokenBreakdown,
    estimate_tokens,
    estimate_tokens_heuristic,
    get_token_estimation_stats,
)


def test_token_estimator_when_prompt_is_empty_then_budget_cost_is_zero():
    assert estimate_tokens("") == 0
    assert estimate_tokens("   ") == 0
    assert estimate_tokens_heuristic("") == 0


def test_token_estimator_when_prompt_contains_chinese_then_counts_cjk_text():
    tokens = estimate_tokens_heuristic("你好世界")

    assert tokens >= 4


def test_token_estimator_when_prompt_contains_english_then_counts_word_budget():
    tokens = estimate_tokens_heuristic("hello world")

    assert tokens >= 1
    assert estimate_tokens_heuristic("abcdefgh") <= 3


def test_token_estimator_when_prompt_gets_longer_then_budget_increases():
    short = estimate_tokens_heuristic("短")
    long = estimate_tokens_heuristic("这是一段比较长的中文文本，用于测试 token 估算的预算变化")

    assert long > short


def test_token_estimator_when_prompt_is_multilingual_then_returns_positive_budget():
    tokens = estimate_tokens("Hello 你好 こんにちは 123")

    assert tokens > 0


def test_token_stats_when_debugging_prompt_then_language_counts_are_visible():
    stats = get_token_estimation_stats("hello world 你好")

    assert stats["characters"] == len("hello world 你好")
    assert stats["chinese_count"] == 2
    assert stats["english_count"] == len("hello") + len("world")
    assert stats["total"] is not None
    assert stats["heuristic"] > 0


def test_prompt_breakdown_when_budget_panel_serializes_then_all_sections_are_present():
    breakdown = PromptTokenBreakdown(persona=100, memory=50, user_message=20, total=170)

    payload = breakdown.to_dict()

    assert payload["persona"] == 100
    assert payload["memory"] == 50
    assert payload["user_message"] == 20
    assert payload["total"] == 170
    assert {
        "identity",
        "skills",
        "diary",
        "history_xml",
        "system_prompt_total",
        "output_total",
    }.issubset(payload)


def test_prompt_breakdown_when_saved_to_json_then_can_be_loaded_for_audit():
    original = PromptTokenBreakdown(persona=100, identity=50, total=800)

    restored = PromptTokenBreakdown.from_json(original.to_json())

    assert restored.persona == 100
    assert restored.identity == 50
    assert restored.total == 800
