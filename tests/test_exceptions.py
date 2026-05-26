"""异常体系测试。"""
from __future__ import annotations

import pytest

from sirius_pulse.exceptions import (
    ConflictingMemoryError,
    ConfigError,
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


class TestSiriusException:
    """基础异常测试。"""

    def test_basic_construction(self):
        exc = SiriusException("测试错误")
        assert str(exc) == "测试错误"
        assert exc.error_code == "UNKNOWN"
        assert exc.context == {}
        assert exc.is_retryable is False

    def test_with_error_code(self):
        exc = SiriusException("错误", error_code="TEST_ERR")
        assert exc.error_code == "TEST_ERR"

    def test_with_context(self):
        exc = SiriusException("错误", context={"key": "value"})
        assert exc.context["key"] == "value"

    def test_to_dict(self):
        exc = SiriusException("错误", error_code="ERR", context={"a": 1})
        d = exc.to_dict()
        assert d["exception_type"] == "SiriusException"
        assert d["message"] == "错误"
        assert d["error_code"] == "ERR"
        assert d["context"] == {"a": 1}
        assert d["is_retryable"] is False

    def test_repr(self):
        exc = SiriusException("test", error_code="ERR")
        r = repr(exc)
        assert "SiriusException" in r
        assert "test" in r


class TestProviderExceptions:
    """Provider 异常测试。"""

    def test_connection_error_is_retryable(self):
        exc = ProviderConnectionError("openai", "连接超时")
        assert exc.is_retryable is True
        assert exc.provider_name == "openai"
        assert exc.error_code == "PROVIDER_CONNECTION_ERROR"

    def test_connection_error_with_original(self):
        original = TimeoutError("timeout")
        exc = ProviderConnectionError("deepseek", "失败", original_error=original, retry_count=3)
        assert exc.original_error is original
        assert exc.retry_count == 3
        assert exc.context["retry_count"] == 3

    def test_auth_error_not_retryable(self):
        exc = ProviderAuthError("openai", "API Key 无效", http_status=401)
        assert exc.is_retryable is False
        assert exc.http_status == 401

    def test_response_error_retryable_on_429(self):
        exc = ProviderResponseError("openai", "限流", http_status=429)
        assert exc.is_retryable is True

    def test_response_error_retryable_on_503(self):
        exc = ProviderResponseError("openai", "服务不可用", http_status=503)
        assert exc.is_retryable is True

    def test_response_error_not_retryable_on_400(self):
        exc = ProviderResponseError("openai", "参数错误", http_status=400)
        assert exc.is_retryable is False

    def test_response_error_with_body(self):
        body = '{"error": "bad request"}'
        exc = ProviderResponseError("openai", "错误", response_body=body)
        assert exc.response_body == body
        assert exc.context["response_preview"] == body

    def test_response_error_long_body_truncated(self):
        body = "x" * 500
        exc = ProviderResponseError("openai", "错误", response_body=body)
        assert len(exc.context["response_preview"]) == 200

    def test_all_provider_errors_inherit(self):
        for cls in [ProviderConnectionError, ProviderAuthError, ProviderResponseError]:
            assert issubclass(cls, ProviderError)
            assert issubclass(cls, SiriusException)


class TestTokenExceptions:
    """Token 异常测试。"""

    def test_budget_exceeded(self):
        exc = TokenBudgetExceededError("chat_main", budget=1000, used=900, requested=200)
        assert exc.task_name == "chat_main"
        assert exc.budget == 1000
        assert exc.used == 900
        assert exc.requested == 200
        assert exc.error_code == "TOKEN_BUDGET_EXCEEDED"
        assert exc.is_retryable is False
        assert exc.context["deficit"] == 100

    def test_estimation_error(self):
        exc = TokenEstimationError("估算失败")
        assert exc.error_code == "TOKEN_ESTIMATION_ERROR"
        assert exc.is_retryable is False

    def test_token_errors_inherit(self):
        assert issubclass(TokenBudgetExceededError, TokenError)
        assert issubclass(TokenEstimationError, TokenError)
        assert issubclass(TokenError, SiriusException)


class TestParseExceptions:
    """解析异常测试。"""

    def test_json_parse_error(self):
        raw = '{"broken":'
        exc = JSONParseError(raw, "JSON 解析失败", expected_schema="Config")
        assert exc.raw_content == raw
        assert exc.expected_schema == "Config"
        assert exc.error_code == "JSON_PARSE_ERROR"

    def test_json_parse_error_long_content_truncated(self):
        raw = "x" * 200
        exc = JSONParseError(raw, "解析失败")
        assert len(exc.context["raw_content_preview"]) == 100

    def test_content_validation_error(self):
        exc = ContentValidationError("字段缺失", field="name", expected_type="str")
        assert exc.field == "name"
        assert exc.expected_type == "str"
        assert exc.error_code == "CONTENT_VALIDATION_ERROR"

    def test_parse_errors_inherit(self):
        assert issubclass(JSONParseError, ParseError)
        assert issubclass(ContentValidationError, ParseError)
        assert issubclass(ParseError, SiriusException)


class TestConfigExceptions:
    """配置异常测试。"""

    def test_invalid_config(self):
        exc = InvalidConfigError("sensitivity", "值超出范围", provided_value=2.0, expected_format="0~1")
        assert exc.config_key == "sensitivity"
        assert exc.provided_value == 2.0
        assert exc.expected_format == "0~1"
        assert exc.error_code == "INVALID_CONFIG"

    def test_missing_config(self):
        exc = MissingConfigError("api_key", "缺少 API Key")
        assert exc.config_key == "api_key"
        assert exc.error_code == "MISSING_CONFIG"

    def test_orchestration_config_error_auto_message(self):
        missing = {"memory_extract": ["gpt-4o-mini"], "response_generate": ["gpt-4o"]}
        exc = OrchestrationConfigError(missing)
        assert "memory_extract" in str(exc)
        assert exc.missing_models == missing

    def test_orchestration_config_error_custom_message(self):
        missing = {"task": ["model"]}
        exc = OrchestrationConfigError(missing, message="自定义消息")
        assert str(exc) == "自定义消息"

    def test_config_errors_inherit(self):
        assert issubclass(InvalidConfigError, ConfigError)
        assert issubclass(MissingConfigError, ConfigError)
        assert issubclass(OrchestrationConfigError, ConfigError)
        assert issubclass(ConfigError, SiriusException)


class TestMemoryExceptions:
    """记忆异常测试。"""

    def test_user_not_found(self):
        exc = UserNotFoundError("user_123")
        assert exc.user_id == "user_123"
        assert exc.error_code == "USER_NOT_FOUND"
        assert "user_123" in str(exc)

    def test_conflicting_memory(self):
        exc = ConflictingMemoryError("u1", ["fact_1", "fact_2"], "记忆冲突")
        assert exc.user_id == "u1"
        assert exc.fact_ids == ["fact_1", "fact_2"]
        assert exc.error_code == "CONFLICTING_MEMORY"

    def test_memory_errors_inherit(self):
        assert issubclass(UserNotFoundError, MemoryError)
        assert issubclass(ConflictingMemoryError, MemoryError)
        assert issubclass(MemoryError, SiriusException)
