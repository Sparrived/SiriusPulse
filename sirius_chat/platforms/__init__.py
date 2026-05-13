"""SiriusChat 平台适配器层。

目前提供 NapCat (QQ) 原生 OneBot v11 适配器，支持正向 WebSocket 连接、
自动重连、群聊/私聊消息收发、图片缓存与后台投递。

使用示例::

    from sirius_chat.platforms import NapCatAdapter, NapCatBridge

    from sirius_chat.platforms.runtime import EngineRuntime

    runtime = EngineRuntime("./work_path", global_data_path="./data")
    adapter = NapCatAdapter(ws_url="ws://localhost:3001", token="napcat_ws")
    bridge = NapCatBridge(adapter, runtime=runtime, work_path="./work_path", config={"root": "123456"})
    await adapter.connect()
    await bridge.start()
"""

from __future__ import annotations

from .onebot_v11.napcat import NapCatAdapter, NapCatBridge, NapCatManager
from .runtime import EngineRuntime

__all__ = [
    "NapCatAdapter",
    "NapCatBridge",
    "EngineRuntime",
    "NapCatManager",
]
