"""Embedding 客户端模块。

向量化由 AMKR 提供（``/v1/embeddings``），本框架只负责把文本送过去。这里不再有
本地推理服务，也就没有模型加载、端口占用与跨进程共享那一套。
"""

from __future__ import annotations

from sirius_pulse.embedding.client import (
    DEFAULT_MODEL,
    EmbeddingClient,
    create_embedding_client,
    load_embedding_model,
)

__all__ = [
    "DEFAULT_MODEL",
    "EmbeddingClient",
    "create_embedding_client",
    "load_embedding_model",
]
