"""LLM 接入层。

供应商、Key 池、模型与采样参数都由 AMKR（``auto-model-key-router``）承担，这里
不再有厂商实现与路由注册表。

唯一的实现是 :class:`GoTransportProvider`：它把「对 AMKR 的 HTTP 调用 + SSE 聚合
+ 多模态内联 + 生图」交给 Go 侧车（``go/llm-transport/``，契约见其
``CONTRACT.md``），prompt 组装、工具循环、重试、兜底任务与 token 记账仍全部留在
Python。侧车是**必需**的——Python 侧不再保留任何传输实现，也没有回退分支。
"""

from sirius_pulse.providers.amkr import AmkrSettings, load_amkr_settings
from sirius_pulse.providers.amkr_sync import AmkrError, SyncResult, register_persona_tasks
from sirius_pulse.providers.base import GenerationRequest, LLMProvider
from sirius_pulse.providers.go_transport import GoTransportProvider
from sirius_pulse.providers.mock import MockProvider

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
]
