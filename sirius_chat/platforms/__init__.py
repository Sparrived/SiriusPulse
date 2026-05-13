"""SiriusChat 平台适配器层。

目前提供 NapCat (QQ) 原生 OneBot v11 适配器，支持正向 WebSocket 连接、
自动重连、群聊/私聊消息收发、图片缓存与后台投递。

使用示例::

    from sirius_chat.platforms import NapCatAdapter
    from sirius_chat.platforms.runtime import EngineRuntime

    runtime = EngineRuntime("./work_path", global_data_path="./data")
    await runtime.start()

    adapter = NapCatAdapter(
        ws_url="ws://localhost:3001", token="napcat_ws",
        work_path="./work_path", config={"root": "123456"},
    )
    await adapter.connect()
    await adapter.start_handling(runtime.engine)
"""

from __future__ import annotations

from .onebot_v11.napcat import NapCatAdapter, NapCatManager
from .runtime import EngineRuntime

__all__ = [
    "NapCatAdapter",
    "EngineRuntime",
    "NapCatManager",
]
