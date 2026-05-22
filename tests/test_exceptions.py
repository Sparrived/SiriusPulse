"""
自定义异常模块的单元测试
"""

from __future__ import annotations

import pytest

from sirius_pulse.exceptions import (
    ContentValidationError,
    InvalidConfigError,
    JSONParseError,
    ProviderConnectionError,
    SiriusException,
    TokenBudgetExceededError,
    UserNotFoundError,
)


def test_sirius_exception_base() -> None:
    """基础异常正确初始化"""
    exc = SiriusException(
        "Test error",
        error_code="TEST_ERROR",
        context={"key": "value"},
        is_retryable=True,
    )

    assert exc.message == "Test error"
    assert exc.error_code == "TEST_ERROR"
    assert exc.context == {"key": "value"}
    assert exc.is_retryable is True


def test_sirius_exception_to_dict() -> None:
    """异常转换为字典"""
    exc = SiriusException(
        "Test error",
        error_code="TEST",
        context={"detail": "some detail"},
    )

    exc_dict = exc.to_dict()
    assert exc_dict["exception_type"] == "SiriusException"
    assert exc_dict["message"] == "Test error"
    assert exc_dict["error_code"] == "TEST"
    assert exc_dict["context"]["detail"] == "some detail"


def test_provider_connection_error() -> None:
    """Provider连接错误"""
    original_error = ConnectionError("Network timeout")
    exc = ProviderConnectionError(
        "openai",
        "Failed to connect to OpenAI",
        original_error=original_error,
        retry_count=2,
    )

    assert exc.provider_name == "openai"
    assert exc.is_retryable is True
    assert exc.context["retry_count"] == 2
    assert "openai" in str(exc.context["provider"])


def test_token_budget_exceeded_error() -> None:
    """Token预算超限"""
    exc = TokenBudgetExceededError(
        task_name="memory_extract",
        budget=1000,
        used=800,
        requested=300,
    )

    assert exc.task_name == "memory_extract"
    assert exc.budget == 1000
    assert exc.used == 800
    assert exc.is_retryable is False
    assert exc.context["deficit"] == 100  # 300 - (1000 - 800)


def test_json_parse_error() -> None:
    """JSON解析错误"""
    exc = JSONParseError(
        '{"incomplete": ',
        "Invalid JSON",
        expected_schema="MemoryFact",
    )

    assert exc.expected_schema == "MemoryFact"
    assert exc.is_retryable is False
    assert "{" in exc.context["raw_content_preview"]


def test_content_validation_error() -> None:
    """内容验证错误"""
    exc = ContentValidationError(
        "Missing required field",
        field="user_id",
        expected_type="str",
    )

    assert exc.field == "user_id"
    assert exc.expected_type == "str"
    assert exc.is_retryable is False


def test_invalid_config_error() -> None:
    """配置参数无效"""
    exc = InvalidConfigError(
        "max_tokens",
        "max_tokens must be positive",
        provided_value=-10,
        expected_format="positive integer",
    )

    assert exc.config_key == "max_tokens"
    assert exc.provided_value == -10
    assert exc.expected_format == "positive integer"


def test_user_not_found_error() -> None:
    """用户未找到"""
    exc = UserNotFoundError("unknown_user")

    assert exc.user_id == "unknown_user"
    assert exc.is_retryable is False
    assert "unknown_user" in exc.message


def test_exception_inheritance_chain() -> None:
    """异常继承链验证"""
    # ProviderConnectionError → ProviderError → SiriusException → Exception
    exc = ProviderConnectionError("test", "test error")

    assert isinstance(exc, ProviderConnectionError)
    assert isinstance(exc, SiriusException)
    assert isinstance(exc, Exception)


def test_exception_str_representation() -> None:
    """异常字符串表示"""
    exc = SiriusException("Test message", error_code="TEST")
    str_repr = str(exc)

    assert "Test message" in str_repr


def test_exception_context_dict_types() -> None:
    """异常context中的各种数据类型"""
    context = {
        "string": "value",
        "number": 42,
        "float": 3.14,
        "list": [1, 2, 3],
        "dict": {"nested": "value"},
        "none": None,
    }

    exc = SiriusException("Test", context=context)
    exc_dict = exc.to_dict()

    assert exc_dict["context"] == context
