"""助手端 WebSocket 客户端 — 连接管家端并请求接管人格控制。

AssistantClient 在助手模式下运行，连接管家端的 ButlerServer，
发送 takeover 请求，维护心跳，断线时通知上层。

用法::

    client = AssistantClient(
        butler_url="ws://server:9500",
    )
    success = await client.connect_and_takeover()
    if success:
        # 运行引擎...
        await client.run_heartbeat()  # 阻塞直到断开
    await client.release()
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sirius_pulse.network.protocol import (
    ButlerMessage,
    MessageType,
    make_heartbeat,
    make_release,
    make_takeover,
)

LOG = logging.getLogger("sirius.assistant_client")

# 心跳发送间隔（秒）
_HEARTBEAT_INTERVAL = 10.0


class AssistantClient:
    """助手端 WebSocket 客户端。"""

    def __init__(
        self,
        butler_url: str,
        token: str | None = None,
    ) -> None:
        self._butler_url = butler_url
        self._token = token
        self._ws: Any = None
        self._heartbeat_task: asyncio.Task | None = None
        self._listen_task: asyncio.Task | None = None
        self._connected = False
        self._takeover_confirmed = False
        self._disconnect_event = asyncio.Event()

    @property
    def connected(self) -> bool:
        """是否已连接到管家端。"""
        return self._connected

    @property
    def takeover_confirmed(self) -> bool:
        """是否已确认接管。"""
        return self._takeover_confirmed

    @property
    def disconnect_event(self) -> asyncio.Event:
        """断开事件，可用于外部等待断开。"""
        return self._disconnect_event

    async def connect_and_takeover(self) -> bool:
        """连接管家端并请求接管。返回是否成功。

        Raises:
            ConnectionError: 无法连接到管家端。
        """
        try:
            import websockets
            import websockets.client
        except ImportError:
            raise ImportError("需要安装 websockets 库: uv pip install websockets")

        LOG.info("正在连接管家端: %s", self._butler_url)
        try:
            self._ws = await websockets.client.connect(self._butler_url)
        except Exception as exc:
            raise ConnectionError(f"无法连接管家端: {exc}") from exc

        self._connected = True
        LOG.info("已连接管家端")

        # 启动监听任务
        self._listen_task = asyncio.create_task(self._listen())

        # 发送 takeover 请求
        takeover_msg = make_takeover(token=self._token)
        await self._ws.send(takeover_msg.to_json())

        # 等待 ACK（最多 10 秒）
        deadline = time.time() + 10.0
        while time.time() < deadline and not self._takeover_confirmed and self._connected:
            await asyncio.sleep(0.1)

        if self._takeover_confirmed:
            LOG.info("管家端已确认接管人格")
            # 启动心跳
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            return True
        else:
            LOG.error("接管请求超时或被拒绝")
            await self.close()
            return False

    async def release(self) -> None:
        """主动释放控制权并关闭连接。"""
        if self._ws and self._connected:
            try:
                msg = make_release()
                await self._ws.send(msg.to_json())
            except Exception:
                pass
        await self.close()

    async def close(self) -> None:
        """关闭连接并清理。"""
        self._connected = False
        self._takeover_confirmed = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        self._disconnect_event.set()
        LOG.info("助手端客户端已关闭")

    async def wait_disconnect(self) -> None:
        """阻塞等待连接断开。"""
        await self._disconnect_event.wait()

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _listen(self) -> None:
        """监听管家端消息。"""
        try:
            async for raw in self._ws:
                try:
                    msg = ButlerMessage.from_json(raw)
                except ValueError:
                    continue

                if msg.type == MessageType.TAKEOVER_ACK:
                    self._takeover_confirmed = True

                elif msg.type == MessageType.TAKEOVER_NACK:
                    LOG.error("接管被拒绝")
                    self._connected = False
                    self._disconnect_event.set()

                elif msg.type == MessageType.ERROR:
                    reason = msg.payload.get("reason", "未知错误")
                    LOG.error("管家端错误: %s", reason)

        except Exception as exc:
            LOG.debug("监听管家端消息异常: %s", exc)
        finally:
            self._connected = False
            self._disconnect_event.set()

    async def _heartbeat_loop(self) -> None:
        """定期发送心跳。"""
        while self._connected:
            try:
                msg = make_heartbeat()
                await self._ws.send(msg.to_json())
            except Exception:
                self._connected = False
                self._disconnect_event.set()
                break
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
