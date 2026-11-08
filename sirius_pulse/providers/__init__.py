"""LLM 接入层。

本框架只保留一个真实实现：:class:`OpenAICompatibleProvider`，端点固定指向
AMKR（``auto-model-key-router``）。供应商、Key 池、模型与采样参数都由 AMKR
承担，这里不再有厂商实现与路由注册表。

可选的第二个传输层是 :class:`GoTransportProvider`：它把「对 AMKR 的 HTTP 调用 +
SSE 聚合 + 多模态内联」交给 Go 侧车（``go/llm-transport/``，契约见其
``CONTRACT.md``），prompt 组装、工具循环、重试、兜底任务与 token 记账仍全部留在
Python。默认关闭——只有 ``global_config.json`` 里 ``llm_transport`` 配成 ``go``
时才会被用到。
"""

from sirius_pulse.providers.amkr import AmkrSettings, load_amkr_settings
from sirius_pulse.providers.amkr_sync import AmkrError, SyncResult, register_persona_tasks
from sirius_pulse.providers.base import GenerationRequest, LLMProvider
from sirius_pulse.providers.go_transport import GoTransportProvider
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
    "GoTransportProvider",
    "MockProvider",
    "OpenAICompatibleProvider",
]
