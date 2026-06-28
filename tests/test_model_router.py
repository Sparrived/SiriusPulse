"""模型路由在业务任务调度中的行为测试。"""

from __future__ import annotations

from sirius_pulse.core.model_router import ModelRouter, TaskConfig


def test_model_router_when_generating_final_reply_then_uses_high_quality_model():
    config = ModelRouter().resolve("response_generate")

    assert config.model_name == "gpt-4o"
    assert config.temperature == 0.7
    assert config.max_tokens == 4096


def test_model_router_when_running_lightweight_cognition_then_uses_fast_model():
    config = ModelRouter().resolve("cognition_analyze")

    assert config.model_name == "gpt-4o-mini"
    assert config.timeout == 15.0


def test_model_router_when_task_is_unknown_then_falls_back_to_reply_generation_config():
    config = ModelRouter().resolve("unknown_business_task")

    assert config.model_name == "gpt-4o"
    assert config.max_tokens == 4096


def test_model_router_when_operator_overrides_reply_model_then_override_is_respected():
    router = ModelRouter(overrides={"response_generate": {"model_name": "custom-reply-model"}})

    config = router.resolve("response_generate")

    assert config.model_name == "custom-reply-model"
    assert config.temperature == 0.7


def test_model_router_when_operator_changes_temperature_then_other_limits_are_preserved():
    router = ModelRouter(overrides={"response_generate": {"temperature": 0.2}})

    config = router.resolve("response_generate")

    assert config.temperature == 0.2
    assert config.max_tokens == 4096
    assert config.timeout == 30.0


def test_model_router_when_custom_business_task_is_registered_then_it_can_be_resolved():
    router = ModelRouter(
        task_registry={
            "support_triage": TaskConfig(
                model_name="support-model",
                temperature=0.1,
                max_tokens=800,
            )
        }
    )

    config = router.resolve("support_triage")

    assert config.model_name == "support-model"
    assert config.temperature == 0.1
    assert router.list_tasks() == ["support_triage"]


def test_model_router_when_cognition_is_urgent_then_model_is_escalated():
    router = ModelRouter()

    normal = router.resolve("cognition_analyze", urgency=50)
    urgent = router.resolve("cognition_analyze", urgency=85)

    assert normal.model_name == "gpt-4o-mini"
    assert urgent.model_name == "gpt-4o"
    assert urgent.temperature < normal.temperature
    assert urgent.max_tokens >= normal.max_tokens


def test_model_router_when_task_is_critical_then_tokens_are_not_reduced():
    router = ModelRouter()

    normal = router.resolve("cognition_analyze", urgency=50)
    critical = router.resolve("cognition_analyze", urgency=96)

    assert critical.model_name == "gpt-4o"
    assert critical.max_tokens >= normal.max_tokens


def test_model_router_when_primary_provider_fails_then_task_has_fallback_model():
    fallback = ModelRouter().get_fallback("response_generate")

    assert fallback is not None
    assert fallback.model_name == "deepseek-reasoner"
    assert fallback.temperature == 0.7


def test_model_router_when_task_has_no_config_then_no_fallback_is_returned():
    assert ModelRouter().get_fallback("missing_task") is None


def test_model_router_when_mapping_to_stronger_model_then_known_tiers_upgrade():
    assert ModelRouter._stronger_model("gpt-4o-mini") == "gpt-4o"
    assert ModelRouter._stronger_model("deepseek-chat") == "deepseek-reasoner"
    assert ModelRouter._stronger_model("private-model") == "private-model"
