"""Embedding 微服务 — 基于 aiohttp 的 HTTP API，支持请求合并批量推理。

启动方式：
    python -m sirius_pulse.embedding.server --port 18900

API：
    POST /embed   {"texts": ["t1", "t2"]}  → {"embeddings": [[...], [...]]}
    GET  /health                           → {"status": "ok", "model": "..."}
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from sirius_pulse.core.constants import EMBEDDING_DEFAULT_PORT

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "BAAI/bge-small-zh"
DEFAULT_PORT = EMBEDDING_DEFAULT_PORT

# 单次 encode 的最大文本数，超出则分片
MAX_ENCODE_BATCH = 64


def _model_available_locally(model_name: str) -> bool:
    """Detect whether model files exist in local HF cache."""
    from pathlib import Path as _Path
    cache_dir = _Path(os.path.expanduser("~"), ".cache", "huggingface", "hub")
    safe_name = "models--" + model_name.replace("/", "--")
    snapshots = cache_dir / safe_name / "snapshots"
    if not snapshots.exists():
        return False
    return any(snapshots.iterdir())


@dataclass
class _PendingRequest:
    """队列中的待处理请求，通过 Future 返回结果。"""
    texts: list[str]
    future: asyncio.Future[list[list[float]]]


@dataclass
class _BatchProcessor:
    """请求合并 + 批量推理核心。

    多个并发请求在时间窗口内合并为一个 batch，
    调用一次 SentenceTransformer.encode() 完成推理，
    然后按请求切分结果分发回去。
    """

    model_name: str = DEFAULT_MODEL_NAME
    max_batch_size: int = 32
    max_wait_ms: int = 50

    _model: Any = field(default=None, init=False, repr=False)
    _queue: asyncio.Queue[_PendingRequest] = field(
        default_factory=asyncio.Queue, init=False, repr=False
    )

    def _load_model(self) -> None:
        """预加载 sentence-transformers 模型（启动时调用，非懒加载）。

自动检测本地模型文件：已缓存 → 离线加载；未缓存 → 在线下载后离线加载。
"""
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer
        local = _model_available_locally(self.model_name)
        if local:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            logger.info("Embedding 模型已缓存，使用离线模式加载: %s", self.model_name)
        else:
            logger.info("Embedding 模型未缓存，开始在线下载: %s ...", self.model_name)
        logger.info("Embedding 服务正在加载模型: %s ...", self.model_name)
        t0 = time.monotonic()
        self._model = SentenceTransformer(
            self.model_name,
            local_files_only=local,
        )
        duration_ms = round((time.monotonic() - t0) * 1000, 1)
        logger.info("Embedding 服务模型加载完成: %s (%.1fms)", self.model_name, duration_ms)

    def _encode_sync(self, texts: list[str]) -> list[list[float]]:
        """同步 encode，供线程池调用。支持大 batch 自动分片。"""
        # 模型已在启动时预加载，此处不应再触发加载
        if len(texts) <= MAX_ENCODE_BATCH:
            vecs = self._model.encode(texts, convert_to_tensor=False)
            return [v.tolist() for v in vecs]

        # 大 batch 分片处理
        all_results: list[list[float]] = []
        for i in range(0, len(texts), MAX_ENCODE_BATCH):
            chunk = texts[i : i + MAX_ENCODE_BATCH]
            vecs = self._model.encode(chunk, convert_to_tensor=False)
            all_results.extend(v.tolist() for v in vecs)
        return all_results

    async def encode(self, texts: list[str]) -> list[list[float]]:
        """外部调用入口：将请求入队，等待批量处理后返回结果。"""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[list[float]]] = loop.create_future()
        await self._queue.put(_PendingRequest(texts=texts, future=future))
        return await future

    async def run(self) -> None:
        """后台循环：收集请求 → 合并批量 → 推理 → 分发结果。"""
        logger.info("Embedding 批量推理循环已启动")
        while True:
            # 1. 至少等一个请求
            first = await self._queue.get()
            batch: list[_PendingRequest] = [first]

            # 2. 在 max_wait_ms 窗口内收集更多请求
            deadline = time.monotonic() + self.max_wait_ms / 1000.0
            while len(batch) < self.max_batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    req = await asyncio.wait_for(
                        self._queue.get(), timeout=remaining
                    )
                    batch.append(req)
                except asyncio.TimeoutError:
                    break

            # 3. 记录每个请求在合并文本中的起止位置
            offsets: list[tuple[int, int]] = []
            all_texts: list[str] = []
            for req in batch:
                start = len(all_texts)
                all_texts.extend(req.texts)
                offsets.append((start, len(all_texts)))

            total_texts = len(all_texts)
            n_requests = len(batch)
            logger.debug(
                "Embedding 批量推理: %d 个请求合并为 %d 条文本",
                n_requests,
                total_texts,
            )

            # 4. 在线程池中执行同步 encode（避免阻塞事件循环）
            loop = asyncio.get_running_loop()
            t0 = time.monotonic()
            try:
                all_embeddings = await loop.run_in_executor(
                    None, self._encode_sync, all_texts
                )
                duration_ms = round((time.monotonic() - t0) * 1000, 1)
                logger.info(
                    "Embedding 推理完成: %d 条文本, %.1fms", total_texts, duration_ms
                )
            except Exception as exc:
                # 推理失败，通知所有等待的请求
                logger.error("Embedding 推理失败: %s", exc)
                for req in batch:
                    if not req.future.done():
                        req.future.set_exception(exc)
                continue

            # 5. 按请求切分结果并分发
            for req, (start, end) in zip(batch, offsets):
                if not req.future.done():
                    req.future.set_result(all_embeddings[start:end])


# ---------------------------------------------------------------------------
# aiohttp Application
# ---------------------------------------------------------------------------

_processor: _BatchProcessor | None = None


async def _handle_embed(request: Any) -> Any:
    """POST /embed — 接收文本列表，返回 embedding 向量。"""
    from aiohttp import web

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    texts = body.get("texts")
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
        return web.json_response(
            {"error": "texts must be a list of strings"}, status=400
        )

    if not texts:
        return web.json_response({"embeddings": []})

    assert _processor is not None
    try:
        embeddings = await _processor.encode(texts)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)

    return web.json_response({"embeddings": embeddings})


async def _handle_health(request: Any) -> Any:
    """GET /health — 健康检查。"""
    from aiohttp import web

    return web.json_response({
        "status": "ok",
        "model": _processor.model_name if _processor else "unknown",
    })


async def _start_processor(app: Any) -> None:
    """aiohttp 启动钩子：启动批量推理后台任务。"""
    assert _processor is not None
    app["_batch_task"] = asyncio.create_task(_processor.run())
    logger.info("Embedding 服务批量推理任务已启动")


async def _stop_processor(app: Any) -> None:
    """aiohttp 停止钩子：取消批量推理任务。"""
    task = app.get("_batch_task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def create_app(
    model_name: str = DEFAULT_MODEL_NAME,
    max_batch_size: int = 32,
    max_wait_ms: int = 50,
) -> Any:
    """创建 aiohttp Application。"""
    from aiohttp import web

    global _processor
    _processor = _BatchProcessor(
        model_name=model_name,
        max_batch_size=max_batch_size,
        max_wait_ms=max_wait_ms,
    )
    # 预加载模型，避免首次请求时阻塞
    _processor._load_model()

    app = web.Application()
    app.router.add_post("/embed", _handle_embed)
    app.router.add_get("/health", _handle_health)
    app.on_startup.append(_start_processor)
    app.on_cleanup.append(_stop_processor)
    return app


def main() -> None:
    """CLI 入口：python -m sirius_pulse.embedding.server。"""
    from sirius_pulse.logging_config import configure_logging

    configure_logging("INFO")

    parser = argparse.ArgumentParser(description="Sirius Chat Embedding 微服务")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL_NAME, help="模型名称")
    parser.add_argument("--max-batch-size", type=int, default=32, help="最大批量大小")
    parser.add_argument("--max-wait-ms", type=int, default=50, help="批量合并等待窗口（毫秒）")
    args = parser.parse_args()

    from aiohttp import web

    app = create_app(
        model_name=args.model,
        max_batch_size=args.max_batch_size,
        max_wait_ms=args.max_wait_ms,
    )
    logger.info("Embedding 服务启动: port=%d model=%s", args.port, args.model)
    web.run_app(app, port=args.port, print=None)


if __name__ == "__main__":
    main()
