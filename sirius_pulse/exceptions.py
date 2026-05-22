"""
自定义异常模块

提供结构化异常体系，便于上层精确处理不同类型的错误。
"""

from __future__ import annotations

from typing import Any


class SiriusException(Exception):
    """Sirius Chat的基础异常类

    所有自定义异常都继承自该类。包含以下属性：
    - error_code: 错误代码（用于国际化和自动处理）
    - context: 错误上下文信息（诊断用）
    - is_retryable: 是否可重试
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "UNKNOWN",
        context: dict[str, Any] | None = None,
        is_retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.context = context or {}
        self.is_retryable = is_retryable

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"error_code={self.error_code}, "
            f"is_retryable={self.is_retryable})"
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典，便于日志序列化"""
        return {
            "exception_type": self.__class__.__name__,
            "message": self.message,
            "error_code": self.error_code,
            "context": self.context,
            "is_retryable": self.is_retryable,
        }


class ProviderError(SiriusException):
    """Provider相关错误的基类

    包括网络连接、API响应、认证等问题。
    """

    pass


class ProviderConnectionError(ProviderError):
    """Provider连接失败（网络超时、连接拒绝等）"""

    def __init__(
        self,
        provider_name: str,
        message: str,
        *,
        original_error: Exception | None = None,
        retry_count: int = 0,
    ) -> None:
        context = {
            "provider": provider_name,
            "original_error": str(original_error),
            "retry_count": retry_count,
        }
        super().__init__(
            message,
            error_code="PROVIDER_CONNECTION_ERROR",
            context=context,
            is_retryable=True,
        )
        self.provider_name = provider_name
        self.original_error = original_error
        self.retry_count = retry_count


class ProviderAuthError(ProviderError):
    """Provider认证失败（API Key无效、权限不足等）"""

    def __init__(
        self,
        provider_name: str,
        message: str,
        *,
        http_status: int | None = None,
    ) -> None:
        context = {
            "provider": provider_name,
            "http_status": http_status,
        }
        super().__init__(
            message,
            error_code="PROVIDER_AUTH_ERROR",
            context=context,
            is_retryable=False,  # 认证错误不能重试
        )
        self.provider_name = provider_name
        self.http_status = http_status


class ProviderResponseError(ProviderError):
    """Provider返回异常响应（HTTP错误、格式错误等）"""

    def __init__(
        self,
        provider_name: str,
        message: str,
        *,
        http_status: int | None = None,
        response_body: str | None = None,
    ) -> None:
        context = {
            "provider": provider_name,
            "http_status": http_status,
            "response_preview": response_body[:200] if response_body else None,
        }
        super().__init__(
            message,
            error_code="PROVIDER_RESPONSE_ERROR",
            context=context,
            is_retryable=http_status in (429, 503) if http_status else True,
        )
        self.provider_name = provider_name
        self.http_status = http_status
        self.response_body = response_body


class TokenError(SiriusException):
    """Token相关错误的基类"""

    pass


class TokenBudgetExceededError(TokenError):
    """Token预算已用完"""

    def __init__(
        self,
        task_name: str,
        budget: int,
        used: int,
        requested: int,
    ) -> None:
        context = {
            "task": task_name,
            "budget": budget,
            "used": used,
            "requested": requested,
            "deficit": requested - (budget - used),
        }
        super().__init__(
            f"Task '{task_name}' exceeded token budget: used={used}, budget={budget}, requested={requested}",
            error_code="TOKEN_BUDGET_EXCEEDED",
            context=context,
            is_retryable=False,
        )
        self.task_name = task_name
        self.budget = budget
        self.used = used
        self.requested = requested


class TokenEstimationError(TokenError):
    """Token estimation error exception."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            error_code="TOKEN_ESTIMATION_ERROR",
            context={},
            is_retryable=False,
        )


class ParseError(SiriusException):
    """Content parsing error exception."""

    pass


class JSONParseError(ParseError):
    """JSON parsing error exception."""

    def __init__(
        self,
        raw_content: str,
        message: str,
        *,
        expected_schema: str | None = None,
    ) -> None:
        preview = raw_content[:100] if len(raw_content) > 100 else raw_content
        context = {
            "raw_content_preview": preview,
            "expected_schema": expected_schema,
        }
        super().__init__(
            message,
            error_code="JSON_PARSE_ERROR",
            context=context,
            is_retryable=False,
        )
        self.raw_content = raw_content
        self.expected_schema = expected_schema


class ContentValidationError(ParseError):
    """内容验证失败（如字段缺失、类型错误）"""

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        expected_type: str | None = None,
    ) -> None:
        context = {
            "field": field,
            "expected_type": expected_type,
        }
        super().__init__(
            message,
            error_code="CONTENT_VALIDATION_ERROR",
            context=context,
            is_retryable=False,
        )
        self.field = field
        self.expected_type = expected_type


class ConfigError(SiriusException):
    """Configuration error exception base class."""

    pass


class InvalidConfigError(ConfigError):
    """Configuration parameter is invalid exception."""

    def __init__(
        self,
        config_key: str,
        message: str,
        *,
        provided_value: Any = None,
        expected_format: str | None = None,
    ) -> None:
        context = {
            "config_key": config_key,
            "provided_value": str(provided_value),
            "expected_format": expected_format,
        }
        super().__init__(
            message,
            error_code="INVALID_CONFIG",
            context=context,
            is_retryable=False,
        )
        self.config_key = config_key
        self.provided_value = provided_value
        self.expected_format = expected_format


class MissingConfigError(ConfigError):
    """Required configuration is missing exception."""

    def __init__(
        self,
        config_key: str,
        message: str,
    ) -> None:
        context = {"config_key": config_key}
        super().__init__(
            message,
            error_code="MISSING_CONFIG",
            context=context,
            is_retryable=False,
        )
        self.config_key = config_key


class OrchestrationConfigError(ConfigError):
    """多模型协同配置错误"""

    def __init__(
        self,
        missing_models: dict[str, list[str]],
        message: str | None = None,
    ) -> None:
        """初始化多模型协同配置错误。
        
        Args:
            missing_models: 缺失的模型配置，映射为 {任务名: [缺失的模型列表]}
            message: 自定义错误消息
        """
        if message is None:
            tasks_str = ", ".join(missing_models.keys())
            models_str = ", ".join(m for models in missing_models.values() for m in models)
            message = (
                f"多模型协同已启用，但缺少以下模型配置：\n"
                f"  未配置的任务: {tasks_str}\n"
                f"  缺失的模型: {models_str}\n"
                f"请使用 configure_orchestration_models() 函数添加配置。"
            )
        
        context = {"missing_models": missing_models}
        super().__init__(
            message,
            error_code="ORCHESTRATION_CONFIG_ERROR",
            context=context,
            is_retryable=False,
        )
        self.missing_models = missing_models


class MemoryError(SiriusException):
    """Memory management error exception."""

    pass


class UserNotFoundError(MemoryError):
    """User record not found exception."""

    def __init__(self, user_id: str) -> None:
        super().__init__(
            f"User '{user_id}' not found in memory manager",
            error_code="USER_NOT_FOUND",
            context={"user_id": user_id},
            is_retryable=False,
        )
        self.user_id = user_id


class ConflictingMemoryError(MemoryError):
    """记忆冲突（用于记忆管理器的冲突检测）"""

    def __init__(
        self,
        user_id: str,
        fact_ids: list[str],
        message: str,
    ) -> None:
        context = {
            "user_id": user_id,
            "conflicting_facts": fact_ids,
        }
        super().__init__(
            message,
            error_code="CONFLICTING_MEMORY",
            context=context,
            is_retryable=False,
        )
        self.user_id = user_id
        self.fact_ids = fact_ids


# 便捷导出
__all__ = [
    "SiriusException",
    "ProviderError",
    "ProviderConnectionError",
    "ProviderAuthError",
    "ProviderResponseError",
    "TokenError",
    "TokenBudgetExceededError",
    "TokenEstimationError",
    "ParseError",
    "JSONParseError",
    "ContentValidationError",
    "ConfigError",
    "InvalidConfigError",
    "MissingConfigError",
    "OrchestrationConfigError",
    "MemoryError",
    "UserNotFoundError",
    "ConflictingMemoryError",
]
