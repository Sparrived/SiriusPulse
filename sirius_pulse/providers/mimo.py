from __future__ import annotations

from sirius_pulse.providers.base import DEFAULT_TIMEOUT_SECONDS
from sirius_pulse.providers.openai_compatible import OpenAICompatibleProvider

# 小米MIMO平台默认Base URL（按量付费）
DEFAULT_MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"

# 小米MIMO Token Plan默认Base URL（订阅制）
DEFAULT_MIMO_TOKENPLAN_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"


def _normalize_mimo_base_url(base_url: str) -> str:
    """标准化MIMO平台Base URL，移除末尾斜杠。"""
    normalized = (base_url or DEFAULT_MIMO_BASE_URL).rstrip("/")
    return normalized


class MimoProvider(OpenAICompatibleProvider):
    """小米MIMO平台provider，支持OpenAI兼容协议。

    支持两种使用模式：
    1. 按量付费API：使用 https://api.xiaomimimo.com/v1，API Key格式为 sk-xxxxx
    2. Token Plan订阅：使用 https://token-plan-cn.xiaomimimo.com/v1，API Key格式为 tp-xxxxx

    Token Plan是一种固定订阅费、按套餐限量调用的计费方式。
    """

    _provider_name = "mimo"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_MIMO_BASE_URL,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            base_url=_normalize_mimo_base_url(base_url),
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )

    def _build_url(self, request) -> str:
        """构建MIMO API请求URL。"""
        return f"{self._base_url}/chat/completions"


class MimoTokenPlanProvider(OpenAICompatibleProvider):
    """小米MIMO Token Plan订阅模式provider。

    Token Plan是小米MIMO平台的订阅制计费方式，提供固定订阅费、按套餐限量调用。
    使用专属的Base URL和API Key（格式为 tp-xxxxx）。
    """

    _provider_name = "mimo-tokenplan"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_MIMO_TOKENPLAN_BASE_URL,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            base_url=_normalize_mimo_base_url(base_url),
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )

    def _build_url(self, request) -> str:
        """构建MIMO Token Plan API请求URL。"""
        return f"{self._base_url}/chat/completions"
