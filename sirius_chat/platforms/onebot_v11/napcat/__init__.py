"""NapCat 平台实现 —— 基于 OneBot v11 协议。

提供：
    - NapCatAdapter: 正向 WebSocket 客户端，继承 BaseAdapter
    - NapCatBridge: 事件→引擎→发送 薄桥接层
    - NapCatManager: NapCat 实例生命周期管理
"""
from sirius_chat.platforms.onebot_v11.napcat.adapter import NapCatAdapter
from sirius_chat.platforms.onebot_v11.napcat.bridge import NapCatBridge
from sirius_chat.platforms.onebot_v11.napcat.manager import NapCatManager

__all__ = [
    "NapCatAdapter",
    "NapCatBridge",
    "NapCatManager",
]
