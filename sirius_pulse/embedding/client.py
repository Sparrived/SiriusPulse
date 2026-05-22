"""Embedding 服务的同步 HTTP 客户端。

供 DiaryIndexer 在同步上下文中调用，
与远程 Embedding 微服务通信。网络开销约 0.1ms（localhost），
远低于本地 SentenceTransformer.encode() 的 10-50ms。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:18900"
DEFAULT_TIMEOUT = 30.0


class EmbeddingClient:
    """同步 HTTP 客户端，封装对 Embedding 微服务的调用。

    使用 stdlib urllib.request，无需额外依赖。
    支持自动检测服务可用性。
    """

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._available: bool | None = None  # None = 未检测

    @property
    def available(self) -> bool:
        """服务是否可用（首次调用时会尝试健康检查）。"""
        if self._available is None:
            self._available = self._check_health()
        return self._available

    def check_health(self) -> bool:
        """强制重新检查服务健康状态并更新缓存。"""
        self._available = self._check_health()
        return self._available

    def encode(self, texts: list[str]) -> list[list[float]]:
        """调用远程 encode，返回嵌入向量列表。

        Args:
            texts: 要编码的文本列表。

        Returns:
            与 texts 等长的嵌入向量列表。

        Raises:
            RuntimeError: 服务请求失败时抛出。
        """
        url = f"{self._base_url}/embed"
        payload = json.dumps({"texts": texts}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            self._available = False
            raise RuntimeError(f"Embedding 服务请求失败: {exc}") from exc

        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list):
            raise RuntimeError(f"Embedding 服务返回格式异常: {data}")
        return embeddings

    def encode_single(self, text: str) -> list[float]:
        """编码单条文本，返回嵌入向量。"""
        results = self.encode([text])
        if not results:
            raise RuntimeError("Embedding 服务返回空结果")
        return results[0]

    def _check_health(self) -> bool:
        """健康检查：GET /health。"""
        url = f"{self._base_url}/health"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                ok = data.get("status") == "ok"
                if ok:
                    logger.info("Embedding 服务已连接: %s", self._base_url)
                return ok
        except Exception as exc:
            logger.debug("Embedding 服务不可用: %s (%s)", self._base_url, exc)
            return False
