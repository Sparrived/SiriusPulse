"""WebUI 认证中间件 — JWT 令牌验证与权限控制。

白名单路径免认证，GET 请求允许 admin/viewer 角色，
写操作（POST/PUT/DELETE）仅允许 admin 角色。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiohttp import web

from sirius_pulse.webui.app_keys import AUTH_MANAGER_KEY
from sirius_pulse.webui.server_utils import _json_response

LOG = logging.getLogger("sirius.webui.middleware")

# 认证白名单路径前缀（免认证）
_WHITELIST_PREFIXES: tuple[str, ...] = (
    "/static/",
    "/ws/",
    "/api/auth/login",
    "/api/auth/status",
)

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


def _extract_token(request: web.Request) -> str | None:
    """从请求中提取 JWT 令牌。

    优先从 Authorization 头提取，其次从查询参数 token 提取。
    """
    # Authorization: Bearer <token>
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()

    # 查询参数 fallback
    return request.query.get("token") or None


@web.middleware
async def auth_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """认证中间件。

    规则：
    1. 白名单路径（/static/, /, /api/auth/login, /api/auth/status）免认证
    2. 其他路径需要有效的 JWT 令牌
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

    role = payload.get("role", "")

    # 写操作权限检查
    if method not in _READ_ONLY_METHODS and role != "admin":
        LOG.debug("权限不足: user=%s, role=%s, method=%s", payload.get("sub"), role, method)
        return _json_response({"error": "权限不足，需要管理员权限"}, status=403)

    # 将用户信息注入请求，供下游处理器使用
    request["auth_user"] = payload.get("sub", "")
    request["auth_role"] = role

    return await handler(request)
