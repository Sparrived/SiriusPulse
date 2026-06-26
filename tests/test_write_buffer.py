"""异步写缓冲测试。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sirius_pulse.network.write_buffer import WriteBuffer


class TestWriteBufferBasic:
    """WriteBuffer 基本操作。"""

    def test_add_queues_operation(self):
        buf = WriteBuffer("http://localhost:9500")
        buf.add("token_usage", {"model": "gpt-4o", "tokens": 100})
        assert buf.pending_count == 1

    def test_add_many_queues_multiple(self):
        buf = WriteBuffer("http://localhost:9500")
        buf.add_many("token_usage", [{"model": "gpt-4o"}, {"model": "claude"}])
        assert buf.pending_count == 2

    def test_add_critical_queues_operation(self):
        buf = WriteBuffer("http://localhost:9500")
        buf.add_critical("message", {"content": "你好"})
        assert buf.pending_count == 1

    def test_stats_reflect_state(self):
        buf = WriteBuffer("http://localhost:9500")
        buf.add("token_usage", {"tokens": 100})
        stats = buf.stats
        assert stats["pending"] == 1
        assert stats["total_flushed"] == 0
        assert stats["is_degraded"] is False


def _mock_aiohttp_session(response_status: int = 200, response_body: str = "{}"):
    """创建正确结构的 aiohttp.ClientSession mock。"""
    mock_resp = AsyncMock()
    mock_resp.status = response_status
    mock_resp.text = AsyncMock(return_value=response_body)

    # session.post(...) / session.get(...) 返回 async context manager
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_cm)
    mock_session.get = MagicMock(return_value=mock_cm)

    # aiohttp.ClientSession() 返回 async context manager
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    return session_cm


class TestWriteBufferFlush:
    """WriteBuffer flush 行为。"""

    @pytest.mark.asyncio
    async def test_flush_sends_batch_and_clears_buffer(self):
        buf = WriteBuffer("http://butler:9500")
        buf.add("token_usage", {"model": "gpt-4o", "tokens": 100})
        buf.add("cognition_event", {"emotion": "happy"})

        mock_session = _mock_aiohttp_session(200)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await buf.flush()

        assert result is True
        assert buf.pending_count == 0
        assert buf.stats["total_flushed"] == 2

    @pytest.mark.asyncio
    async def test_flush_requeues_on_failure(self):
        buf = WriteBuffer("http://butler:9500")
        buf.add("token_usage", {"tokens": 100})

        mock_session = _mock_aiohttp_session(500, "Internal Server Error")

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await buf.flush()

        assert result is False
        assert buf.pending_count == 1  # 数据放回缓冲

    @pytest.mark.asyncio
    async def test_flush_with_empty_buffer_succeeds(self):
        buf = WriteBuffer("http://butler:9500")
        result = await buf.flush()
        assert result is True

    @pytest.mark.asyncio
    async def test_degraded_mode_drops_non_critical(self):
        buf = WriteBuffer("http://butler:9500")
        buf._consecutive_failures = 10  # 模拟降级

        mock_session = _mock_aiohttp_session(500, "error")

        buf.add("token_usage", {"tokens": 100})
        buf.add("cognition_event", {"emotion": "happy"})

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await buf.flush()

        # 非关键数据被丢弃
        assert buf.stats["total_dropped"] == 2


class TestWriteBufferLifecycle:
    """WriteBuffer 生命周期。"""

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        buf = WriteBuffer("http://butler:9500", flush_interval=0.1)
        await buf.start()
        assert buf._flush_task is not None
        assert not buf._flush_task.done()

        await buf.stop()
        assert buf._flush_task.done()

    @pytest.mark.asyncio
    async def test_stop_does_final_flush(self):
        buf = WriteBuffer("http://butler:9500")
        buf.add("token_usage", {"tokens": 100})

        mock_session = _mock_aiohttp_session(200)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await buf.start()
            await buf.stop()

        assert buf.pending_count == 0
