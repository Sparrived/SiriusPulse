"""Typed aiohttp application keys for WebUI shared state."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from sirius_pulse.webui.auth import AuthManager
    from sirius_pulse.webui.ws_server import WebSocketManager
else:
    AuthManager = Any
    WebSocketManager = Any

DATA_DIR_KEY: web.AppKey[Path] = web.AppKey("data_dir", Path)
AUTH_MANAGER_KEY: web.AppKey[AuthManager] = web.AppKey("auth_manager", AuthManager)
WS_MANAGER_KEY: web.AppKey[WebSocketManager] = web.AppKey("ws_manager", WebSocketManager)
