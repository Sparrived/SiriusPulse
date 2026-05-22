"""GitHub API 客户端 —— 标准 headers + httpx.AsyncClient 封装。

所有 GitHub API 调用应使用此模块提供的 headers 与 Client，
确保统一的 API 版本（2022-11-28）和 Content-Type。
"""

from __future__ import annotations

from typing import Any

import httpx

_GITHUB_API_BASE = "https://api.github.com"
_DEFAULT_TIMEOUT = 30.0


def github_headers(token: str = "", *, extra_accept: str | None = None) -> dict[str, str]:
    """构建标准 GitHub REST API 请求头。

    Args:
        token: GitHub PAT（空字符串表示匿名访问）
        extra_accept: 覆盖默认的 Accept header（如 "application/vnd.github.v3.diff"）
    """
    headers: dict[str, str] = {
        "Accept": extra_accept or "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SiriusChat-GitHub/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class GitHubClient:
    """GitHub REST API 客户端。

    用例::

        async with GitHubClient("ghp_xxx") as client:
            issues = await client.get("/repos/owner/repo/issues")
            labels = await client.post("/repos/owner/repo/labels", json={...})

    不关心响应的调用（如仅需成功/失败）可使用 ``client.head / get / post / put / patch``，
    需取得 JSON 体时使用 ``client.get_json / post_json``。
    """

    def __init__(
        self,
        token: str = "",
        *,
        base_url: str = _GITHUB_API_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url
        self._timeout = timeout
        self._extra_headers = dict(extra_headers or {})

        headers = github_headers(token)
        headers.update(self._extra_headers)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout),
        )

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.aclose()

    async def close(self) -> None:
        """手动关闭底层 HTTP 客户端。"""
        await self._client.aclose()

    # ── 基础 HTTP 方法 ──

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._client.get(path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._client.post(path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._client.put(path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._client.patch(path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._client.delete(path, **kwargs)

    # ── JSON 快捷方法 ──

    async def get_json(self, path: str, **kwargs: Any) -> list[dict[str, Any]] | dict[str, Any] | None:
        """GET 并解析 JSON 响应体。非 200 时返回 None。"""
        resp = await self._client.get(path, **kwargs)
        if resp.status_code == 200:
            return resp.json()
        return None

    async def post_json(self, path: str, **kwargs: Any) -> dict[str, Any] | None:
        """POST 并解析 JSON 响应体。非 2xx 时返回 None。"""
        resp = await self._client.post(path, **kwargs)
        if resp.status_code in (200, 201):
            return resp.json()
        return None
