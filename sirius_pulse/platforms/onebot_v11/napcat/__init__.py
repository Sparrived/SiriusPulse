"""NapCat 平台实现 —— 基于 OneBot v11 协议。

提供：
    - NapCatAdapter: 正向 WebSocket 客户端 + 平台集成（事件→引擎→发送）
"""

from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter

__all__ = [
    "NapCatAdapter",
]
