from __future__ import annotations

from sirius_pulse.providers.base import DEFAULT_TIMEOUT_SECONDS
from sirius_pulse.providers.openai_compatible import OpenAICompatibleProvider

DEFAULT_VOLCENGINE_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


class VolcengineArkProvider(OpenAICompatibleProvider):
    """Volcengine Ark provider backed by /api/v3/chat/completions."""

    _provider_name = "volcengine-ark"

    def __init__(self, *, api_key: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        # Strip the /api/v3 suffix so _build_url() can append it consistently
        base = DEFAULT_VOLCENGINE_ARK_BASE_URL.removesuffix("/api/v3")
        super().__init__(
            base_url=base,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )

    def _build_url(self, request) -> str:
        return f"{self._base_url}/api/v3/chat/completions"
