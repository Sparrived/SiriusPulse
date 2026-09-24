"""WebUI 认证中间件 — JWT 令牌验证与权限控制。

白名单路径免认证，GET 请求允许 admin/viewer 角色，
写操作（POST/PUT/DELETE）仅允许 admin 角色。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiohttp import web

from sirius_pulse.providers.amkr import AMKR_PROXY_PREFIX
from sirius_pulse.webui.app_keys import AUTH_MANAGER_KEY
from sirius_pulse.webui.server_utils import _json_response

LOG = logging.getLogger("sirius.webui.middleware")

# 认证白名单路径前缀（免认证）。WebSocket 不在白名单中：它会和
# REST API 一样在 upgrade 前经过 JWT 校验，避免 /ws/* 新路由意外裸露。
#
# ``/amkr/``（同源反代，见 :mod:`sirius_pulse.webui.amkr_proxy`）必须免 JWT：
# 面板是 iframe 里的**另一个文档**，它持有的是 AMKR 的工作空间面板 key，而不是
# 本框架的 JWT，因此带不了认证头。这条路径把「有没有凭据」重新交还给 AMKR——
# 静态资源本就不需要凭据，而取数接口仍要一把面板 key，且反代只放行面板用得到
# 的那几条路径。
_WHITELIST_PREFIXES: tuple[str, ...] = (
    "/static/",
    "/api/auth/login",
    "/api/auth/status",
    f"{AMKR_PROXY_PREFIX}/",
)

# 浏览器 WebSocket API 不允许设置 Authorization 头。前端把 JWT 放在
# Sec-WebSocket-Protocol 的受控第二项；服务端只协商固定的第一个协议，
# 因而不会在 URL、访问日志或响应协议中回显 JWT。
_WS_AUTH_PROTOCOL = "sirius-auth"
_VALID_ROLES: frozenset[str] = frozenset({"admin", "viewer"})

# 认证白名单精确路径
_WHITELIST_EXACT: tuple[str, ...] = (
    "/",
    "/index.html",
)

# 只读 HTTP 方法
_READ_ONLY_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


def _is_whitelisted(path: str) -> bool:
    """判断请求路径是否在认证白名单中。"""
    # 精确匹配
    if path in _WHITELIST_EXACT:
        return True
    # 前缀匹配
    return any(path.startswith(prefix) for prefix in _WHITELIST_PREFIXES)


def _extract_websocket_token(request: web.Request) -> str | None:
    """Read a JWT from the controlled WebSocket subprotocol offer.

    Expected browser offer: ``sirius-auth, <jwt>``.  Reject malformed and
    oversized values instead of accepting arbitrary subprotocol text.  Tokens
    are intentionally not accepted in query parameters because URLs are often
    retained by access logs, browser history, and proxies.
    """
    offered = request.headers.get("Sec-WebSocket-Protocol", "")
    parts = [part.strip() for part in offered.split(",") if part.strip()]
    if len(parts) != 2 or parts[0] != _WS_AUTH_PROTOCOL:
        return None
    token = parts[1]
    if len(token) > 8192 or any(char.isspace() for char in token):
        return None
    return token or None


def _extract_token(request: web.Request) -> str | None:
    """Extract a JWT for an HTTP request or a WebSocket upgrade."""
    if request.path.startswith("/ws/"):
        return _extract_websocket_token(request)

    # Authorization: Bearer <token>
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()

    # Retain REST query fallback for compatibility.  WebSocket upgrades never
    # reach this branch and therefore never place credentials in URLs.
    return request.query.get("token") or None


@web.middleware
async def auth_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """认证中间件。

    规则：
    1. 白名单路径（/static/, /, /api/auth/login, /api/auth/status）免认证
    2. REST 与 /ws/ upgrade 都需要有效的 JWT；WebSocket 使用受控
       Sec-WebSocket-Protocol 协商项而非 URL 参数
    3. GET/HEAD/OPTIONS 请求：admin 或 viewer 角色均可访问
    4. POST/PUT/DELETE 请求：仅 admin 角色可访问
    """
    path = request.path
    method = request.method.upper()

    # 白名单路径直接放行
    if _is_whitelisted(path):
        return await handler(request)

    # 提取并验证令牌
    token = _extract_token(request)
    if not token:
        LOG.debug("未提供认证令牌: %s %s", method, path)
        return _json_response({"error": "未提供认证令牌，请先登录"}, status=401)

    # 从应用获取 AuthManager 实例
    auth_manager = request.app.get(AUTH_MANAGER_KEY)
    if not auth_manager:
        LOG.error("AuthManager 未注册到应用中")
        return _json_response({"error": "服务端认证配置错误"}, status=500)

    payload = auth_manager.verify_token(token)
    if payload is None:
        LOG.debug("令牌验证失败: %s %s", method, path)
        return _json_response({"error": "令牌无效或已过期，请重新登录"}, status=401)

    role = str(payload.get("role", ""))
    if role not in _VALID_ROLES:
        LOG.debug("令牌角色无效: %s %s", method, path)
        return _json_response({"error": "令牌角色无效"}, status=403)

    # 写操作权限检查
    if method not in _READ_ONLY_METHODS and role != "admin":
        LOG.debug("权限不足: user=%s, role=%s, method=%s", payload.get("sub"), role, method)
        return _json_response({"error": "权限不足，需要管理员权限"}, status=403)

    # 将用户信息注入请求，供下游处理器使用
    request["auth_user"] = payload.get("sub", "")
    request["auth_role"] = role

    return await handler(request)
