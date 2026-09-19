"""LLM 接入层。

本框架只保留一个真实实现：:class:`OpenAICompatibleProvider`，端点固定指向
AMKR（``auto-model-key-router``）。供应商、Key 池、模型与采样参数都由 AMKR
承担，这里不再有厂商实现与路由注册表。
"""

from sirius_pulse.providers.amkr import AmkrSettings, load_amkr_settings
from sirius_pulse.providers.amkr_sync import AmkrError, SyncResult, register_persona_tasks
from sirius_pulse.providers.base import GenerationRequest, LLMProvider
from sirius_pulse.providers.mock import MockProvider
from sirius_pulse.providers.openai_compatible import OpenAICompatibleProvider

__all__ = [
    "GenerationRequest",
    "LLMProvider",
    "AmkrSettings",
    "load_amkr_settings",
    "AmkrError",
    "SyncResult",
    "register_persona_tasks",
    "MockProvider",
    "OpenAICompatibleProvider",
]
