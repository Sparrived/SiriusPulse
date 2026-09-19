"""Embedding 的同步 HTTP 客户端。

向量化不再由本框架自己算：请求发往 AMKR 的 ``/v1/embeddings``，由 AMKR 决定
真正调用哪家供应商（例如硅基流动的 ``BAAI/bge-m3``）。本框架只说两件事：要哪个
模型、要编码哪些文本。

凭据用该人格工作空间的**推理 key**（``amkr_ik_…``），与对话补全走同一把——它被
AMKR 钉死在一个空间上，调不了管理接口。模型名必须写进该空间的 ``models`` 白名单，
否则 AMKR 会拒掉这次直连模型调用（``unified-model`` 这类全局计划对受限 key 不可用）。

用 stdlib urllib 而非 httpx：本客户端同时在同步记忆代码里被调用，保持零额外依赖。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

#: AMKR 的默认地址（与 ``providers/amkr.py`` 的默认值一致）。
DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 30.0

#: 全局配置里指定 embedding 模型名的字段。
MODEL_FIELD = "embedding_model"

#: 默认 embedding 模型。``bge-m3`` 支持中文且为多语种，1024 维。
#:
#: 改名等于换维度：``bge-small-zh`` 是 512 维，改回它会让已有向量库全部作废
#: （见 :func:`sirius_pulse.memory.diary.vector_store.DiaryVectorStore.model_matches`）。
DEFAULT_MODEL = "BAAI/bge-m3"


class EmbeddingClient:
    """同步 HTTP 客户端，封装对 AMKR ``/v1/embeddings`` 的调用。

    健康检查走 AMKR 的 ``/health``（免费、免鉴权），因此可以放心被 WebUI 轮询；
    真正的向量化只在需要时发生。
    """

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        timeout: float = DEFAULT_TIMEOUT,
        *,
        api_key: str = "",
        model: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._available: bool | None = None  # None = 未检测
        self._dimension: int | None = None

    @property
    def model(self) -> str:
        """当前使用的 embedding 模型名（AMKR 侧的真实模型名）。"""
        return self._model

    @property
    def dimension(self) -> int | None:
        """已观测到的向量维度；尚未成功编码过时为 ``None``。

        维度是模型的属性而不是配置项：换模型就会变（``bge-small-zh`` 512 维、
        ``bge-m3`` 1024 维），所以只能从响应里学，不能假定。
        """
        return self._dimension

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
        """调用 AMKR 的 ``/v1/embeddings``，返回嵌入向量列表。

        Args:
            texts: 要编码的文本列表。

        Returns:
            与 texts 等长、顺序一致的嵌入向量列表。

        Raises:
            RuntimeError: 服务请求失败或响应格式异常时抛出。
        """
        if not texts:
            return []
        url = f"{self._base_url}/v1/embeddings"
        payload = json.dumps({"model": self._model, "input": texts}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self._available = False
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            raise RuntimeError(f"Embedding 请求失败: HTTP {exc.code} {detail or exc.reason}") from exc
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            self._available = False
            raise RuntimeError(f"Embedding 服务请求失败: {exc}") from exc

        items = data.get("data")
        if not isinstance(items, list):
            raise RuntimeError(f"Embedding 服务返回格式异常: {data}")
        # OpenAI 契约允许乱序返回，靠 index 归位，否则「向量与文本错配」会静默地
        # 把语义检索搞错，比直接报错难查得多。
        ordered: list[list[float]] = [[] for _ in texts]
        for item in items:
            if not isinstance(item, dict):
                continue
            embedding = item.get("embedding")
            if not isinstance(embedding, list):
                continue
            index = item.get("index")
            if not isinstance(index, int) or not 0 <= index < len(ordered):
                index = next((i for i, vec in enumerate(ordered) if not vec), 0)
            ordered[index] = embedding
        if any(not vec for vec in ordered):
            raise RuntimeError(f"Embedding 服务返回的向量数量不足: 期望 {len(texts)}")

        self._available = True
        self._dimension = len(ordered[0])
        return ordered

    def encode_single(self, text: str) -> list[float]:
        """编码单条文本，返回嵌入向量。"""
        results = self.encode([text])
        if not results:
            raise RuntimeError("Embedding 服务返回空结果")
        return results[0]

    def _check_health(self) -> bool:
        """健康检查：GET AMKR ``/health``，确认服务在线且模型已配置。

        刻意不在这里做一次真实向量化：本方法会被 WebUI 轮询，而每次向量化都要花钱。
        ``/health`` 会列出 AMKR 已配置的模型名，因此「服务在线」与「模型已配置」
        两个条件都能免费验证。供应商侧是否真的可用，由启动时的一次预热编码暴露。
        """
        url = f"{self._base_url}/health"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.debug("Embedding 服务不可用: %s (%s)", self._base_url, exc)
            return False

        if data.get("status") != "ok":
            logger.warning("AMKR /health 状态异常: %s", data)
            return False
        models = data.get("models")
        if self._model and isinstance(models, list) and self._model not in models:
            logger.warning(
                "AMKR 未配置 embedding 模型 %s（已配置: %s）", self._model, ", ".join(map(str, models))
            )
            return False
        logger.debug("Embedding 服务已连接: %s", self._base_url)
        return True


def load_embedding_model(global_data_path: Path | str) -> str:
    """读取配置里的 embedding 模型名，环境变量优先。"""
    import os

    from sirius_pulse.utils.json_io import read_json

    env_model = os.getenv("SIRIUS_EMBEDDING_MODEL", "").strip()
    if env_model:
        return env_model
    data = read_json(Path(global_data_path) / "global_config.json", default=None)
    if isinstance(data, dict):
        configured = str(data.get(MODEL_FIELD, "") or "").strip()
        if configured:
            return configured
    return DEFAULT_MODEL


def create_embedding_client(
    global_data_path: Path | str,
    persona: str,
    *,
    model: str = "",
) -> EmbeddingClient:
    """为某个人格构造指向 AMKR 的 embedding 客户端。

    地址、凭据、模型名都来自同一处：AMKR 连接配置 + 该人格工作空间的推理 key。
    这样「换 embedding 模型」只是 AMKR 侧的一个配置改动加一次 WebUI 重建索引，
    本框架不需要知道供应商是谁。
    """
    from sirius_pulse.providers.amkr import load_amkr_settings, load_inference_keys
    from sirius_pulse.providers.amkr_sync import workspace_for

    settings = load_amkr_settings(global_data_path)
    workspace = workspace_for(settings, persona)
    inference_key = load_inference_keys(global_data_path).get(workspace, "")
    return EmbeddingClient(
        base_url=settings.base_url,
        timeout=float(settings.timeout_seconds),
        api_key=inference_key,
        model=model or load_embedding_model(global_data_path),
    )


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "DEFAULT_URL",
    "MODEL_FIELD",
    "EmbeddingClient",
    "create_embedding_client",
    "load_embedding_model",
]
