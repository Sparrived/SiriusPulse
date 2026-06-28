from __future__ import annotations

from sirius_pulse.providers.base import DEFAULT_TIMEOUT_SECONDS
from sirius_pulse.providers.openai_compatible import OpenAICompatibleProvider

DEFAULT_ALIYUN_BAILIAN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode"


def _normalize_aliyun_bailian_base_url(base_url: str) -> str:
    normalized = (base_url or DEFAULT_ALIYUN_BAILIAN_BASE_URL).rstrip("/")
    if normalized.endswith("/v1"):
        return normalized[:-3]
    return normalized


class AliyunBailianProvider(OpenAICompatibleProvider):
    """Aliyun Bailian provider backed by DashScope's OpenAI-compatible endpoint.

    The constructor accepts either:
    - https://dashscope.aliyuncs.com/compatible-mode
    - https://dashscope.aliyuncs.com/compatible-mode/v1
    and normalizes both to the same request endpoint.
    """

    _provider_name = "aliyun-bailian"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_ALIYUN_BAILIAN_BASE_URL,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            base_url=_normalize_aliyun_bailian_base_url(base_url),
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
