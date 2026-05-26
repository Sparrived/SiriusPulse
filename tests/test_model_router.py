"""ModelRouter 模型路由测试。"""
from __future__ import annotations

from sirius_pulse.core.model_router import ModelRouter, TaskConfig


class TestBasicResolve:
    """基础路由解析。"""

    def test_resolve_known_task(self):
        router = ModelRouter()
        cfg = router.resolve("response_generate")
        assert cfg.model_name == "gpt-4o"
        assert cfg.temperature == 0.7
        assert cfg.max_tokens == 4096

    def test_resolve_unknown_task_falls_back(self):
        router = ModelRouter()
        cfg = router.resolve("nonexistent_task")
        assert cfg.model_name == "gpt-4o"

    def test_resolve_with_custom_registry(self):
        registry = {
            "my_task": TaskConfig(model_name="custom-model", temperature=0.5, max_tokens=1024)
        }
        router = ModelRouter(task_registry=registry)
        cfg = router.resolve("my_task")
        assert cfg.model_name == "custom-model"

    def test_list_tasks(self):
        router = ModelRouter()
        tasks = router.list_tasks()
        assert "response_generate" in tasks
        assert "cognition_analyze" in tasks
        assert len(tasks) > 5


class TestOverrides:
    """参数覆盖测试。"""

    def test_override_temperature(self):
        router = ModelRouter(overrides={"response_generate": {"temperature": 0.3}})
        cfg = router.resolve("response_generate")
        assert cfg.temperature == 0.3
        assert cfg.model_name == "gpt-4o"

    def test_override_model_name(self):
        router = ModelRouter(overrides={"response_generate": {"model_name": "gpt-4o-mini"}})
        cfg = router.resolve("response_generate")
        assert cfg.model_name == "gpt-4o-mini"

    def test_override_preserves_unspecified_fields(self):
        router = ModelRouter(overrides={"response_generate": {"temperature": 0.2}})
        cfg = router.resolve("response_generate")
        assert cfg.temperature == 0.2
        assert cfg.max_tokens == 4096
        assert cfg.timeout == 30.0

    def test_override_nonexistent_task_ignored(self):
        router = ModelRouter(overrides={"ghost_task": {"temperature": 0.1}})
        cfg = router.resolve("response_generate")
        assert cfg.temperature == 0.7


class TestUrgencyEscalation:
    """紧急度升级测试。"""

    def test_no_escalation_low_urgency(self):
        router = ModelRouter()
        cfg = router.resolve("cognition_analyze", urgency=50)
        assert cfg.model_name == "gpt-4o-mini"

    def test_escalation_high_urgency(self):
        router = ModelRouter()
        cfg = router.resolve("cognition_analyze", urgency=85)
        assert cfg.model_name == "gpt-4o"

    def test_escalation_critical_urgency(self):
        router = ModelRouter()
        cfg = router.resolve("cognition_analyze", urgency=96)
        assert cfg.model_name == "gpt-4o"

    def test_escalation_lowers_temperature(self):
        router = ModelRouter()
        normal = router.resolve("cognition_analyze", urgency=50)
        high = router.resolve("cognition_analyze", urgency=85)
        critical = router.resolve("cognition_analyze", urgency=96)
        assert critical.temperature <= high.temperature <= normal.temperature

    def test_escalation_increases_tokens(self):
        router = ModelRouter()
        normal = router.resolve("cognition_analyze", urgency=50)
        critical = router.resolve("cognition_analyze", urgency=96)
        assert critical.max_tokens >= normal.max_tokens


class TestStrongerModel:
    """模型升级映射测试。"""

    def test_gpt4o_mini_upgrades_to_gpt4o(self):
        assert ModelRouter._stronger_model("gpt-4o-mini") == "gpt-4o"

    def test_deepseek_chat_upgrades_to_reasoner(self):
        assert ModelRouter._stronger_model("deepseek-chat") == "deepseek-reasoner"

    def test_unknown_model_unchanged(self):
        assert ModelRouter._stronger_model("custom-model") == "custom-model"


class TestFallback:
    """回退模型测试。"""

    def test_get_fallback_for_known_task(self):
        router = ModelRouter()
        fallback = router.get_fallback("cognition_analyze")
        assert fallback is not None
        assert fallback.model_name == "deepseek-chat"

    def test_get_fallback_for_unknown_task(self):
        router = ModelRouter()
        fallback = router.get_fallback("nonexistent")
        assert fallback is None

    def test_fallback_preserves_config(self):
        router = ModelRouter()
        fallback = router.get_fallback("response_generate")
        assert fallback is not None
        assert fallback.temperature == 0.7
        assert fallback.max_tokens == 4096
