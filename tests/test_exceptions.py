"""异常体系面向运维诊断和业务恢复的行为测试。"""

from __future__ import annotations

from sirius_pulse.exceptions import (
    ConfigError,
    ConflictingMemoryError,
    ContentValidationError,
    InvalidConfigError,
    JSONParseError,
    MemoryError,
    MissingConfigError,
    OrchestrationConfigError,
    ParseError,
    ProviderAuthError,
    ProviderConnectionError,
    ProviderError,
    ProviderResponseError,
    SiriusException,
    TokenBudgetExceededError,
    TokenError,
    TokenEstimationError,
    UserNotFoundError,
)


def test_exception_when_logged_then_contains_stable_code_context_and_retry_hint():
    exc = SiriusException(
        "业务失败",
        error_code="BUSINESS_FAILED",
        context={"group_id": "group_a"},
        is_retryable=True,
    )

    payload = exc.to_dict()

    assert payload == {
        "exception_type": "SiriusException",
        "message": "业务失败",
        "error_code": "BUSINESS_FAILED",
        "context": {"group_id": "group_a"},
        "is_retryable": True,
    }
    assert "BUSINESS_FAILED" in repr(exc)


def test_provider_error_when_network_times_out_then_runtime_knows_it_can_retry():
    original = TimeoutError("timeout")

    exc = ProviderConnectionError(
        "openai",
        "连接超时",
        original_error=original,
        retry_count=2,
    )

    assert exc.is_retryable is True
    assert exc.provider_name == "openai"
    assert exc.original_error is original
    assert exc.context["retry_count"] == 2
    assert isinstance(exc, ProviderError)


def test_provider_error_when_api_key_is_wrong_then_runtime_does_not_retry():
    exc = ProviderAuthError("openai", "API Key 无效", http_status=401)

    assert exc.is_retryable is False
    assert exc.http_status == 401
    assert exc.error_code == "PROVIDER_AUTH_ERROR"


def test_provider_error_when_rate_limited_then_retry_is_allowed_and_body_is_truncated():
    exc = ProviderResponseError("openai", "限流", http_status=429, response_body="x" * 500)

    assert exc.is_retryable is True
    assert exc.context["http_status"] == 429
    assert len(exc.context["response_preview"]) == 200


def test_provider_error_when_bad_request_then_runtime_surfaces_non_retryable_failure():
    exc = ProviderResponseError("openai", "参数错误", http_status=400)

    assert exc.is_retryable is False
    assert exc.provider_name == "openai"


def test_token_error_when_budget_is_exceeded_then_deficit_is_visible_to_budget_ui():
    exc = TokenBudgetExceededError("chat_main", budget=1000, used=900, requested=200)

    assert exc.error_code == "TOKEN_BUDGET_EXCEEDED"
    assert exc.is_retryable is False
    assert exc.context["deficit"] == 100
    assert isinstance(exc, TokenError)


def test_token_error_when_estimation_fails_then_error_is_non_retryable():
    exc = TokenEstimationError("估算失败")

    assert exc.error_code == "TOKEN_ESTIMATION_ERROR"
    assert exc.is_retryable is False


def test_parse_error_when_llm_returns_broken_json_then_preview_and_schema_are_kept():
    raw = "{" + "x" * 200

    exc = JSONParseError(raw, "JSON 解析失败", expected_schema="IntentAnalysis")

    assert exc.raw_content == raw
    assert exc.expected_schema == "IntentAnalysis"
    assert len(exc.context["raw_content_preview"]) == 100
    assert isinstance(exc, ParseError)


def test_parse_error_when_required_field_is_missing_then_field_name_is_reported():
    exc = ContentValidationError("字段缺失", field="name", expected_type="str")

    assert exc.field == "name"
    assert exc.expected_type == "str"
    assert exc.error_code == "CONTENT_VALIDATION_ERROR"


def test_config_error_when_admin_sets_invalid_value_then_expected_format_is_visible():
    exc = InvalidConfigError(
        "engagement_sensitivity",
        "值超出范围",
        provided_value=2.0,
        expected_format="0~1",
    )

    assert exc.config_key == "engagement_sensitivity"
    assert exc.provided_value == 2.0
    assert exc.context["expected_format"] == "0~1"
    assert isinstance(exc, ConfigError)


def test_config_error_when_provider_key_is_missing_then_missing_key_is_reported():
    exc = MissingConfigError("api_key", "缺少 API Key")

    assert exc.config_key == "api_key"
    assert exc.error_code == "MISSING_CONFIG"


def test_config_error_when_orchestration_models_are_missing_then_tasks_are_listed():
    missing = {"memory_extract": ["gpt-4o-mini"], "response_generate": ["gpt-4o"]}

    exc = OrchestrationConfigError(missing)

    assert "memory_extract" in str(exc)
    assert exc.missing_models == missing


def test_memory_error_when_user_is_unknown_then_user_id_is_kept_for_recovery():
    exc = UserNotFoundError("user_123")

    assert exc.user_id == "user_123"
    assert exc.context["user_id"] == "user_123"
    assert isinstance(exc, MemoryError)


def test_memory_error_when_facts_conflict_then_conflicting_fact_ids_are_visible():
    exc = ConflictingMemoryError("u1", ["fact_1", "fact_2"], "记忆冲突")

    assert exc.user_id == "u1"
    assert exc.fact_ids == ["fact_1", "fact_2"]
    assert exc.context["conflicting_facts"] == ["fact_1", "fact_2"]


def test_exception_hierarchy_when_handlers_catch_category_then_specific_errors_are_included():
    assert issubclass(ProviderConnectionError, ProviderError)
    assert issubclass(TokenBudgetExceededError, TokenError)
    assert issubclass(JSONParseError, ParseError)
    assert issubclass(InvalidConfigError, ConfigError)
    assert issubclass(UserNotFoundError, MemoryError)
    assert issubclass(MemoryError, SiriusException)
