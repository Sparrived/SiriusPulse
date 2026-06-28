"""共享 Embedding 服务模块。

提供跨进程共享的 sentence-transformers 推理服务，
通过 HTTP API 供各 PersonaWorker 调用，避免每个子进程重复加载模型。
"""
from __future__ import annotations

from sirius_pulse.embedding.client import EmbeddingClient

__all__ = ["EmbeddingClient"]
