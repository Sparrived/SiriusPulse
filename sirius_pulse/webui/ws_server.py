"""WebSocket 事件推送服务 — 把事件送到 WebUI 前端。

本模块只负责连接管理与广播（按人格订阅 ``/ws/events/{name}``、全局订阅
``/ws/events``）。内容有两个来源：``WebUIFileEventBridge`` 把 ``data/`` 下的
文件变更映射成粗粒度资源事件，``EngineEventBridge`` 订阅人格引擎的
``SessionEventBus`` 并转发引擎事件。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from sirius_pulse.webui.middleware import _WS_AUTH_PROTOCOL

LOG = logging.getLogger("sirius.webui.ws")

# 心跳间隔（秒）
_PING_INTERVAL = 30
# Bound long-lived browser connections so one authenticated browser/user cannot
# consume the WebUI's file-event fan-out capacity indefinitely.
_MAX_CONNECTIONS = 64
_MAX_CONNECTIONS_PER_USER = 8


def _path_event_payload(data_dir: Path, path: Path) -> dict[str, Any] | None:
    """Map a changed file under data_dir to coarse WebUI resource events."""
    try:
        rel = path.resolve().relative_to(data_dir.resolve())
    except Exception:
        return None

    parts = rel.parts
    name = path.name.lower()
    resources: set[str] = set()
    persona = ""

    if "personas" in parts:
        idx = parts.index("personas")
        if idx + 1 < len(parts):
            persona = parts[idx + 1]

    if name in {"global_config.json", "webui_status.json", "worker_status.json"}:
        resources.update({"personas", "monitoring", "dashboard"})
    if name.endswith((".log", ".txt")) or "logs" in parts:
        resources.add("logs")
    if "token_usage.db" in name or name == "token_usage_records.json":
        resources.update({"tokens", "monitoring", "dashboard"})
    if "cognition_events.db" in name:
        resources.update({"cognition", "monitoring", "dashboard"})
    if name.endswith(".jsonl") and "archive" in parts:
        resources.update({"conversations", "monitoring", "dashboard"})
    if name == "basic_memory.json":
        resources.update({"conversations", "memory", "monitoring", "dashboard"})
    if name == ".telemetry.jsonl":
        resources.update({"tool-history", "monitoring", "dashboard"})
    if "diary" in parts or "memory" in parts:
        resources.update({"memory", "dashboard"})
    # 意图与自主产出都写在 memory/ 下：让她惦记的和做过的事都能自动刷新，
    # 不必为此另开一条推送通路。
    if "autonomy" in parts or name == "intentions.json":
        resources.update({"autonomy", "dashboard"})
    # 工作模式轨迹同样落在 memory/ 下，写一轮就能在页面上看到一轮。
    if "work_mode" in parts:
        resources.update({"work-mode", "dashboard"})

    if not resources:
        return None

    return {
        "type": "data_changed",
        "resources": sorted(resources),
        "persona": persona,
        "path": str(rel).replace("\\", "/"),
    }


def _normalized_origin(origin: str) -> tuple[str, str, int | None] | None:
    """Return a strict scheme/host/port tuple for an HTTP Origin value."""
    try:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        scheme = parsed.scheme.casefold()
        port = parsed.port
        if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
            port = None
        return (scheme, parsed.hostname.casefold().rstrip("."), port)
    except ValueError:
        return None


def _request_origin_allowed(request: web.Request) -> bool:
    """Accept only same-origin browser WebSocket upgrades.

    A real browser always sends Origin for a WebSocket handshake.  Requiring it
    rejects cross-site scripts even when an attacker has access to an existing
    credential.  The comparison uses Host plus socket scheme; reverse proxies
    should pass a correct Host and terminate TLS before forwarding.
    """
    origin = request.headers.get("Origin", "").strip()
    if not origin:
        return False
    expected = _normalized_origin(f"{request.scheme}://{request.host}")
    supplied = _normalized_origin(origin)
    return expected is not None and _origin_matches(expected, supplied)


def _origin_matches(
    expected: tuple[str, str, int | None],
    supplied: tuple[str, str, int | None] | None,
) -> bool:
    """Compare parsed public origin fields without accepting malformed values."""
    return supplied == expected


class WebSocketManager:
    """WebSocket 连接管理器，桥接引擎事件到前端。"""

    def __init__(
        self,
        *,
        max_connections: int = _MAX_CONNECTIONS,
        max_connections_per_user: int = _MAX_CONNECTIONS_PER_USER,
    ) -> None:
        # 人格名称 -> 连接列表；"*" 表示全局订阅
        self._connections: dict[str, list[web.WebSocketResponse]] = {}
        self._connection_users: dict[web.WebSocketResponse, str] = {}
        self._connections_by_user: dict[str, int] = defaultdict(int)
        # Handshake preparation awaits.  Reserve capacity before that await so
        # concurrent upgrades cannot all pass the same stale count check.
        self._pending_connections = 0
        self._pending_connections_by_user: dict[str, int] = defaultdict(int)
        self._max_connections = max(1, int(max_connections))
        self._max_connections_per_user = max(1, int(max_connections_per_user))

    async def handle_ws(self, request: web.Request) -> web.StreamResponse:
        """Authenticate and handle one same-origin WebSocket subscription.

        JWT validation is performed by :func:`auth_middleware` before this
        method is entered.  This method repeats only the WebSocket-specific
        authorization invariants (role, Origin and bounded connections) before
        calling ``prepare()``, so no unauthorized caller can complete an
        upgrade.
        """
        role = str(request.get("auth_role", ""))
        user = str(request.get("auth_user", ""))
        if role not in {"admin", "viewer"} or not user:
            return web.json_response({"error": "WebSocket 认证无效"}, status=403)
        if not _request_origin_allowed(request):
            LOG.warning("拒绝跨域或缺失 Origin 的 WebSocket upgrade")
            return web.json_response({"error": "WebSocket Origin 不被允许"}, status=403)
        if self.connection_count + self._pending_connections >= self._max_connections:
            return web.json_response({"error": "WebSocket 连接数已达上限"}, status=503)
        if (
            self._connections_by_user[user] + self._pending_connections_by_user[user]
            >= self._max_connections_per_user
        ):
            return web.json_response({"error": "该用户 WebSocket 连接数已达上限"}, status=429)

        name = request.match_info.get("name", "*").strip() or "*"
        self._pending_connections += 1
        self._pending_connections_by_user[user] += 1
        registered = False
        try:
            # The middleware has already checked the offered token protocol.
            # Only negotiate the constant capability label, never a client-
            # supplied JWT.
            ws = web.WebSocketResponse(heartbeat=_PING_INTERVAL, protocols=(_WS_AUTH_PROTOCOL,))
            await ws.prepare(request)

            self._connections.setdefault(name, []).append(ws)
            self._connection_users[ws] = user
            self._connections_by_user[user] += 1
            registered = True
        finally:
            self._pending_connections -= 1
            pending_for_user = self._pending_connections_by_user[user] - 1
            if pending_for_user > 0:
                self._pending_connections_by_user[user] = pending_for_user
            else:
                self._pending_connections_by_user.pop(user, None)
        if not registered:
            # ws.prepare() either raised or returned a failed response; the
            # aiohttp exception/response owns the handshake outcome.
            raise RuntimeError("WebSocket upgrade did not complete")
        LOG.info("WebSocket 已连接: persona=%s, 当前连接数=%d", name, self.connection_count)

        # 发送握手确认
        await self._safe_send_json(
            ws,
            {
                "type": "connected",
                "persona": name,
                "timestamp": time.time(),
            },
        )

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    # 客户端文本消息保留为扩展点，当前仅记录
                    LOG.debug("收到客户端消息: persona=%s, data=%s", name, msg.data)
                elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._unregister(name, ws)
            if not ws.closed:
                await ws.close()
            LOG.info(
                "WebSocket 已断开: persona=%s, 剩余连接数=%d",
                name,
                self.connection_count,
            )

        return ws

    async def broadcast_to_persona(self, persona_name: str, event_data: dict[str, Any]) -> None:
        """向指定人格的所有 WebSocket 客户端广播事件。

        同时向全局订阅者（"*"）发送，确保全局监听不遗漏。
        """
        payload = {
            "persona": persona_name,
            "timestamp": time.time(),
            **event_data,
        }
        targets = list(self._connections.get(persona_name, []))
        # 全局订阅者也会收到
        targets.extend(self._connections.get("*", []))
        await self._broadcast(targets, payload)

    async def broadcast_all(self, event_data: dict[str, Any]) -> None:
        """向所有连接的客户端广播事件。"""
        payload = {
            "timestamp": time.time(),
            **event_data,
        }
        all_targets: list[web.WebSocketResponse] = []
        for conns in self._connections.values():
            all_targets.extend(conns)
        await self._broadcast(all_targets, payload)

    async def close_all(self) -> None:
        """关闭所有 WebSocket 连接。"""
        for name, conns in list(self._connections.items()):
            for ws in list(conns):
                try:
                    if not ws.closed:
                        await ws.close(
                            code=web.WSCloseCode.GOING_AWAY,  # type: ignore[attr-defined]
                            message=b"server shutting down",
                        )
                except (ConnectionResetError, asyncio.CancelledError):
                    pass
                finally:
                    self._unregister(name, ws)
        self._connections.clear()
        self._connection_users.clear()
        self._connections_by_user.clear()
        LOG.info("所有 WebSocket 连接已关闭")

    @property
    def connection_count(self) -> int:
        """当前活跃连接数。"""
        return sum(len(conns) for conns in self._connections.values())

    # ─── 内部辅助方法 ─────────────────────────────────────

    def _unregister(self, name: str, ws: web.WebSocketResponse) -> None:
        """Remove a connection and its per-user admission reservation."""
        conns = self._connections.get(name)
        if conns is not None:
            try:
                conns.remove(ws)
            except ValueError:
                pass
            # 清理空列表，避免键堆积
            if not conns:
                self._connections.pop(name, None)

        user = self._connection_users.pop(ws, "")
        if user:
            remaining = self._connections_by_user.get(user, 0) - 1
            if remaining > 0:
                self._connections_by_user[user] = remaining
            else:
                self._connections_by_user.pop(user, None)

    async def _broadcast(
        self,
        targets: list[web.WebSocketResponse],
        payload: dict[str, Any],
    ) -> None:
        """向一组连接发送 JSON 消息，自动清理断开的连接。"""
        dead: list[tuple[str, web.WebSocketResponse]] = []
        for ws in targets:
            ok = await self._safe_send_json(ws, payload)
            if not ok:
                # 查找该连接所属的人格键，稍后统一清理
                for name, conns in self._connections.items():
                    if ws in conns:
                        dead.append((name, ws))
                        break

        # 批量移除死连接
        for name, ws in dead:
            self._unregister(name, ws)

    @staticmethod
    async def _safe_send_json(ws: web.WebSocketResponse, data: dict[str, Any]) -> bool:
        """安全地向 WebSocket 发送 JSON，返回是否成功。"""
        if ws.closed:
            return False
        try:
            await ws.send_json(data)
            return True
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            return False


class WebUIFileEventBridge:
    """Watch data files and publish coarse change events to WebSocket clients."""

    def __init__(self, data_dir: Path, ws_manager: WebSocketManager) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.ws_manager = ws_manager
        self._observer: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_emit: dict[tuple[str, str], float] = {}

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._observer is not None:
            return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except Exception:
            LOG.warning("watchdog 不可用，WebUI 文件事件推送已禁用")
            return

        bridge = self

        class Handler(FileSystemEventHandler):
            def on_modified(self, event: Any) -> None:
                bridge._handle_path(event.src_path, event.is_directory)

            def on_created(self, event: Any) -> None:
                bridge._handle_path(event.src_path, event.is_directory)

            def on_moved(self, event: Any) -> None:
                bridge._handle_path(getattr(event, "dest_path", event.src_path), event.is_directory)

        self._loop = loop
        observer = Observer()
        observer.schedule(Handler(), str(self.data_dir), recursive=True)
        observer.daemon = True
        observer.start()
        self._observer = observer
        LOG.info("WebUI 文件事件桥已启动: %s", self.data_dir)

    def stop(self) -> None:
        observer = self._observer
        self._observer = None
        if observer is None:
            return
        observer.stop()
        observer.join(timeout=2)
        LOG.info("WebUI 文件事件桥已停止")

    def _handle_path(self, raw_path: str, is_directory: bool) -> None:
        if is_directory or self._loop is None:
            return
        payload = _path_event_payload(self.data_dir, Path(raw_path))
        if payload is None:
            return

        now = time.monotonic()
        key = (str(payload.get("persona", "")), ",".join(payload.get("resources", [])))
        if now - self._last_emit.get(key, 0) < 0.35:
            return
        self._last_emit[key] = now

        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self.ws_manager.broadcast_all(payload))
        )


def setup_ws_routes(app: web.Application, ws_manager: WebSocketManager) -> None:
    """将 WebSocket 路由注册到 aiohttp Application。

    路由：
        GET /ws/events       → 全局事件订阅
        GET /ws/events/{name} → 按人格事件订阅
    """
    app.router.add_get("/ws/events", ws_manager.handle_ws)
    app.router.add_get("/ws/events/{name}", ws_manager.handle_ws)
    LOG.info("WebSocket 路由已注册: /ws/events, /ws/events/{name}")
