"""异步写缓冲 — 收集运行时写操作，定期批量推送到管家端。

WriteBuffer 是助手端的核心组件。引擎管线是同步代码，不能直接做 HTTP 调用，
所以所有写操作通过 WriteBuffer 的同步方法入队，由后台异步任务定期 flush。

关键数据（category 为 "critical_*"）入队后会额外触发一次异步 flush，
实现混合策略：关键数据近乎实时推送，非关键数据定期批量推送。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

LOG = logging.getLogger("sirius.write_buffer")

# 连续失败次数超过此值后进入降级模式
_MAX_CONSECUTIVE_FAILURES = 5


class WriteBuffer:
    """异步写缓冲。

    线程安全：add() 由引擎同步线程调用，flush() 由异步事件循环调用。
    通过 threading.Lock 保护缓冲区。
    """

    def __init__(
        self,
        api_url: str,
        token: str | None = None,
        flush_interval: float = 30.0,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._flush_interval = flush_interval

        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._flush_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        self._consecutive_failures = 0
        self._total_flushed = 0
        self._total_dropped = 0

    # ------------------------------------------------------------------
    # 同步入口（引擎管线调用）
    # ------------------------------------------------------------------

    def add(self, op_type: str, data: dict[str, Any]) -> None:
        """添加一条写操作到缓冲区（同步，非阻塞）。

        Args:
            op_type: 操作类型，如 "token_usage", "cognition_event", "semantic_profile" 等。
            data: 操作数据。
        """
        with self._lock:
            self._buffer.append({"type": op_type, "data": data})

    def add_critical(self, op_type: str, data: dict[str, Any]) -> None:
        """关键数据：添加到缓冲并安排尽快 flush。

        关键数据包括：新消息归档、用户画像变更、术语学习等。
        入队后通过 call_soon_threadsafe 触发异步 flush，不等待完成。
        """
        self.add(op_type, data)
        self._schedule_flush()

    def add_many(self, op_type: str, items: list[dict[str, Any]]) -> None:
        """批量添加同类型操作。"""
        with self._lock:
            self._buffer.extend({"type": op_type, "data": item} for item in items)

    # ------------------------------------------------------------------
    # 异步生命周期
    # ------------------------------------------------------------------

    async def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """启动定期 flush 后台任务。"""
        self._loop = loop or asyncio.get_running_loop()
        self._flush_task = asyncio.create_task(self._periodic_flush_loop())
        LOG.info("WriteBuffer 已启动，flush 间隔: %.0fs", self._flush_interval)

    async def stop(self) -> None:
        """停止并执行最后一次 flush。"""
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        # 最后一次 flush
        await self.flush()
        LOG.info(
            "WriteBuffer 已停止，累计 flush: %d 条, 丢弃: %d 条",
            self._total_flushed,
            self._total_dropped,
        )

    # ------------------------------------------------------------------
    # Flush 逻辑
    # ------------------------------------------------------------------

    async def flush(self) -> bool:
        """将缓冲区所有数据批量 POST 到管家端。

        Returns:
            True if flush succeeded, False otherwise.
        """
        with self._lock:
            if not self._buffer:
                return True
            batch = self._buffer[:]
            self._buffer.clear()

        url = f"{self._api_url}/api/data/batch"
        payload = json.dumps({"operations": batch}, ensure_ascii=False)

        try:
            import aiohttp

            headers = {"Content-Type": "application/json"}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        self._consecutive_failures = 0
                        self._total_flushed += len(batch)
                        LOG.debug("WriteBuffer flush 成功: %d 条操作", len(batch))
                        return True
                    else:
                        body = await resp.text()
                        LOG.warning("WriteBuffer flush 失败 (HTTP %d): %s", resp.status, body[:200])
                        self._consecutive_failures += 1
                        self._requeue(batch)
                        return False
        except Exception as exc:
            LOG.warning("WriteBuffer flush 异常: %s", exc)
            self._consecutive_failures += 1
            self._requeue(batch)
            return False

    def _requeue(self, batch: list[dict[str, Any]]) -> None:
        """Flush 失败时将数据放回缓冲区。"""
        if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            # 降级模式：丢弃非关键数据，只保留关键数据
            critical = [op for op in batch if op.get("type", "").startswith("critical_")]
            dropped = len(batch) - len(critical)
            if dropped:
                self._total_dropped += dropped
                LOG.warning(
                    "降级模式：丢弃 %d 条非关键数据，保留 %d 条关键数据",
                    dropped,
                    len(critical),
                )
            with self._lock:
                self._buffer = critical + self._buffer
        else:
            with self._lock:
                self._buffer = batch + self._buffer

    async def _periodic_flush_loop(self) -> None:
        """后台定期 flush 循环。"""
        while True:
            await asyncio.sleep(self._flush_interval)
            try:
                await self.flush()
            except Exception as exc:
                LOG.warning("定期 flush 异常: %s", exc)

    def _schedule_flush(self) -> None:
        """从同步线程安排一次异步 flush。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.flush(), loop)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    @property
    def pending_count(self) -> int:
        """缓冲区中待 flush 的操作数。"""
        with self._lock:
            return len(self._buffer)

    @property
    def is_degraded(self) -> bool:
        """是否处于降级模式。"""
        return self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES

    @property
    def stats(self) -> dict[str, Any]:
        """缓冲区统计信息。"""
        return {
            "pending": self.pending_count,
            "total_flushed": self._total_flushed,
            "total_dropped": self._total_dropped,
            "consecutive_failures": self._consecutive_failures,
            "is_degraded": self.is_degraded,
        }
