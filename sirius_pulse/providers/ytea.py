from __future__ import annotations

from sirius_pulse.providers.openai_compatible import OpenAICompatibleProvider

DEFAULT_YTEA_BASE_URL = "https://api.ytea.top"


class YTeaProvider(OpenAICompatibleProvider):
    """YTea provider backed by OpenAI-compatible /v1/chat/completions."""

    _provider_name = "ytea"

    def __init__(self, *, api_key: str, timeout_seconds: int = 30) -> None:
        super().__init__(
            base_url=DEFAULT_YTEA_BASE_URL,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
