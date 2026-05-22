"""SiriusChat WebUI — 基于 aiohttp 的配置管理面板。

提供 REST API + 内嵌前端页面，用于：
- Provider / 人格 / 模型编排 配置
- 群白名单管理
- 引擎状态监控与重启
- NapCat 环境管理

使用示例::

    from sirius_pulse.webui import WebUIServer
    webui = WebUIServer(bridge=bridge, host="0.0.0.0", port=8080)
    await webui.start()
"""

from __future__ import annotations

from .server import WebUIServer

__all__ = ["WebUIServer"]
