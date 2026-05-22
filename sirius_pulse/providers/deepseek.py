from __future__ import annotations

from sirius_pulse.providers.openai_compatible import OpenAICompatibleProvider

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class DeepSeekProvider(OpenAICompatibleProvider):
    """DeepSeek provider backed by /chat/completions.

    DeepSeek is OpenAI-compatible. The constructor accepts either:
    - https://api.deepseek.com
    - https://api.deepseek.com/v1
    and normalizes both to the same request endpoint.
    """

    _provider_name = "deepseek"

    def __init__(self, *, api_key: str, timeout_seconds: int = 30) -> None:
        super().__init__(
            base_url=DEFAULT_DEEPSEEK_BASE_URL,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )

    def _build_url(self, request) -> str:
        return f"{self._base_url}/chat/completions"
