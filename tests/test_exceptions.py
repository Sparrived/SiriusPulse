"""异常体系面向运维诊断和业务恢复的行为测试。"""

from __future__ import annotations

from sirius_pulse.exceptions import (
    JSONParseError,
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


def test_config_error_when_orchestration_models_are_missing_then_tasks_are_listed():
    missing = {"memory_extract": ["gpt-4o-mini"], "response_generate": ["gpt-4o"]}

    exc = OrchestrationConfigError(missing)

    assert "memory_extract" in str(exc)
    assert exc.missing_models == missing
