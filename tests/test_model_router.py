"""Tests for ModelRouter (v0.28 task-aware model selection)."""

from __future__ import annotations

import pytest

from sirius_pulse.core.model_router import ModelRouter, TaskConfig


class TestModelRouterDefaults:
    def test_list_tasks(self):
        router = ModelRouter()
        tasks = router.list_tasks()
        assert "cognition_analyze" in tasks
        assert "response_generate" in tasks
        assert "proactive_generate" in tasks

    def test_cognition_task_is_lightweight(self):
        router = ModelRouter()
        cfg = router.resolve("cognition_analyze")
        assert cfg.max_tokens <= 512
        assert cfg.temperature <= 0.5

    def test_response_task_is_stronger(self):
        router = ModelRouter()
        cfg = router.resolve("response_generate")
        assert cfg.max_tokens >= 512
        assert cfg.temperature >= 0.5


class TestUrgencyEscalation:
    def test_critical_urgency_escalates_model(self):
        router = ModelRouter()
        normal = router.resolve("response_generate", urgency=50)
        critical = router.resolve("response_generate", urgency=99)
        assert critical.model_name != normal.model_name
        assert critical.temperature < normal.temperature
        assert critical.max_tokens > normal.max_tokens

    def test_high_urgency_escalates(self):
        router = ModelRouter()
        normal = router.resolve("response_generate", urgency=50)
        high = router.resolve("response_generate", urgency=85)
        assert high.model_name != normal.model_name
        assert high.temperature < normal.temperature

    def test_low_urgency_no_escalation(self):
        router = ModelRouter()
        normal = router.resolve("response_generate", urgency=50)
        low = router.resolve("response_generate", urgency=30)
        assert low.model_name == normal.model_name
        assert low.temperature == normal.temperature


class TestHeatAdaptation:
    def test_heat_does_not_affect_max_tokens(self):
        """热度不再缩减 max_tokens，长度控制移至 prompt 指令层。"""
        router = ModelRouter()
        normal = router.resolve("response_generate", heat_level="warm")
        hot = router.resolve("response_generate", heat_level="overheated")
        assert hot.max_tokens == normal.max_tokens


class TestOverrides:
    def test_custom_override_applied(self):
        router = ModelRouter(overrides={
            "response_generate": {"temperature": 0.5, "max_tokens": 300},
        })
        cfg = router.resolve("response_generate")
        assert cfg.temperature == 0.5
        assert cfg.max_tokens == 300

    def test_partial_override_preserves_others(self):
        router = ModelRouter(overrides={
            "cognition_analyze": {"model_name": "custom-model"},
        })
        cfg = router.resolve("cognition_analyze")
        assert cfg.model_name == "custom-model"
        assert cfg.temperature == 0.3  # unchanged


class TestFallback:
    def test_fallback_returns_config(self):
        router = ModelRouter()
        fb = router.get_fallback("cognition_analyze")
        assert fb is not None
        assert fb.model_name == "deepseek-chat"

    def test_unknown_task_uses_response_generate_defaults(self):
        router = ModelRouter()
        cfg = router.resolve("nonexistent_task")
        assert cfg.model_name == "gpt-4o"


class TestStrongerModel:
    def test_gpt4o_mini_to_gpt4o(self):
        router = ModelRouter()
        assert router._stronger_model("gpt-4o-mini") == "gpt-4o"

    def test_deepseek_chat_to_reasoner(self):
        router = ModelRouter()
        assert router._stronger_model("deepseek-chat") == "deepseek-reasoner"

    def test_unknown_model_unchanged(self):
        router = ModelRouter()
        assert router._stronger_model("custom-model") == "custom-model"
