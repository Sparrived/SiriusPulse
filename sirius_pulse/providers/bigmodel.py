from __future__ import annotations

from sirius_pulse.providers.openai_compatible import OpenAICompatibleProvider
from sirius_pulse.providers.base import DEFAULT_TIMEOUT_SECONDS

DEFAULT_BIGMODEL_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"


def _normalize_bigmodel_base_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if not normalized:
        return DEFAULT_BIGMODEL_BASE_URL
    if normalized.endswith("/api/paas/v4"):
        return normalized
    if normalized.endswith("/api/paas"):
        return f"{normalized}/v4"
    return f"{normalized}/api/paas/v4"


class BigModelProvider(OpenAICompatibleProvider):
    """BigModel provider backed by /api/paas/v4/chat/completions.

    BigModel GLM models use an OpenAI-compatible message schema with a
    BigModel-specific base path. The provider accepts either:
    - https://open.bigmodel.cn
    - https://open.bigmodel.cn/api/paas
    - https://open.bigmodel.cn/api/paas/v4
    and normalizes all of them to the same request endpoint.
    """

    _provider_name = "bigmodel"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BIGMODEL_BASE_URL,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            base_url=_normalize_bigmodel_base_url(base_url),
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )

    def _build_url(self, request) -> str:
        return f"{self._base_url}/chat/completions"
