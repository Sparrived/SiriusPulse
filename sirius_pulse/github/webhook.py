"""GitHub Webhook 基础设施。

提供签名验证、HTTP 服务器生命周期管理与事件分发机制。
SKILL 与 Plugin 均可复用此模块，无需各自实现 Webhook 服务端。
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Awaitable, Callable

from aiohttp import web

logger = logging.getLogger(__name__)

# Webhook 事件处理器签名: async def handler(event_type: str, body: dict) -> None
WebhookHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
# 仓库过滤器签名: 接收 "owner/repo" 字符串，返回 True 表示需要处理
RepoFilter = Callable[[str], bool]


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """验证 GitHub Webhook HMAC-SHA256 签名。

    若未配置 secret 则始终通过（仅用于调试，生产环境应配置）。
    """
    if not secret:
        return True
    expected = "sha256=" + hmac.new(secret.encode(), payload, "sha256").hexdigest()
    return hmac.compare_digest(expected, signature)


class GitHubWebhookServer:
    """GitHub Webhook HTTP 服务器。

    封装 aiohttp 生命周期，支持多事件类型分发和仓库过滤。
    使用示例::

        server = GitHubWebhookServer(secret="my_secret", port=8080)
        server.set_repo_filter(lambda r: r in {"owner/repo1", "owner/repo2"})
        server.add_handler("issues", my_issue_handler)
        server.add_handler("pull_request", my_pr_handler)
        await server.start()
        # ... 运行中 ...
        await server.stop()
    """

    def __init__(self, secret: str = "", host: str = "127.0.0.1", port: int = 0):
        self._secret = secret
        self._host = host
        self._port = port
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._handlers: dict[str, list[WebhookHandler]] = {}
        self._repo_filter: RepoFilter | None = None

        self._app.router.add_post("/webhook/github", self._handle)

    def set_repo_filter(self, filter_fn: RepoFilter) -> None:
        self._repo_filter = filter_fn

    def add_handler(self, event_type: str, handler: WebhookHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    async def start(self) -> int:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        actual_port = site._server.sockets[0].getsockname()[1] if self._port == 0 else self._port  # type: ignore[union-attr]
        logger.info(
            "GitHub Webhook 服务已启动: http://%s:%s/webhook/github", self._host, actual_port
        )
        return actual_port

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            logger.info("GitHub Webhook 服务已停止")

    async def _handle(self, request: web.Request) -> web.Response:
        body_bytes = await request.read()
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not verify_signature(body_bytes, sig, self._secret):
            logger.warning("Webhook 签名验证失败")
            return web.json_response({"error": "signature mismatch"}, status=401)

        event_type = request.headers.get("X-GitHub-Event", "")
        body = await request.json()

        repo_name = body.get("repository", {}).get("full_name", "")
        if self._repo_filter is not None and not self._repo_filter(repo_name):
            logger.debug("Webhook 仓库 %s 不在处理范围内，忽略", repo_name)
            return web.json_response({"status": "ignored", "reason": "repo filtered out"})

        handlers = self._handlers.get(event_type, [])
        if not handlers:
            return web.json_response(
                {"status": "ignored", "reason": f"no handler for event: {event_type}"}
            )

        for handler in handlers:
            try:
                await handler(event_type, body)
            except Exception as exc:
                logger.error(
                    "Webhook 处理器异常 (event=%s, repo=%s): %s",
                    event_type,
                    repo_name,
                    exc,
                    exc_info=True,
                )

        return web.json_response({"status": "ok", "event": event_type})
