"""管家端 WebSocket 服务 — 接受助手端连接并协调人格控制权。

ButlerServer 在主进程中运行，监听指定端口，接受助手端的 WebSocket 连接。
当助手端请求接管人格时，ButlerServer 通过文件标志通知 PersonaWorker
暂停 NapCat 消息处理；助手端断开后自动恢复。

用法::

    server = ButlerServer(host="0.0.0.0", port=9500, data_dir=Path("data/personas/sirius"))
    await server.start()
    # ...
    await server.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from sirius_pulse.network.protocol import (
    ButlerMessage,
    MessageType,
    make_error,
    make_takeover_ack,
)

LOG = logging.getLogger("sirius.butler_server")

# 心跳超时：超过此时间未收到心跳则认为助手端已断开
_HEARTBEAT_TIMEOUT = 30.0

# 心跳检查间隔（秒）
_HEARTBEAT_CHECK_INTERVAL = 5.0


class ButlerServer:
    """管家端 WebSocket 服务。"""

    def __init__(
        self,
        host: str,
        port: int,
        data_dir: Path,
        token: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._data_dir = data_dir
        self._token = token
        self._server: asyncio.AbstractServer | None = None
        self._session: _AssistantSession | None = None
        self._heartbeat_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动 WebSocket 服务。"""
        try:
            import websockets
            import websockets.server
        except ImportError:
            LOG.error("需要安装 websockets 库: uv pip install websockets")
            raise

        self._server = await websockets.server.serve(
            self._handle_client,
            self._host,
            self._port,
        )
        self._heartbeat_task = asyncio.create_task(self._heartbeat_monitor())
        LOG.info("ButlerServer 已启动: ws://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        """停止服务，释放人格控制权。"""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._session is not None:
            self._release_persona()

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        LOG.info("ButlerServer 已停止")

    @property
    def is_taken_over(self) -> bool:
        """当前是否被助手端接管。"""
        return self._session is not None

    # ------------------------------------------------------------------
    # WebSocket 连接处理
    # ------------------------------------------------------------------

    async def _handle_client(self, ws: Any) -> None:
        """处理单个助手端 WebSocket 连接。"""
        taken_over = False
        try:
            async for raw in ws:
                try:
                    msg = ButlerMessage.from_json(raw)
                except ValueError as exc:
                    await ws.send(make_error(f"消息解析失败: {exc}").to_json())
                    continue

                if msg.type == MessageType.HELLO:
                    LOG.debug("收到 HELLO")

                elif msg.type == MessageType.TAKEOVER:
                    ack = await self._handle_takeover(msg, ws)
                    await ws.send(ack.to_json())
                    if ack.type == MessageType.TAKEOVER_ACK:
                        taken_over = True

                elif msg.type == MessageType.RELEASE:
                    if taken_over:
                        self._release_persona()
                        await ws.send(make_takeover_ack().to_json())
                        LOG.info("助手端主动释放人格控制权")
                        taken_over = False

                elif msg.type == MessageType.HEARTBEAT:
                    if self._session is not None:
                        self._session.last_heartbeat = time.time()

                else:
                    LOG.warning("未处理的消息类型: %s", msg.type)

        except Exception as exc:
            LOG.debug("助手端连接异常: %s", exc)
        finally:
            if taken_over:
                self._release_persona()
                LOG.info("助手端断开，已自动恢复人格的消息处理")

    async def _handle_takeover(
        self,
        msg: ButlerMessage,
        ws: Any,
    ) -> ButlerMessage:
        """处理 takeover 请求。"""
        # 验证人格目录存在
        if not self._data_dir.is_dir():
            return make_error(f"人格数据目录不存在: {self._data_dir}")

        # 验证 token
        if self._token:
            client_token = msg.payload.get("token", "")
            if client_token != self._token:
                return make_error("认证失败")

        # 检查是否已被其他助手接管
        if self._session is not None:
            return make_error("人格已被其他助手接管")

        # 执行接管：通过文件标志禁用管家端 adapter
        self._set_persona_enabled_flag(False)
        self._session = _AssistantSession(
            ws=ws,
            last_heartbeat=time.time(),
        )
        LOG.info("助手端已接管人格，管家端暂停消息处理")
        return make_takeover_ack()

    def _release_persona(self) -> None:
        """释放人格控制权，恢复管家端消息处理。"""
        self._session = None
        self._set_persona_enabled_flag(True)
        LOG.info("人格控制权已恢复到管家端")

    # ------------------------------------------------------------------
    # 文件标志控制
    # ------------------------------------------------------------------

    def _set_persona_enabled_flag(self, enabled: bool) -> None:
        """通过 engine_state/enabled 文件标志控制 PersonaWorker。"""
        flag_path = self._data_dir / "engine_state" / "enabled"
        try:
            flag_path.parent.mkdir(parents=True, exist_ok=True)
            flag_path.write_text("1" if enabled else "0", encoding="utf-8")
        except Exception as exc:
            LOG.warning("写入 enabled 标志失败: %s", exc)

    # ------------------------------------------------------------------
    # 心跳监控
    # ------------------------------------------------------------------

    async def _heartbeat_monitor(self) -> None:
        """定期检查助手端心跳，超时则自动释放。"""
        while True:
            await asyncio.sleep(_HEARTBEAT_CHECK_INTERVAL)
            session = self._session
            if session is not None:
                if time.time() - session.last_heartbeat > _HEARTBEAT_TIMEOUT:
                    LOG.warning("助手端心跳超时（%.0fs），自动恢复人格消息处理", _HEARTBEAT_TIMEOUT)
                    self._release_persona()


class _AssistantSession:
    """助手端会话状态。"""

    __slots__ = ("ws", "last_heartbeat")

    def __init__(self, ws: Any, last_heartbeat: float) -> None:
        self.ws = ws
        self.last_heartbeat = last_heartbeat
