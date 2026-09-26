"""原生 NapCat OneBot v11 Adapter — 完整的平台集成。

职责：
    - 正向 WebSocket 连接、OneBot v11 API 调用
    - OneBot 事件 → ParsedEvent 解析（表情/图片/@ 转换）
    - 引擎事件总线监听（delayed/reminder 投递）
    - 消息回复发送（带锁）

继承 BaseAdapter，实现平台无关的消息发送接口。
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import shutil
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

import websockets
import websockets.exceptions

try:
    from websockets.asyncio.client import ClientConnection as WebSocketClientProtocol
except ImportError:
    from websockets import (  # type: ignore[assignment, attr-defined, no-redef]
        WebSocketClientProtocol,
    )

from sirius_pulse.adapters.base import BaseAdapter, DeliveryUncertainError
from sirius_pulse.adapters.models import (
    AtSegment,
    FileSegment,
    ImageSegment,
    MessageGroup,
    ParsedEvent,
    ReplySegment,
    TextSegment,
    VoiceSegment,
)
from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.core.group_dispatcher import GroupDispatcher
from sirius_pulse.core.qq_mentions import parse_qq_at_mentions
from sirius_pulse.models.models import Message, UnifiedUser
from sirius_pulse.tools.builtin._internal._markdown_image import to_image_reference

LOG = logging.getLogger("sirius.platforms.napcat")
_DISPATCH_EVENT_TIME_BUCKET_SECONDS = 5
_FORWARD_NODE_CHARS = 800
_ATOMIC_PROACTIVE_SEND: ContextVar[bool] = ContextVar("atomic_proactive_send", default=False)
_PROACTIVE_DELIVERY_START: ContextVar[Callable[[], bool] | None] = ContextVar(
    "proactive_delivery_start",
    default=None,
)

EventHandler = Callable[[dict[str, Any]], Any]


class _OneBotDeliveryUncertain(DeliveryUncertainError):
    """The request reached the WebSocket but its OneBot result was not observed."""


def _is_ws_closed(ws: Any) -> bool:
    """兼容各版本 websockets 的 closed 检测。"""
    try:
        return bool(ws.closed)
    except AttributeError:
        try:
            from websockets.protocol import State

            return ws.state != State.OPEN
        except Exception:
            return getattr(ws, "close_code", None) is not None


def _safe_download_name(file_name: str) -> str:
    name = str(file_name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return "".join("_" if char in '<>:"|?*' or ord(char) < 32 else char for char in name)


def _split_forward_node_texts(text: str, limit: int = _FORWARD_NODE_CHARS) -> list[str]:
    """按行把文本切成不超过 *limit* 个字符的节点正文，避免单个节点超出 QQ 上限。"""
    chunks: list[str] = []
    current = ""
    for raw_line in str(text or "").splitlines():
        line = raw_line
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if not current:
            current = line
        elif len(current) + 1 + len(line) <= limit:
            current = f"{current}\n{line}"
        else:
            chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks or [""]


def _download_url(url: str, destination: Path) -> int:
    parsed = urlparse(str(url or ""))
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("NapCat 群文件下载链接必须是有效的 HTTP(S) URL")
    request = Request(url, headers={"User-Agent": "SiriusChat/1.1"})
    # The scheme and authority are validated immediately above. NapCat supplies
    # this URL for a user-authorized group-file download; file/custom schemes are rejected.
    with urlopen(request, timeout=300) as response, destination.open("wb") as output:  # nosec B310
        shutil.copyfileobj(response, output, length=1024 * 1024)
    return destination.stat().st_size


def _remove_file_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _normalize_local_path(value: str) -> Path | None:
    """把 NapCat 报出的文件引用解析成本机路径，非本机文件返回 ``None``。

    兼容 ``file://`` 形式（含 Windows 盘符写法）与 ``file:///`` 三斜杠路径。
    """
    text = str(value or "").strip()
    if not text or text.startswith(("http://", "https://", "data:", "base64://")):
        return None
    if text.startswith("file://"):
        parsed = urlparse(text)
        raw_path = unquote(parsed.path or "")
        if parsed.netloc:
            raw_path = f"//{parsed.netloc}{raw_path}"
        if raw_path.startswith("/") and len(raw_path) >= 3 and raw_path[2] == ":":
            raw_path = raw_path[1:]
        text = raw_path
    if not text:
        return None
    return Path(text).expanduser()


class NapCatAdapter(BaseAdapter):
    """NapCat OneBot v11 正向 WebSocket 客户端 + 平台集成。

    同时承担原 NapCatBridge 的职责：事件→引擎→发送。
    """

    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 30.0
    # 0 表示**永不放弃**：NapCat 重启、网络抖动期间适配器会一直在退避重试，
    # 而不是在 5 次失败后永久离线（此时心跳照写、UI 仍显示运行中，没人发现）。
    # 退避上限 30 秒，因此长期断线也只留下很低的日志/CPU 噪音。
    _MAX_RECONNECT_ATTEMPTS = 0

    adapter_type = "napcat"
    _NOT_READY_LOG_INTERVAL = 30.0
    _OFFLINE_LOG_INTERVAL = 60.0

    def __init__(
        self,
        ws_url: str,
        token: str | None = None,
        reconnect_interval: float = 5.0,
        api_timeout: float = 30.0,
        work_path: str | Path | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.ws_url = ws_url
        self.token = token
        self.reconnect_interval = reconnect_interval
        self.api_timeout = api_timeout

        self.ws: WebSocketClientProtocol | None = None
        self._running = False
        self._event_handlers: list[EventHandler] = []
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._echo_counter = 0
        self._listen_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None

        # 图片缓存路径
        _wp = Path(work_path) if work_path else Path(".")
        self._work_path = _wp
        self._image_cache_dir = _wp / "image_cache"
        self._sticker_cache_dir = _wp / "sticker_cache"

        # 引擎集成（原 Bridge 字段）
        self.plugin_config = dict(config or {})
        account_id = str(self.plugin_config.get("qq_number", "") or "").strip()
        if account_id:
            self.adapter_route_id = f"{self.adapter_type}:{account_id}"
        else:
            # Do not expose the WebSocket URL (which can contain credentials) in
            # event payloads; a bounded digest still keeps same-type instances
            # separate when an account ID is unavailable during bootstrap.
            route_digest = hashlib.sha256(str(ws_url).encode("utf-8")).hexdigest()[:16]
            self.adapter_route_id = f"{self.adapter_type}:endpoint-{route_digest}"
        self._enabled = True
        self._engine: Any = None
        self._last_not_ready_log: float = 0.0
        # 离线可观测性：断线起点、上次播报时间、是否已耗尽重试
        self._offline_since: float | None = None
        self._last_offline_log: float = 0.0
        self._reconnect_exhausted = False
        self._reply_locks: dict[str, asyncio.Lock] = {}
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._seen_message_ids: dict[str, float] = {}
        self._seen_message_ttl = 300.0
        self._group_member_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._bot_admin_cache: dict[str, tuple[float, bool]] = {}
        self._group_metadata_ttl = 300.0

        # API 限流：每群/私聊独立，每秒最多 1 条消息
        self._last_api_call_at: dict[str, float] = {}
        self._api_send_lock = asyncio.Lock()
        self._event_bus_task: asyncio.Task | None = None

        # 消息处理锁：防止并发进入引擎 process_message 导致字典迭代时修改错误。
        # 按会话（群/私聊）分锁而不是全局单锁——一次 process_message 会跨越整轮
        # LLM 生成，全局锁意味着一个群 30 秒的生成会把其余所有群串行阻塞。
        self._process_locks: dict[str, asyncio.Lock] = {}
        self._process_locks_guard = asyncio.Lock()
        self._dispatcher: GroupDispatcher | None = None
        self._dispatch_leases: dict[str, str] = {}
        self._dispatch_delivery_active: set[str] = set()
        self._dispatch_retry_tasks: dict[str, asyncio.Task] = {}
        self._proactive_event_locks: dict[str, asyncio.Lock] = {}
        self._proactive_event_results: dict[str, float] = {}
        # Keep fire-and-forget event handlers so stop/rebind can cancel work
        # that still owns an old engine.
        self._event_handler_tasks: set[asyncio.Task[Any]] = set()
        # Every raw event task captures this generation.  It prevents a task
        # queued just before a rebind from running against the new engine.
        self._engine_generation = 0
        # Set during ownership handoff so the WebSocket path cannot add a raw
        # handler after the lifecycle code has started draining tasks.
        self._detaching_engine = False
        # Serialize start/rebind/stop so a listener cannot be rebound while a
        # concurrent lifecycle transition is still cancelling its handlers.
        self._engine_lifecycle_lock = asyncio.Lock()

    # ─── 生命周期 ─────────────────────────────────────────

    async def start_handling(self, engine: Any) -> None:
        """启动事件处理和引擎事件总线监听。

        调用者必须在外层先完成 runtime.start() 初始化引擎。
        此方法注册 _on_event 处理器并开始监听引擎事件总线。
        """
        async with self._engine_lifecycle_lock:
            previous_engine = self._engine
            if previous_engine is not None and previous_engine is not engine:
                await self._detach_engine_locked(previous_engine)
            self._engine_generation += 1
            self._engine = engine
            self._detaching_engine = False
            register_adapter = getattr(engine, "register_adapter", None)
            if callable(register_adapter):
                register_adapter(
                    self,
                    adapter_type=self.adapter_type,
                    group_ids=self._get_allowed_group_ids(),
                    private_user_ids=self._get_allowed_private_user_ids(),
                )
            self._get_dispatcher()
            if self._event_bus_task is None or self._event_bus_task.done():
                self._event_bus_task = asyncio.create_task(self._event_bus_listener())
            self.on_event(self._on_event)
            LOG.info("NapCatAdapter 平台集成已启动")

    async def rebind_engine(self, engine: Any) -> None:
        """Switch the adapter and event listener to a rebuilt engine."""
        async with self._engine_lifecycle_lock:
            previous_engine = self._engine
            # Detach first so a raw event arriving during the handoff cannot
            # work against the old engine or the not-yet-registered new one.
            await self._detach_engine_locked(previous_engine)
            self._engine_generation += 1
            self._engine = engine
            self._detaching_engine = False
            register_adapter = getattr(engine, "register_adapter", None)
            if callable(register_adapter):
                register_adapter(
                    self,
                    adapter_type=self.adapter_type,
                    group_ids=self._get_allowed_group_ids(),
                    private_user_ids=self._get_allowed_private_user_ids(),
                )
            if self._running:
                self._event_bus_task = asyncio.create_task(self._event_bus_listener())

    async def stop_handling(self) -> None:
        """停止事件处理和引擎事件总线监听。"""
        async with self._engine_lifecycle_lock:
            self._running = False
            await self._detach_engine_locked(self._engine)
            if self._dispatcher is not None:
                self._dispatcher.close()
                self._dispatcher = None
            LOG.info("NapCatAdapter 平台集成已停止")

    async def _detach_engine_locked(self, engine: Any | None) -> None:
        """Stop listeners and handlers before changing engine ownership."""
        self._detaching_engine = True
        self._engine_generation += 1
        self._engine = None
        task = self._event_bus_task
        self._event_bus_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Drain handlers before retries: an active inbound handler can still
        # schedule a retry while it is being cancelled.
        await self._cancel_event_handler_tasks()
        await self._cancel_dispatch_retry_tasks()
        self._release_dispatch_leases()
        self._dispatch_delivery_active.clear()
        self._dispatch_leases.clear()
        if engine is not None:
            unregister_adapter = getattr(engine, "unregister_adapter", None)
            if callable(unregister_adapter):
                unregister_adapter(self)

    async def connect(self) -> None:
        """建立 WebSocket 连接并启动监听循环。"""
        self._running = True
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def close(self) -> None:
        """关闭连接并清理资源。"""
        await self.stop_handling()
        self._running = False
        for echo, future in list(self._pending.items()):
            if not future.done():
                future.cancel()
        self._pending.clear()

        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None
        await self._disconnect()

    def _get_dispatcher(self) -> GroupDispatcher | None:
        if self._dispatcher is not None:
            return self._dispatcher
        if not bool(self.plugin_config.get("group_dispatch_enabled", True)):
            return None
        worker_id = str(self.plugin_config.get("persona_name", "") or "").strip()
        if not worker_id:
            # Standalone adapter tests and legacy direct users have no stable
            # worker identity; leave their historical behavior unchanged.
            return None
        db_path = str(self.plugin_config.get("dispatch_db_path", "") or "").strip()
        if not db_path:
            db_path = str(self._work_path.parent.parent / "dispatcher" / "dispatcher.db")
        self._dispatcher = GroupDispatcher(
            db_path,
            worker_id=worker_id,
            account_id=str(self.plugin_config.get("qq_number", "") or ""),
            priority=float(self.plugin_config.get("dispatch_priority", 0.0)),
            min_reply_interval_seconds=float(
                self.plugin_config.get("dispatch_min_reply_interval_seconds", 3.0)
            ),
            lease_seconds=float(self.plugin_config.get("dispatch_lease_seconds", 120.0)),
            peer_cooldown_seconds=float(
                self.plugin_config.get("dispatch_peer_cooldown_seconds", 5.0)
            ),
            max_peer_turns=int(self.plugin_config.get("dispatch_max_peer_turns", 3)),
            score_collection_seconds=float(
                self.plugin_config.get("dispatch_score_collection_seconds", 0.15)
            ),
            activity_window_seconds=float(
                self.plugin_config.get("dispatch_activity_window_seconds", 300.0)
            ),
            activity_penalty_per_reply=float(
                self.plugin_config.get("dispatch_activity_penalty_per_reply", 0.12)
            ),
            max_activity_penalty=float(
                self.plugin_config.get("dispatch_max_activity_penalty", 0.6)
            ),
        )
        return self._dispatcher

    async def _connect_once(self) -> bool:
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            LOG.info("Connecting to NapCat WS: %s", self.ws_url)
            self.ws = await websockets.connect(self.ws_url, additional_headers=headers)
            LOG.info("NapCat WS connected")
            return True
        except Exception as exc:
            LOG.warning("NapCat WS connect failed: %s", exc)
            return False

    async def _disconnect(self) -> None:
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

    async def _reconnect_loop(self) -> None:
        """自动重连循环：连接断开后指数退避重试，默认永不放弃。

        放弃重连等于「AI 静默离线但 UI 仍显示运行中」——这是最难被发现的一类
        故障。因此默认 ``_MAX_RECONNECT_ATTEMPTS = 0``（无限重试），并在断线
        持续期间周期性升格日志级别，让运维能从日志看出适配器已经离线多久。
        """
        delay = self._RECONNECT_BASE_DELAY
        attempts = 0
        while self._running:
            if self.ws is None or _is_ws_closed(self.ws):
                if await self._connect_once():
                    if attempts > 0:
                        LOG.warning("NapCat WS 已恢复连接（此前失败 %d 次）", attempts)
                    delay = self._RECONNECT_BASE_DELAY
                    attempts = 0
                    self._offline_since = None
                    self._reconnect_exhausted = False
                    self._listen_task = asyncio.create_task(self._listen_loop())
                    # 等待监听任务结束（连接断开）
                    try:
                        if self._listen_task:
                            await self._listen_task
                    except asyncio.CancelledError:
                        break
                    except Exception as exc:
                        LOG.warning("Listen task ended: %s", exc)
                else:
                    if (
                        self._MAX_RECONNECT_ATTEMPTS > 0
                        and attempts >= self._MAX_RECONNECT_ATTEMPTS
                    ):
                        LOG.error(
                            "NapCat WS 重连次数耗尽 (%s 次)，适配器已离线；需重启进程恢复",
                            self._MAX_RECONNECT_ATTEMPTS,
                        )
                        self._offline_since = self._offline_since or time.monotonic()
                        self._reconnect_exhausted = True
                        break
                    self._offline_since = self._offline_since or time.monotonic()
                    attempts += 1
                    # 持续断线期间按固定间隔播报一次，避免刷屏又不会没人发现。
                    if time.monotonic() - self._last_offline_log >= self._OFFLINE_LOG_INTERVAL:
                        self._last_offline_log = time.monotonic()
                        LOG.error(
                            "NapCat WS 离线中（已失败 %d 次，持续 %.0f 秒），继续重试",
                            attempts,
                            time.monotonic() - self._offline_since,
                        )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self._RECONNECT_MAX_DELAY)
            else:
                self._offline_since = None
                await asyncio.sleep(self.reconnect_interval)

    @property
    def is_online(self) -> bool:
        """WebSocket 当前是否处于已连接状态。"""
        return self.ws is not None and not _is_ws_closed(self.ws)

    def connection_state(self) -> dict[str, Any]:
        """供 WebUI / 监控读取的连接健康快照。"""
        offline_seconds = (
            round(time.monotonic() - self._offline_since, 1)
            if self._offline_since is not None
            else 0.0
        )
        return {
            "online": self.is_online,
            "reconnect_exhausted": self._reconnect_exhausted,
            "offline_seconds": offline_seconds,
        }

    # ─── 事件分发 ─────────────────────────────────────────

    def on_event(self, handler: EventHandler) -> None:
        """注册事件处理器。"""
        if handler in self._event_handlers:
            return
        self._event_handlers.append(handler)

    def _message_dedup_key(self, event: dict[str, Any]) -> str:
        msg_id = str(event.get("message_id", "") or "")
        if not msg_id:
            return ""
        msg_type = str(event.get("message_type", "") or "")
        if msg_type == "group":
            scope = f"group:{event.get('group_id', '')}"
        else:
            scope = f"private:{event.get('user_id', '')}"
        return f"{event.get('self_id', '')}:{msg_type}:{scope}:{msg_id}"

    def _is_duplicate_message_event(self, event: dict[str, Any]) -> bool:
        key = self._message_dedup_key(event)
        if not key:
            return False
        now = time.monotonic()
        cutoff = now - self._seen_message_ttl
        for seen_key, seen_at in list(self._seen_message_ids.items()):
            if seen_at < cutoff:
                self._seen_message_ids.pop(seen_key, None)
        if key in self._seen_message_ids:
            LOG.info("Skip duplicate NapCat message event: %s", key)
            return True
        self._seen_message_ids[key] = now
        return False

    @staticmethod
    def _is_poke_event(event: dict[str, Any]) -> bool:
        return (
            event.get("post_type") == "notice"
            and event.get("notice_type") == "notify"
            and event.get("sub_type") == "poke"
        )

    async def _listen_loop(self) -> None:
        """WebSocket 消息监听与分发。"""
        if self.ws is None:
            return
        try:
            async for raw in self.ws:
                if not self._running:
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(data)
        except websockets.exceptions.ConnectionClosed as exc:
            LOG.info("NapCat WS closed: code=%s reason=%s", exc.code, exc.reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.exception("NapCat WS listen error: %s", exc)

    async def _dispatch(self, data: dict[str, Any]) -> None:
        """分发入站消息：API 响应交给 pending future，事件交给 handlers。"""
        echo = data.get("echo")
        if echo and echo in self._pending:
            try:
                self._pending[echo].set_result(data)
            except asyncio.InvalidStateError:
                pass
            return

        if self._detaching_engine:
            return
        generation = self._engine_generation
        for handler in tuple(self._event_handlers):
            try:
                self._track_task(
                    asyncio.create_task(self._run_raw_event_handler(handler, data, generation))
                )
            except Exception:
                LOG.exception("Event handler error")

    # ── 消息发送限流（每群/私聊独立） ────────────────────

    @staticmethod
    def _is_send_action(action: str) -> bool:
        return action in (
            "send_group_msg",
            "send_private_msg",
            "send_group_forward_msg",
            "send_private_forward_msg",
        )

    @staticmethod
    def _is_irreversible_action(action: str) -> bool:
        return action in (
            "send_group_msg",
            "send_private_msg",
            "send_group_forward_msg",
            "send_private_forward_msg",
            "group_poke",
            "friend_poke",
        )

    def _send_channel_key(self, action: str, params: dict[str, Any]) -> str:
        if action in ("send_group_msg", "send_group_forward_msg"):
            return f"group_{params.get('group_id', '')}"
        if action in ("send_private_msg", "send_private_forward_msg"):
            return f"private_{params.get('user_id', '')}"
        return ""

    # ─── API 调用 ─────────────────────────────────────────

    async def call_api(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """通过 WebSocket 发送 OneBot API 请求并等待响应。

        消息发送内置限流：每群/私聊每秒最多 1 条，各频道独立。
        """
        if self._is_send_action(action):
            channel = self._send_channel_key(action, params)
            async with self._api_send_lock:
                last = self._last_api_call_at.get(channel, 0.0)
                elapsed = time.monotonic() - last
                if elapsed < 1.0:
                    await asyncio.sleep(1.0 - elapsed)
                resp = await self._call_api_inner(action, params)
                self._last_api_call_at[channel] = time.monotonic()
                return resp
        return await self._call_api_inner(action, params)

    async def _call_api_inner(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """发送 API 请求核心逻辑（不含限流）。"""
        if not self.ws or _is_ws_closed(self.ws) or not self._running:
            raise RuntimeError("WebSocket not connected")

        self._echo_counter += 1
        echo = f"req_{self._echo_counter}_{action}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[echo] = future

        payload = {"action": action, "params": params, "echo": echo}
        atomic_irreversible = self._is_irreversible_action(action) and _ATOMIC_PROACTIVE_SEND.get()
        try:
            delivery_start = _PROACTIVE_DELIVERY_START.get()
            if atomic_irreversible and callable(delivery_start):
                if not delivery_start():
                    raise RuntimeError("proactive delivery lease expired before platform I/O")
            try:
                await self.ws.send(json.dumps(payload))
            except asyncio.CancelledError as exc:
                if atomic_irreversible:
                    raise _OneBotDeliveryUncertain(
                        f"WebSocket send was cancelled with unknown outcome: {action}"
                    ) from exc
                raise
            except Exception as exc:
                if atomic_irreversible:
                    raise _OneBotDeliveryUncertain(
                        f"WebSocket send outcome is unknown: {action}"
                    ) from exc
                raise RuntimeError(f"Failed to send API request: {exc}") from exc

            try:
                resp = await asyncio.wait_for(future, timeout=self.api_timeout)
            except asyncio.CancelledError as exc:
                if atomic_irreversible:
                    raise _OneBotDeliveryUncertain(
                        f"API acknowledgement wait was cancelled: {action}"
                    ) from exc
                raise
            except asyncio.TimeoutError as exc:
                if atomic_irreversible:
                    raise _OneBotDeliveryUncertain(f"API timeout after send: {action}") from exc
                raise RuntimeError(f"API timeout: {action}") from exc
        finally:
            self._pending.pop(echo, None)
            if not future.done():
                future.cancel()

        if resp.get("status") not in {"ok", "async"}:
            retcode = resp.get("retcode", -1)
            wording = resp.get("wording", "unknown error")
            raise RuntimeError(f"API error: {action} retcode={retcode} {wording}")
        return resp

    # ─── BaseAdapter 接口实现 ──────────────────────────────

    async def send_group_message(
        self, group_id: str, message: MessageGroup | str
    ) -> dict[str, Any]:
        """发送群聊消息（平台无关接口）。"""
        self._require_allowed_group(str(group_id))
        segments = self._message_group_to_onebot(message)
        return await self.call_api(
            "send_group_msg", {"group_id": int(group_id), "message": segments}
        )

    async def send_private_message(
        self, user_id: str, message: MessageGroup | str
    ) -> dict[str, Any]:
        """发送私聊消息（平台无关接口）。"""
        segments = self._message_group_to_onebot(message)
        return await self.call_api(
            "send_private_msg", {"user_id": int(user_id), "message": segments}
        )

    # ─── 旧方法（保留兼容） ────────────────────────────────

    async def send_group_msg(
        self, group_id: str | int, message: list[dict[str, Any]] | str
    ) -> dict[str, Any]:
        """发送群消息（OneBot 接口）。message 为字符串时自动包装。"""
        self._require_allowed_group(str(group_id))
        segments = self._to_segments(message)
        return await self.call_api(
            "send_group_msg", {"group_id": int(group_id), "message": segments}
        )

    async def send_private_msg(
        self, user_id: str | int, message: list[dict[str, Any]] | str
    ) -> dict[str, Any]:
        """发送私聊消息（OneBot 接口）。"""
        segments = self._to_segments(message)
        return await self.call_api(
            "send_private_msg", {"user_id": int(user_id), "message": segments}
        )

    # ─── 合并转发（NapCat Go-CQHTTP 接口） ──────────────────

    async def send_group_forward_msg(self, group_id: str | int, text: str) -> dict[str, Any]:
        """发送群聊合并转发消息，节点正文来自 *text*。"""
        self._require_allowed_group(str(group_id))
        return await self.call_api(
            "send_group_forward_msg",
            {"group_id": int(group_id), "messages": self._build_forward_nodes(text)},
        )

    async def send_private_forward_msg(self, user_id: str | int, text: str) -> dict[str, Any]:
        """发送私聊合并转发消息，节点正文来自 *text*。"""
        return await self.call_api(
            "send_private_forward_msg",
            {"user_id": int(user_id), "messages": self._build_forward_nodes(text)},
        )

    def _build_forward_nodes(self, text: str) -> list[dict[str, Any]]:
        """把纯文本包装成合并转发节点，署名保持当前登录账号。"""
        uin = str(self.plugin_config.get("qq_number", "") or "").strip()
        nickname = self._persona_name or uin or "Sirius"
        return [
            {
                "type": "node",
                "data": {
                    "uin": uin,
                    "nickname": nickname,
                    "content": [{"type": "text", "data": {"text": chunk}}],
                },
            }
            for chunk in _split_forward_node_texts(text)
        ]

    @staticmethod
    def _to_file_uri(file_path: str) -> str:
        """将本地路径转换为 file:// URI，解决中文路径被 NapCat URI 解析器拒绝的问题。"""
        from urllib.request import pathname2url

        p = Path(file_path).resolve()
        return "file://" + pathname2url(str(p))

    @staticmethod
    def _shared_upload_root() -> Path:
        """返回 Sirius 与 NapCat 约定的文件上传暂存目录。"""
        return Path(os.getenv("SIRIUS_NAPCAT_UPLOAD_ROOT", "/app/data/napcat-upload"))

    @staticmethod
    def _napcat_upload_root() -> str:
        """返回上述暂存目录在 NapCat 容器中的只读挂载位置。"""
        return os.getenv("SIRIUS_NAPCAT_UPLOAD_TARGET_ROOT", "/sirius-upload").rstrip("/")

    @staticmethod
    def _local_file_path(file_path: str) -> Path:
        """将本地路径或 file:// URI 规范化为当前容器可读的路径。"""
        if file_path.startswith("file://"):
            return Path(unquote(urlparse(file_path).path)).expanduser()
        return Path(file_path).expanduser()

    @classmethod
    async def _prepare_upload_file(cls, file_path: str) -> tuple[str, Path | None]:
        """暂存本地文件，并返回 NapCat 可访问的引用和待清理副本。"""
        reference = str(file_path or "").strip()
        if reference.startswith(("http://", "https://", "base64://")):
            return reference, None

        source = cls._local_file_path(reference)
        if not source.is_file():
            return cls._to_file_uri(str(source)), None

        source = source.resolve()
        shared_root = cls._shared_upload_root().resolve()
        try:
            relative_path = source.relative_to(shared_root)
            temporary_path = None
        except ValueError:
            await asyncio.to_thread(shared_root.mkdir, parents=True, exist_ok=True)
            temporary_path = shared_root / f"{uuid4().hex}_{source.name}"
            try:
                await asyncio.to_thread(shutil.copyfile, source, temporary_path)
            except BaseException:
                await cls._cleanup_staged_upload(temporary_path)
                raise
            relative_path = temporary_path.relative_to(shared_root)

        target_root = cls._napcat_upload_root() or "/sirius-upload"
        target_path = f"{target_root}/{relative_path.as_posix()}"
        return f"file://{quote(target_path, safe='/')}", temporary_path

    @staticmethod
    async def _cleanup_staged_upload(temporary_path: Path | None) -> None:
        if temporary_path is None:
            return
        try:
            await asyncio.to_thread(temporary_path.unlink)
        except OSError as exc:
            LOG.warning("清理 NapCat 文件上传暂存副本失败: %s | %s", temporary_path, exc)

    async def upload_group_file(
        self, group_id: str | int, file_path: str, name: str = ""
    ) -> dict[str, Any]:
        """上传文件到群文件。"""
        file_reference, temporary_path = await self._prepare_upload_file(file_path)
        try:
            return await self.call_api(
                "upload_group_file",
                {
                    "group_id": int(group_id),
                    "file": file_reference,
                    "name": name or Path(file_path).name,
                },
            )
        finally:
            await self._cleanup_staged_upload(temporary_path)

    async def upload_private_file(
        self, user_id: str | int, file_path: str, name: str = ""
    ) -> dict[str, Any]:
        """上传文件到私聊。"""
        file_reference, temporary_path = await self._prepare_upload_file(file_path)
        try:
            return await self.call_api(
                "upload_private_file",
                {
                    "user_id": int(user_id),
                    "file": file_reference,
                    "name": name or Path(file_path).name,
                },
            )
        finally:
            await self._cleanup_staged_upload(temporary_path)

    async def get_group_file_list(
        self, group_id: str | int, folder_id: str = "", file_count: int = 50
    ) -> dict[str, Any]:
        """获取群根目录或指定文件夹下的文件列表。"""
        params: dict[str, Any] = {
            "group_id": int(group_id),
            "file_count": max(1, int(file_count)),
        }
        if folder_id:
            params["folder_id"] = str(folder_id)
            action = "get_group_files_by_folder"
        else:
            action = "get_group_root_files"
        response = await self.call_api(action, params)
        return response.get("data", {}) or {}

    async def download_group_file(
        self,
        group_id: str | int,
        file_id: str,
        file_name: str = "",
        destination_dir: str = "",
    ) -> dict[str, Any]:
        """获取群文件 URL 并下载到当前人格的 group_files 目录。"""
        response = await self.call_api(
            "get_group_file_url",
            {"group_id": int(group_id), "file_id": str(file_id)},
        )
        url = str((response.get("data", {}) or {}).get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            raise RuntimeError("NapCat 未返回有效的群文件下载链接")

        output_dir = (
            Path(destination_dir).expanduser()
            if destination_dir
            else self._work_path / "group_files"
        )
        safe_name = _safe_download_name(file_name)
        if safe_name in {".", ".."}:
            safe_name = ""
        safe_name = safe_name or str(file_id)
        output_path = output_dir / safe_name
        temporary_path = output_path.with_name(f".{output_path.name}.{uuid4().hex}.part")
        await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
        try:
            size = await asyncio.to_thread(_download_url, url, temporary_path)
            await asyncio.to_thread(temporary_path.replace, output_path)
        except BaseException:
            await asyncio.to_thread(_remove_file_if_exists, temporary_path)
            raise
        return {"path": str(output_path.resolve()), "file_name": safe_name, "size": size}

    async def get_group_member_info(
        self, group_id: str | int, user_id: str | int, no_cache: bool = False
    ) -> dict[str, Any]:
        """获取群成员信息。"""
        resp = await self.call_api(
            "get_group_member_info",
            {"group_id": int(group_id), "user_id": int(user_id), "no_cache": no_cache},
        )
        return resp.get("data", {}) or {}

    async def get_group_member_list(self, group_id: str | int) -> list[dict[str, Any]]:
        """获取群成员列表。"""
        resp = await self.call_api("get_group_member_list", {"group_id": int(group_id)})
        return resp.get("data", []) or []

    async def get_group_msg_history(
        self, group_id: str, message_seq: int | None = None, count: int = 20
    ) -> list[dict[str, Any]]:
        """获取群聊历史消息（OneBot v11 API）。"""
        params: dict[str, Any] = {"group_id": int(group_id), "count": count}
        if message_seq is not None:
            params["message_seq"] = message_seq
        resp = await self.call_api("get_group_msg_history", params)
        return resp.get("data", {}).get("messages", []) or []

    async def get_login_info(self) -> dict[str, Any]:
        """获取登录信息。"""
        resp = await self.call_api("get_login_info", {})
        return resp.get("data", {}) or {}

    @staticmethod
    def _to_segments(message: list[dict[str, Any]] | str) -> list[dict[str, Any]]:
        if isinstance(message, str):
            return [{"type": "text", "data": {"text": message}}]
        return message

    @staticmethod
    def _message_group_to_onebot(message: MessageGroup | str) -> list[dict[str, Any]]:
        """将 MessageGroup 转换为 OneBot v11 消息段数组。"""
        if isinstance(message, str):
            return [{"type": "text", "data": {"text": message}}]

        segments: list[dict[str, Any]] = []
        for seg in message:
            if isinstance(seg, TextSegment):
                segments.append({"type": "text", "data": {"text": seg.text}})
            elif isinstance(seg, AtSegment):
                segments.append({"type": "at", "data": {"qq": seg.user_id}})
            elif isinstance(seg, ImageSegment):
                img_data: dict[str, str] = {"file": seg.file_path}
                if seg.url:
                    img_data["url"] = seg.url
                if seg.sub_type:
                    img_data["sub_type"] = seg.sub_type
                segments.append({"type": "image", "data": img_data})
            elif isinstance(seg, VoiceSegment):
                segments.append({"type": "record", "data": {"file": seg.file_path}})
            elif isinstance(seg, ReplySegment):
                segments.append({"type": "reply", "data": {"id": seg.message_id}})
            elif isinstance(seg, FileSegment):
                segments.append(
                    {
                        "type": "file",
                        "data": {
                            "file": seg.file_path,
                            "name": seg.name or Path(seg.file_path).name,
                        },
                    }
                )
        return segments

    # ─── 事件解析（OneBot → 引擎格式） ──────────────────────

    async def parse_event(self, raw_event: dict[str, Any]) -> "ParsedEvent | None":
        """将原始 OneBot 事件解析为引擎可消费的结构化格式。

        包含：表情→文字转换、@→昵称替换、图片标签生成。
        """
        from sirius_pulse.adapters.models import ParsedEvent

        post_type = raw_event.get("post_type")
        is_poke = self._is_poke_event(raw_event)
        if post_type != "message" and not is_poke:
            return None

        msg_type = raw_event.get("message_type", "")
        if is_poke:
            msg_type = "group" if raw_event.get("group_id") else "private"
        uid = str(raw_event.get("user_id", ""))
        self_id = str(raw_event.get("self_id", ""))

        if msg_type == "group":
            gid = str(raw_event.get("group_id", ""))
            if not self._is_group_allowed(gid):
                return None
        elif msg_type == "private":
            if not self._is_private_user_allowed(uid):
                return None
            gid = f"private_{uid}"
        else:
            gid = ""

        nickname, card = self.extract_sender_names(raw_event)

        # 被引用消息携带的图片（引用回复场景下模型也需要看到）
        quote_images: list[dict[str, str]] = []
        if is_poke:
            prompt = self._render_poke_prompt(raw_event, self_id)
        elif msg_type == "group":
            prompt = await self._render_group_prompt(raw_event, self_id, gid, quote_images)
        elif msg_type == "private":
            prompt = await self._render_private_prompt(raw_event, quote_images)
        else:
            return None

        if not prompt:
            return None

        # 提取 @ 提及目标
        at_user_ids: list[str] = []
        mention_all = False
        for seg in raw_event.get("message", []):
            if seg.get("type") == "at":
                at_qq = str(seg.get("data", {}).get("qq", ""))
                if at_qq == "all":
                    mention_all = True
                elif at_qq:
                    at_user_ids.append(at_qq)

        multimodal_inputs = quote_images + await self._collect_image_inputs(
            raw_event.get("message", [])
        )

        try:
            event_time = int(raw_event.get("time", 0) or 0)
        except (TypeError, ValueError):
            event_time = 0

        # 提取平台消息 ID（只用于引用回复，不能作为跨账号调度 ID）
        msg_id = str(raw_event.get("message_id", ""))
        poke_target_id = str(raw_event.get("target_id", "")) if is_poke else ""
        if is_poke and not msg_id:
            msg_id = "poke-{}-{}-{}".format(raw_event.get("time", ""), uid, poke_target_id)

        return ParsedEvent(
            group_id=gid,
            user_id=uid,
            self_id=self_id,
            message_type=msg_type,
            prompt=prompt,
            nickname=nickname,
            card=card,
            message_id=msg_id,
            event_time=event_time,
            multimodal_inputs=multimodal_inputs,
            at_user_ids=at_user_ids,
            mention_all=mention_all,
            poke_target_id=poke_target_id,
        )

    def _render_poke_prompt(self, event: dict[str, Any], self_id: str) -> str:
        """Render a OneBot poke notice as normal model-readable text."""
        target_id = str(event.get("target_id", ""))
        target_name = self._persona_name if target_id == self_id else f"qq_{target_id}"
        return f"戳了一下 {target_name}"

    async def _render_group_prompt(
        self,
        event: dict[str, Any],
        self_id: str,
        group_id: str,
        quote_images: list[dict[str, str]] | None = None,
    ) -> str:
        """将群聊 OneBot 消息段渲染为引擎可读的 prompt 文本。"""
        from ..protocol import _face_to_text, build_image_label

        parts: list[str] = []
        mention_cache: dict[str, str] = {}
        image_index = 1
        image_names: dict[str, int] = {}

        for seg in event.get("message", []):
            seg_type = seg.get("type")
            data = seg.get("data", {})
            if seg_type == "reply":
                # 引用消息：尝试获取被引用消息内容并注入 prompt
                quote_text = await self._resolve_quote_content(data, quote_images)
                if quote_text:
                    parts.append(quote_text)
            elif seg_type == "text":
                parts.append(data.get("text", ""))
            elif seg_type == "at":
                target_uid = str(data.get("qq", ""))
                if target_uid == "all":
                    parts.append("@全体成员")
                    continue
                if target_uid not in mention_cache:
                    if target_uid == self_id:
                        display = self._persona_name
                    else:
                        display = f"qq_{target_uid}"
                        try:
                            info = await self.get_group_member_info(group_id, target_uid)
                            card = str(info.get("card", "") or "").strip()
                            nickname = str(info.get("nickname", "") or "").strip()
                            if nickname and card and nickname != card:
                                display = f"{nickname}(群昵称为{card})"
                            else:
                                display = nickname or card or display
                        except Exception:
                            pass
                    mention_cache[target_uid] = display
                parts.append(f"@{mention_cache[target_uid]}")
            elif seg_type == "face":
                parts.append(_face_to_text(data))
            elif seg_type == "image":
                label = "动画表情" if str(data.get("sub_type", "")) == "1" else "图片"
                parts.append(build_image_label(seg, image_index, label, image_names))
                image_index += 1

        return "".join(parts).strip()

    async def _resolve_quote_content(
        self,
        data: dict[str, Any],
        quote_images: list[dict[str, str]] | None = None,
    ) -> str:
        """解析引用消息段，通过 get_msg API 获取被引用消息内容。

        被引用消息中的图片会缓存到本地并追加进 ``quote_images``，使其作为真实
        视觉输入进入多模态通道（仅文本注入时模型无法「看到」引用的图）。

        Returns:
            格式化的引用文本，如 ``[引用消息 msg_id="123" speaker="张三"] 内容 [/引用消息]``
            获取失败时返回空字符串。
        """
        from ..protocol import build_image_label

        msg_id = str(data.get("id", ""))
        if not msg_id:
            return ""
        try:
            resp = await self.call_api("get_msg", {"message_id": int(msg_id)})
            msg_data = resp.get("data", {})
            # 提取被引用消息的文本与图片标签
            raw_segments = msg_data.get("message", [])
            text_parts: list[str] = []
            image_index = 1
            image_names: dict[str, int] = {}
            for seg in raw_segments:
                if seg.get("type") == "text":
                    text_parts.append(seg.get("data", {}).get("text", ""))
                elif seg.get("type") == "image":
                    is_sticker = str(seg.get("data", {}).get("sub_type", "")) == "1"
                    label = "动画表情" if is_sticker else "图片"
                    text_parts.append(build_image_label(seg, image_index, label, image_names))
                    image_index += 1
            if quote_images is not None:
                quote_images.extend(await self._collect_image_inputs(raw_segments))
            quote_text = "".join(text_parts).strip()
            if not quote_text:
                return ""
            # 提取发送者信息
            sender = msg_data.get("sender", {})
            nickname = sender.get("nickname", "") or sender.get("card", "") or ""
            safe_nick = html.escape(nickname, quote=True) if nickname else ""
            safe_msg_id = html.escape(msg_id, quote=True)
            # 截断过长的引用内容
            if len(quote_text) > 200:
                quote_text = quote_text[:200] + "..."
            safe_quote = html.escape(quote_text, quote=False)
            if safe_nick:
                return f'[引用消息 msg_id="{safe_msg_id}" speaker="{safe_nick}"]' f"{safe_quote}[/引用消息]"
            return f'[引用消息 msg_id="{safe_msg_id}"]{safe_quote}[/引用消息]'
        except Exception as exc:
            LOG.debug("获取引用消息失败 (msg_id=%s): %s", msg_id, exc)
            return ""

    async def _collect_image_inputs(self, segments: list[dict[str, Any]]) -> list[dict[str, str]]:
        """把 OneBot 消息段中的图片缓存到本地并转换为多模态输入项。

        只有真正拿到本地副本的图片才会进入多模态通道：下载失败时该图被丢弃，
        而不是退回原始平台地址（QQ 多媒体链接带短时效签名且校验 Referer，
        上游模型自行下载只会得到 403）。
        """
        inputs: list[dict[str, str]] = []
        for seg in segments:
            if seg.get("type") != "image":
                continue
            data = seg.get("data", {})
            url = data.get("url", "") or data.get("file", "")
            if not url:
                continue
            is_sticker = str(data.get("sub_type", "")) == "1"
            local_path = await self._resolve_segment_image(data, is_sticker=is_sticker)
            if not local_path:
                LOG.warning("图片无法本地化，已跳过视觉输入: %s", str(url)[:80])
                continue
            item: dict[str, str] = {
                "type": "image",
                "value": local_path,
                "file_path": local_path,
            }
            if is_sticker:
                item["sub_type"] = "1"
            inputs.append(item)
        return inputs

    async def _resolve_segment_image(self, data: dict[str, Any], *, is_sticker: bool) -> str:
        """把一个 OneBot 图片段落成本地可用引用，失败返回空字符串。"""
        primary = str(data.get("url", "") or data.get("file", "")).strip()
        file_ref = str(data.get("file", "")).strip()

        # 非网络地址（本地路径/数据地址）已经是可用输入，无需下载或兜底。
        if primary and not primary.startswith(("http://", "https://")):
            return primary

        candidates: list[str] = []
        for candidate in (primary, file_ref):
            if candidate and candidate.startswith(("http://", "https://")):
                if candidate not in candidates:
                    candidates.append(candidate)

        for candidate in candidates:
            local_path = await self.cache_image(candidate, is_sticker=is_sticker)
            if local_path:
                return local_path

        return await self._recover_image_via_api(file_ref or primary, is_sticker=is_sticker)

    async def _recover_image_via_api(self, file_ref: str, *, is_sticker: bool) -> str:
        """直连下载失败时，用 NapCat 的 get_image 取回图片。

        NapCat 与该进程可能不在同一文件系统上，因此只有当它给出的路径在本机
        确实可读时才会采用；否则退回按 URL 再缓存一次。
        """
        file_ref = str(file_ref or "").strip()
        if not file_ref:
            return ""
        try:
            resp = await self.call_api("get_image", {"file": file_ref})
        except Exception as exc:
            LOG.debug("get_image 兜底失败 (%s): %s", file_ref[:60], exc)
            return ""

        payload = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(payload, dict):
            return ""
        for key in ("file", "path", "url"):
            value = str(payload.get(key, "") or "").strip()
            if not value:
                continue
            adopted = self._adopt_local_image(value, is_sticker=is_sticker)
            if adopted:
                return adopted
            if value.startswith(("http://", "https://")):
                cached = await self.cache_image(value, is_sticker=is_sticker)
                if cached:
                    return cached
        return ""

    def _adopt_local_image(self, value: str, *, is_sticker: bool) -> str:
        """把 NapCat 报出的本机图片文件收进人格图片缓存，失败返回空字符串。"""
        import hashlib

        source = _normalize_local_path(value)
        if source is None:
            return ""
        try:
            if not source.is_file():
                return ""
            data = source.read_bytes()
        except OSError as exc:
            LOG.debug("读取 NapCat 本地图片失败 (%s): %s", source, exc)
            return ""

        cache_dir = self._sticker_cache_dir if is_sticker else self._image_cache_dir
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            suffix = source.suffix or ".jpg"
            content_hash = hashlib.md5(data, usedforsecurity=False).hexdigest()
            cache_path = cache_dir / f"{content_hash}{suffix}"
            if not cache_path.exists():
                cache_path.write_bytes(data)
            return str(cache_path)
        except OSError as exc:
            LOG.debug("写入 NapCat 图片缓存失败 (%s): %s", source, exc)
            return ""

    async def _render_private_prompt(
        self,
        event: dict[str, Any],
        quote_images: list[dict[str, str]] | None = None,
    ) -> str:
        """将私聊 OneBot 消息段渲染为引擎可读的 prompt 文本。"""
        from ..protocol import _face_to_text, build_image_label

        parts: list[str] = []
        image_index = 1
        image_names: dict[str, int] = {}

        for seg in event.get("message", []):
            seg_type = seg.get("type")
            data = seg.get("data", {})
            if seg_type == "reply":
                quote_text = await self._resolve_quote_content(data, quote_images)
                if quote_text:
                    parts.append(quote_text)
            elif seg_type == "text":
                parts.append(data.get("text", ""))
            elif seg_type == "face":
                parts.append(_face_to_text(data))
            elif seg_type == "image":
                label = "动画表情" if str(data.get("sub_type", "")) == "1" else "图片"
                parts.append(build_image_label(seg, image_index, label, image_names))
                image_index += 1

        return "".join(parts).strip()

    async def _publish_group_metadata(self, group_id: str, self_id: str = "") -> None:
        """Refresh QQ group metadata used by prompt-time @ and admin-only tools."""
        engine = self._engine
        if engine is None or not group_id:
            return

        members = await self._get_cached_group_members(group_id)
        if members and hasattr(engine, "update_qq_group_members"):
            try:
                engine.update_qq_group_members(group_id, members)
            except Exception as exc:
                LOG.debug("更新 QQ 群成员缓存失败 (%s): %s", group_id, exc)

        is_admin = await self._get_cached_bot_admin(group_id, self_id, members)
        if hasattr(engine, "update_qq_bot_group_admin"):
            try:
                engine.update_qq_bot_group_admin(group_id, is_admin)
            except Exception as exc:
                LOG.debug("更新 QQ Bot 管理员缓存失败 (%s): %s", group_id, exc)

    async def _get_cached_group_members(self, group_id: str) -> list[dict[str, Any]]:
        now = time.monotonic()
        cached = self._group_member_cache.get(group_id)
        if cached and now - cached[0] < self._group_metadata_ttl:
            return list(cached[1])
        try:
            members = await self.get_group_member_list(group_id)
            self._group_member_cache[group_id] = (now, list(members))
            return list(members)
        except Exception as exc:
            LOG.debug("获取群成员列表失败 (%s): %s", group_id, exc)
            return list(cached[1]) if cached else []

    async def _get_cached_bot_admin(
        self,
        group_id: str,
        self_id: str = "",
        members: list[dict[str, Any]] | None = None,
    ) -> bool:
        now = time.monotonic()
        cached = self._bot_admin_cache.get(group_id)
        if cached and now - cached[0] < self._group_metadata_ttl:
            return cached[1]

        role = ""
        if self_id:
            for member in members or []:
                if str(member.get("user_id", "")) == str(self_id):
                    role = str(member.get("role", "") or "").strip()
                    break
            if not role:
                try:
                    info = await self.get_group_member_info(group_id, self_id)
                    role = str(info.get("role", "") or "").strip()
                except Exception as exc:
                    LOG.debug("获取 Bot 群身份失败 (%s/%s): %s", group_id, self_id, exc)

        is_admin = role in {"admin", "owner"}
        self._bot_admin_cache[group_id] = (now, is_admin)
        return is_admin

    # ─── 图片缓存 ───────────────────────────────────────────

    @staticmethod
    def _cache_image_headers() -> dict[str, str]:
        """NapCat 图片缓存需要 QQ 多媒体 Referer。"""
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.0"
            ),
            "Referer": "https://multimedia.nt.qq.com.cn/",
        }

    # ─── 配置与权限 ───────────────────────────────────────────

    @property
    def _persona_name(self) -> str:
        return getattr(self, "_persona_name_val", "") or ""

    def set_persona_name(self, name: str) -> None:
        self._persona_name_val = name

    def _is_admin(self, uid: str) -> bool:
        return uid == str(self.plugin_config.get("root", "")).strip()

    def _get_allowed_group_ids(self) -> list[str]:
        gids = self.plugin_config.get("allowed_group_ids", [])
        if isinstance(gids, str):
            try:
                parsed = json.loads(gids)
                if isinstance(parsed, list):
                    return [str(g).strip() for g in parsed if g]
            except (json.JSONDecodeError, ValueError):
                pass
            if "," in gids:
                return [g.strip().strip("'\"[]()") for g in gids.split(",") if g.strip()]
            return [gids.strip()] if gids.strip() else []
        return [str(g).strip() for g in gids if g]

    def get_configured_group_ids(self) -> list[str]:
        """Expose the current group destinations to generic engine routing."""
        return self._get_allowed_group_ids()

    def get_configured_private_user_ids(self) -> list[str]:
        """Expose the current private destinations to generic engine routing."""
        return self._get_allowed_private_user_ids()

    def _get_allowed_private_user_ids(self) -> list[str]:
        """Return normalized private-chat destinations from adapter config."""
        uids = self.plugin_config.get("allowed_private_user_ids", [])
        if isinstance(uids, str):
            try:
                parsed = json.loads(uids)
                if isinstance(parsed, list):
                    return [
                        str(value).strip().removeprefix("private_").removeprefix("qq_")
                        for value in parsed
                        if str(value).strip()
                    ]
                if isinstance(parsed, (str, int, float)) and not isinstance(parsed, bool):
                    value = str(parsed).strip().removeprefix("private_").removeprefix("qq_")
                    return [value] if value else []
            except (json.JSONDecodeError, ValueError):
                pass
            if "," in uids:
                return [
                    value.strip().strip("'\\\"[]()").removeprefix("private_").removeprefix("qq_")
                    for value in uids.split(",")
                    if value.strip()
                ]
            value = uids.strip().removeprefix("private_").removeprefix("qq_")
            return [value] if value else []
        if not isinstance(uids, (list, tuple, set)):
            return []
        return [
            str(value).strip().removeprefix("private_").removeprefix("qq_")
            for value in uids
            if str(value).strip()
        ]

    def _is_private_user_allowed(self, user_id: str) -> bool:
        allowed = self._get_allowed_private_user_ids()
        normalized_user_id = str(user_id or "").strip()
        normalized_user_id = normalized_user_id.removeprefix("private_").removeprefix("qq_")
        return bool(self.plugin_config.get("enable_private_chat", True)) and (
            not allowed or normalized_user_id in set(allowed)
        )

    def is_proactive_destination_allowed(self, destination_id: str) -> bool:
        """Return whether a proactive event may target this destination."""
        if not self._enabled:
            return False
        destination = str(destination_id or "").strip()
        if destination.startswith("private_"):
            user_id = destination.removeprefix("private_").removeprefix("qq_")
            return self._is_private_user_allowed(user_id)
        return self._is_group_allowed(destination)

    def _is_group_allowed(self, group_id: str) -> bool:
        normalized_group_id = str(group_id or "").strip()
        return bool(self.plugin_config.get("enable_group_chat", True)) and (
            normalized_group_id in set(self._get_allowed_group_ids())
        )

    def _require_allowed_group(self, group_id: str) -> None:
        if not self._is_group_allowed(group_id):
            raise PermissionError(f"群 {group_id} 不在允许列表中")

    @staticmethod
    def extract_sender_names(event: dict[str, Any]) -> tuple[str, str]:
        from ..protocol import extract_sender_names

        return extract_sender_names(event)

    # ─── 事件入口 ─────────────────────────────────────────

    async def _on_event(self, event: dict[str, Any]) -> None:
        post_type = event.get("post_type")
        is_poke = self._is_poke_event(event)
        if post_type != "message" and not is_poke:
            return
        if post_type == "message" and self._is_duplicate_message_event(event):
            return
        msg_type = event.get("message_type")
        if is_poke:
            msg_type = "group" if event.get("group_id") else "private"
        if msg_type == "group" and not self._is_group_allowed(str(event.get("group_id", ""))):
            LOG.debug("忽略不在群白名单中的消息: group=%s", event.get("group_id", ""))
            return
        if msg_type == "private" and not self._is_private_user_allowed(
            str(event.get("user_id", ""))
        ):
            LOG.debug("忽略不在私聊白名单中的消息: user=%s", event.get("user_id", ""))
            return
        if self._detaching_engine or bool(getattr(self._engine, "_runtime_retiring", False)):
            return
        self._event_queue.put_nowait(event)
        if msg_type == "group":
            await self._on_group_message(event)
        elif msg_type == "private":
            await self._on_private_message(event)

    async def _on_group_message(self, event: dict[str, Any]) -> None:
        uid = str(event.get("user_id", ""))
        self_id = str(event.get("self_id", ""))
        if uid == self_id:
            return
        if not self._enabled:
            return
        if self._engine is None or not self._engine_ready():
            self._log_not_ready()
            return
        if await self._try_inject_tool_chain_event(event):
            return
        await self._process_event(event)

    async def _try_inject_tool_chain_event(self, event: dict[str, Any]) -> bool:
        """Offer an explicitly addressed group message to an active tool chain."""
        engine = self._engine
        if engine is None:
            return False

        group_id = str(event.get("group_id", "") or "").strip()
        is_active = getattr(engine, "is_tool_chain_active", None)
        inject = getattr(engine, "inject_tool_chain_message", None)
        if (
            not group_id
            or not callable(is_active)
            or not is_active(group_id)
            or not callable(inject)
        ):
            return False

        parsed = await self.parse_event(event)
        if parsed is None or parsed.message_type != "group" or not parsed.prompt.strip():
            return False

        self_id = str(parsed.self_id or event.get("self_id", "") or "")
        qq_number = str(self.plugin_config.get("qq_number", "") or "").strip()
        mentions_current_bot = bool(
            (self_id and self_id in parsed.at_user_ids)
            or (qq_number and qq_number in parsed.at_user_ids)
        )
        uid = str(parsed.user_id or event.get("user_id", "") or "")
        peer_ai_ids = self.plugin_config.get("peer_ai_ids", [])
        is_peer_ai = uid in {str(value) for value in peer_ai_ids}
        participant = UnifiedUser(
            name=parsed.nickname or f"qq_{uid}",
            user_id=f"qq_{uid}",
            identities={"qq_native_sirius_pulse": uid},
            metadata={
                "platform": "qq",
                "qq_uid": uid,
                "is_developer": self._is_admin(uid),
                "is_ai": is_peer_ai,
                "group_id": group_id,
                "scope": "group",
            },
        )
        message = Message(
            role="user",
            content=parsed.prompt,
            speaker=parsed.card or parsed.nickname or f"qq_{uid}",
            nickname=parsed.nickname,
            channel="qq_native_sirius_pulse",
            channel_user_id=uid,
            group_id=group_id,
            message_id=parsed.message_id,
            multimodal_inputs=parsed.multimodal_inputs,
            adapter_type=self.adapter_type,
            adapter_route_id=self.adapter_route_id,
            sender_type="other_ai" if is_peer_ai else "human",
            mentions_current_bot=mentions_current_bot,
        )
        if not inject(message, [participant], group_id):
            return False
        event["_sirius_tool_chain_injected"] = True
        LOG.info("[工具链] 注入群聊文本: group=%s sender=%s", group_id, uid)
        return True

    async def _on_private_message(self, event: dict[str, Any]) -> None:
        uid = str(event.get("user_id", ""))
        self_id = str(event.get("self_id", ""))
        if uid == self_id:
            return
        if not self._enabled:
            return
        if self._engine is None or not self._engine_ready():
            self._log_not_ready()
            return
        await self._process_event(event)

    @staticmethod
    def _dispatch_event_id(parsed: "ParsedEvent") -> str:
        if parsed.event_time:
            raw = json.dumps(
                {
                    "group_id": parsed.group_id,
                    "user_id": parsed.user_id,
                    "message_type": parsed.message_type,
                    "prompt": parsed.prompt,
                    "at_user_ids": sorted(parsed.at_user_ids),
                    "mention_all": parsed.mention_all,
                    "poke_target_id": parsed.poke_target_id,
                    # Different NapCat instances can receive the same event a
                    # few seconds apart; the bucket keeps their dispatch ID aligned.
                    "event_time_bucket": parsed.event_time // _DISPATCH_EVENT_TIME_BUCKET_SECONDS,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
            return f"qq:{parsed.group_id}:event:{digest}"
        if parsed.message_id:
            return f"qq:{parsed.group_id}:{parsed.message_id}"
        raw = "|".join((parsed.group_id, parsed.user_id, parsed.prompt, parsed.message_type))
        return f"qq:{parsed.group_id}:hash:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"

    @staticmethod
    def _conversation_key(event: dict[str, Any]) -> str:
        """会话标识：群聊按群，私聊按人。同一会话内仍需串行。"""
        group_id = str(event.get("group_id") or "")
        if group_id:
            return f"group:{group_id}"
        user_id = str(event.get("user_id") or "")
        return f"private:{user_id}" if user_id else "private:unknown"

    async def _conversation_lock(self, key: str) -> asyncio.Lock:
        """取出（必要时创建）某个会话的处理锁，并回收长期不用的条目。"""
        async with self._process_locks_guard:
            lock = self._process_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._process_locks[key] = lock
            # 无人持有也无人等待的锁没有价值，顺手清掉，避免长跑进程里无界增长。
            if len(self._process_locks) > 4096:
                for stale_key, stale_lock in list(self._process_locks.items()):
                    if len(self._process_locks) <= 2048:
                        break
                    if stale_key != key and not stale_lock.locked():
                        self._process_locks.pop(stale_key, None)
            return lock

    async def _process_event(self, event: dict[str, Any]) -> bool:
        """统一消息处理：解析 → 引擎 → 发送。

        锁的粒度是**会话**：同一群的消息保持串行，不同群可以并行，这样一个群
        的长生成不再阻塞其余群。
        """
        lock = await self._conversation_lock(self._conversation_key(event))
        async with lock:
            return await self._process_event_impl(event)

    async def _process_event_impl(self, event: dict[str, Any]) -> bool:
        """实际的消息处理逻辑，受该会话的处理锁保护。"""
        parsed = await self.parse_event(event)
        if parsed is None:
            return True

        # 记录 Bot 自身的 platform_uid
        if self._engine is not None and parsed.self_id:
            self._engine._bot_platform_uids["qq_native_sirius_pulse"] = parsed.self_id
        if parsed.message_type == "group":
            await self._publish_group_metadata(parsed.group_id, parsed.self_id)

        speaker_name = parsed.card or parsed.nickname or f"qq_{parsed.user_id}"
        uid = f"qq_{parsed.user_id}"
        group_id = parsed.group_id
        dispatcher = self._get_dispatcher() if parsed.message_type == "group" else None
        qq_number = str(self.plugin_config.get("qq_number", "") or "").strip()
        if dispatcher is not None:
            dispatcher.set_account_id(parsed.self_id or qq_number)

        peer_ai_ids = self.plugin_config.get("peer_ai_ids", [])
        is_peer_ai = str(parsed.user_id) in [str(v) for v in peer_ai_ids] or bool(
            dispatcher is not None and dispatcher.is_peer_account(parsed.user_id)
        )
        mentions_current_bot = bool(
            (
                parsed.message_type == "group"
                and (
                    (parsed.self_id and parsed.self_id in parsed.at_user_ids)
                    or (qq_number and qq_number in parsed.at_user_ids)
                )
            )
            or (parsed.poke_target_id and parsed.poke_target_id in {parsed.self_id, qq_number})
        )

        participant = UnifiedUser(
            name=parsed.nickname or f"qq_{parsed.user_id}",
            user_id=uid,
            identities={"qq_native_sirius_pulse": parsed.user_id},
            metadata={
                "platform": "qq",
                "qq_uid": parsed.user_id,
                "is_developer": self._is_admin(parsed.user_id),
                "is_ai": is_peer_ai,
                "group_id": group_id if parsed.message_type == "group" else "",
                "scope": "private" if parsed.message_type == "private" else "group",
            },
        )

        message = Message(
            role="user",
            content=parsed.prompt,
            speaker=speaker_name,
            nickname=parsed.nickname,
            channel="qq_native_sirius_pulse",
            channel_user_id=parsed.user_id,
            group_id=group_id,
            message_id=parsed.message_id,
            multimodal_inputs=parsed.multimodal_inputs,
            adapter_type=self.adapter_type,
            adapter_route_id=self.adapter_route_id,
            sender_type="other_ai" if is_peer_ai else "human",
            mentions_current_bot=mentions_current_bot,
        )

        # A peer must see the actual platform message before it can obtain a
        # dispatcher lease and generate a reply. The event flag survives a
        # deferred retry, while Message prevents duplicate local history rows.
        if is_peer_ai and self._engine is not None:
            observe = getattr(self._engine, "observe_message", None)
            if callable(observe):
                if not bool(event.get("_sirius_peer_transcript_recorded")):
                    observe(message, [participant], group_id)
                    event["_sirius_peer_transcript_recorded"] = True
                message.transcript_recorded = True

        dispatch_lease_id = ""
        if dispatcher is not None:
            event_id = self._dispatch_event_id(parsed)
            target_account_ids = tuple(
                dict.fromkeys(
                    target_id
                    for target_id in [*parsed.at_user_ids, parsed.poke_target_id]
                    if target_id
                )
            )
            preview_dispatch = getattr(self._engine, "preview_dispatch", None)
            if callable(preview_dispatch):
                try:
                    candidate = preview_dispatch(message, [participant], group_id)
                except Exception:
                    LOG.warning("[群调度] 预判失败，按静默候选处理", exc_info=True)
                    candidate = {
                        "should_reply": False,
                        "score": 0.0,
                        "reason": "preview_failed",
                        "strategy": "silent",
                        "delay_seconds": 0.0,
                    }
            else:
                candidate = {
                    "should_reply": True,
                    "score": 1.0,
                    "reason": "legacy_engine",
                    "strategy": "immediate",
                    "delay_seconds": 0.0,
                }
            decision = await dispatcher.coordinate(
                event_id=event_id,
                group_id=group_id,
                base_score=float(candidate.get("score", 0.0)),
                should_reply=bool(candidate.get("should_reply", False)),
                response_strategy=str(candidate.get("strategy", "silent")),
                response_delay_seconds=float(candidate.get("delay_seconds", 0.0) or 0.0),
                sender_type=message.sender_type,
                sender_account_id=parsed.user_id,
                target_account_ids=target_account_ids,
                preferred_worker_id=(
                    dispatcher.worker_id
                    if candidate.get("is_mentioned") and not target_account_ids
                    else ""
                ),
                reason=str(candidate.get("reason", "")),
                message_text=parsed.prompt,
            )
            if not decision.granted:
                if decision.deferred:
                    self._schedule_dispatch_retry(event, event_id, group_id, dispatcher)
                    LOG.info(
                        "[群调度] defer group=%s worker=%s selected=%s reason=%s",
                        group_id,
                        dispatcher.worker_id,
                        decision.worker_id,
                        decision.reason,
                    )
                    return False
                observe = getattr(self._engine, "observe_message", None)
                if callable(observe) and not message.transcript_recorded:
                    observe(message, [participant], group_id)
                LOG.info(
                    "[群调度] observe group=%s worker=%s selected=%s reason=%s",
                    group_id,
                    dispatcher.worker_id,
                    decision.worker_id,
                    decision.reason,
                )
                return True
            dispatch_lease_id = decision.lease_id
            message.dispatch_coordinated = True
            message.dispatch_lease_id = dispatch_lease_id
            message.dispatch_response_strategy = decision.response_strategy
            message.dispatch_response_delay_seconds = decision.response_delay_seconds
            self._dispatch_leases[group_id] = dispatch_lease_id

        msg_preview = (parsed.prompt or "")[:200].replace("\n", " ")
        LOG.info(
            "[收到消息] %s | sender=%s(%s) uid=%s | content=%s",
            f"group={group_id}" if parsed.message_type == "group" else f"private={parsed.user_id}",
            parsed.nickname or "",
            parsed.card or "",
            parsed.user_id,
            msg_preview,
        )

        dispatch_sent = False
        dispatch_deferred = False
        response_parts: list[str] = []
        try:
            result = await self._engine.process_message(
                message=message,
                participants=[participant],
                group_id=group_id,
            )
            partial_sent_count = 0
            for partial in result.get("partial_replies", []):
                if partial:
                    if parsed.message_type == "group":
                        if partial_sent_count > 0:
                            await self._sleep_before_reply_part(partial)
                        sent = bool(await self._send_group_text(group_id, partial))
                    else:
                        if partial_sent_count > 0:
                            await self._sleep_before_reply_part(partial)
                        sent = bool(await self._send_private_text(parsed.user_id, partial))
                    if sent:
                        response_parts.append(str(partial))
                    dispatch_sent = sent or dispatch_sent
                    partial_sent_count += 1

            reply = result.get("reply")
            message_group = result.get("message_group")
            if message_group is not None:
                # 多模态消息：通过 MessageGroup 发送（图片/语音/文件等）
                if parsed.message_type == "group":
                    await self.send_group_message(group_id, message_group)
                else:
                    await self.send_private_message(parsed.user_id, message_group)
                dispatch_sent = True
            elif reply:
                clean_reply = reply.strip()
                if clean_reply:
                    if parsed.message_type == "group":
                        if partial_sent_count > 0:
                            await self._sleep_before_reply_part(clean_reply)
                        if clean_reply:
                            sent = bool(await self._send_group_text(group_id, clean_reply))
                    else:
                        if partial_sent_count > 0:
                            await self._sleep_before_reply_part(clean_reply)
                        if clean_reply:
                            sent = bool(await self._send_private_text(parsed.user_id, clean_reply))
                    if sent:
                        response_parts.append(clean_reply)
                    dispatch_sent = sent or dispatch_sent
            sticker_names = result.get("sticker_names", [])
            poke_user_ids = result.get("poke_user_ids", [])
            await self._send_stickers_after_reply(group_id, sticker_names)
            await self._send_pokes_after_reply(group_id, poke_user_ids)
            dispatch_sent = bool(sticker_names or poke_user_ids) or dispatch_sent
            if (
                dispatch_lease_id
                and not dispatch_sent
                and result.get("strategy")
                in {
                    "immediate",
                    "delayed",
                }
            ):
                # The existing queue will deliver the generated reply later;
                # keep the group lease until its delivery event completes.
                dispatch_deferred = True
        except asyncio.CancelledError:
            raise
        except RuntimeError as exc:
            LOG.exception("引擎处理错误 (%s/%s): %s", group_id, parsed.user_id, exc)
        except Exception as exc:
            LOG.exception("消息处理异常 (%s/%s): %s", group_id, parsed.user_id, exc)
        finally:
            if dispatch_lease_id and not dispatch_deferred and dispatcher is not None:
                dispatcher.finish(
                    dispatch_lease_id,
                    sent=dispatch_sent,
                    response_text="\n".join(response_parts),
                )
                if self._dispatch_leases.get(group_id) == dispatch_lease_id:
                    self._dispatch_leases.pop(group_id, None)
        return True

    def _schedule_dispatch_retry(
        self,
        event: dict[str, Any],
        event_id: str,
        group_id: str,
        dispatcher: GroupDispatcher,
    ) -> None:
        task = self._dispatch_retry_tasks.get(event_id)
        if task is not None and not task.done():
            return
        engine = self._engine
        if engine is None:
            return
        wait_seconds = min(
            max(30.0, dispatcher.lease_seconds + dispatcher.peer_cooldown_seconds),
            180.0,
        )
        self._dispatch_retry_tasks[event_id] = asyncio.create_task(
            self._retry_dispatch_event(
                event,
                event_id,
                wait_seconds,
                engine=engine,
                generation=self._engine_generation,
            )
        )

    async def _retry_dispatch_event(
        self,
        event: dict[str, Any],
        event_id: str,
        wait_seconds: float,
        *,
        engine: Any,
        generation: int,
    ) -> None:
        """Retry only while the adapter still owns the original engine."""
        deadline = time.monotonic() + wait_seconds
        try:
            while (
                self._running
                and not self._detaching_engine
                and self._engine is engine
                and generation == self._engine_generation
                and not getattr(engine, "_runtime_retiring", False)
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.5)
                if (
                    self._detaching_engine
                    or self._engine is not engine
                    or generation != self._engine_generation
                    or getattr(engine, "_runtime_retiring", False)
                ):
                    return
                if await self._process_event(event):
                    return
            if self._engine is engine and generation == self._engine_generation:
                LOG.info("[群调度] retry expired event=%s", event_id)
        except asyncio.CancelledError:
            raise
        finally:
            if self._dispatch_retry_tasks.get(event_id) is asyncio.current_task():
                self._dispatch_retry_tasks.pop(event_id, None)

    # ─── 事件总线监听 ────────────────────────────────────

    def _release_dispatch_leases(self) -> None:
        """Release deferred delivery leases that will not survive a rebind."""
        dispatcher = self._dispatcher
        if dispatcher is None:
            return
        for lease_id in set(self._dispatch_leases.values()):
            if not lease_id:
                continue
            try:
                dispatcher.finish(lease_id, sent=False)
            except Exception:
                LOG.debug("释放重绑定前的群调度 lease 失败", exc_info=True)

    async def _cancel_dispatch_retry_tasks(self) -> None:
        """Cancel deferred inbound-event retries before engine ownership moves."""
        tasks = list(self._dispatch_retry_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._dispatch_retry_tasks.clear()

    async def _run_raw_event_handler(
        self,
        handler: EventHandler,
        data: dict[str, Any],
        generation: int,
    ) -> None:
        """Run an inbound handler only while its engine generation is current."""
        if self._detaching_engine or generation != self._engine_generation:
            return
        await handler(data)

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        """Retain a fire-and-forget task until it is observed and complete."""
        self._event_handler_tasks.add(task)

        def _discard(done: asyncio.Task[Any]) -> None:
            self._event_handler_tasks.discard(done)
            if done.cancelled():
                return
            try:
                exc = done.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                LOG.warning("事件处理任务异常: %s", exc)

        task.add_done_callback(_discard)

    def _track_event_handler(self, event: SessionEvent, engine: Any) -> None:
        """Run one bus event without losing ownership during shutdown/rebind."""
        self._track_task(asyncio.create_task(self._handle_event(event, engine=engine)))

    async def _cancel_event_handler_tasks(self) -> None:
        """Cancel and drain every tracked handler before ownership changes."""
        while self._event_handler_tasks:
            tasks = list(self._event_handler_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for task in tasks:
                self._event_handler_tasks.discard(task)

    async def _event_bus_listener(self) -> None:
        engine = self._engine
        while (
            self._running
            and not self._detaching_engine
            and engine is not None
            and not getattr(engine, "_runtime_retiring", False)
        ):
            try:
                async for event in engine.event_bus.subscribe():
                    if (
                        not self._running
                        or self._detaching_engine
                        or self._engine is not engine
                        or getattr(engine, "_runtime_retiring", False)
                    ):
                        break
                    self._track_event_handler(event, engine)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                LOG.warning("事件总线监听异常: %s", exc)
                await asyncio.sleep(1)

    async def _keep_dispatch_lease_alive(
        self,
        dispatcher: GroupDispatcher,
        lease_id: str,
    ) -> None:
        """Renew a delivery lease while OneBot I/O is still pending."""
        interval = max(0.25, min(30.0, dispatcher.lease_seconds / 3.0))
        while True:
            await asyncio.sleep(interval)
            if not dispatcher.renew(lease_id):
                return

    async def _deliver_proactive_payload(
        self,
        *,
        group_id: str,
        reply: str,
        reply_refs: Any,
        image_path: str,
        sticker_names: Any,
        poke_user_ids: Any,
    ) -> bool:
        """Deliver required text first; optional media never masks text failure."""
        private = group_id.startswith("private_")
        target_id = group_id.removeprefix("private_").removeprefix("qq_") if private else group_id
        atomic_token = _ATOMIC_PROACTIVE_SEND.set(True)
        try:
            if reply:
                if private:
                    text_sent = await self._send_private_text(target_id, reply, reply_refs)
                else:
                    text_sent = await self._send_group_text(group_id, reply, reply_refs)
                if not text_sent:
                    return False
                # Text is authoritative for plugin notifications.  Images and
                # interaction effects are optional enhancements after it succeeds.
                if image_path:
                    if private:
                        await self._send_private_image(target_id, image_path)
                    else:
                        await self._send_group_image(group_id, image_path)
                effect_group_id = f"private_{target_id}" if private else group_id
                await self._send_stickers_after_reply(effect_group_id, sticker_names)
                await self._send_pokes_after_reply(effect_group_id, poke_user_ids)
                return True

            sent = False
            if image_path:
                if private:
                    sent = await self._send_private_image(target_id, image_path)
                else:
                    sent = await self._send_group_image(group_id, image_path)
            effect_group_id = f"private_{target_id}" if private else group_id
            sticker_sent = await self._send_stickers_after_reply(effect_group_id, sticker_names)
            poke_sent = await self._send_pokes_after_reply(effect_group_id, poke_user_ids)
            return bool(sent or sticker_sent or poke_sent)
        finally:
            _ATOMIC_PROACTIVE_SEND.reset(atomic_token)

    def _prune_local_proactive_events(self) -> None:
        """Bound fallback reminder receipts and per-event asyncio locks."""
        cutoff = time.monotonic() - 30 * 86400.0
        keep = sorted(
            (
                (key, seen_at)
                for key, seen_at in self._proactive_event_results.items()
                if seen_at >= cutoff
            ),
            key=lambda item: item[1],
            reverse=True,
        )[:4096]
        self._proactive_event_results = dict(keep)
        if len(self._proactive_event_locks) <= 8192:
            return
        retained_results = set(self._proactive_event_results)
        for key, lock in list(self._proactive_event_locks.items()):
            if len(self._proactive_event_locks) <= 8192:
                break
            waiters = getattr(lock, "_waiters", None)
            has_waiters = any(not waiter.done() for waiter in (waiters or ()))
            if key not in retained_results and not lock.locked() and not has_waiters:
                self._proactive_event_locks.pop(key, None)

    def _remember_local_proactive_event(self, event_key: str) -> None:
        """Bound same-process fallback reminder deduplication to thirty days."""
        self._proactive_event_results[event_key] = time.monotonic()
        self._prune_local_proactive_events()

    def _ack_proactive_delivery(self, event: SessionEvent, accepted: bool) -> None:
        """Resolve an in-memory receipt attached by the proactive dispatcher."""
        receipt = event.data.get("_delivery_ack")
        if not isinstance(receipt, dict):
            return
        expected = {
            str(value).strip() for value in receipt.get("expected", []) if str(value).strip()
        }
        if expected and self.adapter_type not in expected:
            return
        results = receipt.setdefault("results", {})
        type_results = results.setdefault(self.adapter_type, {})
        if not isinstance(type_results, dict):
            return
        route_key = str(getattr(self, "adapter_route_id", "") or f"instance:{id(self)}")
        type_results.setdefault(route_key, bool(accepted))
        future = receipt.get("future")
        if not isinstance(future, asyncio.Future) or future.done():
            return
        if not expected:
            # Legacy broadcasts have no complete candidate set.  One confirmed
            # platform send is enough; all-negative cases use the bounded
            # dispatcher timeout.
            if any(type_results.values()):
                future.set_result(True)
            return

        succeeded = {
            adapter
            for adapter in expected
            if isinstance(results.get(adapter), dict)
            and any(bool(value) for value in results[adapter].values())
        }
        if expected.issubset(succeeded):
            future.set_result(True)
            return
        raw_counts = receipt.get("expected_counts", {})
        expected_counts = raw_counts if isinstance(raw_counts, dict) else {}
        for adapter in expected - succeeded:
            adapter_results = results.get(adapter, {})
            required = max(1, int(expected_counts.get(adapter, 1) or 1))
            if isinstance(adapter_results, dict) and len(adapter_results) >= required:
                future.set_result(False)
                return

    async def _handle_event(self, event: SessionEvent, *, engine: Any | None = None) -> None:
        engine = self._engine if engine is None else engine
        if (
            engine is None
            or self._engine is not engine
            or getattr(engine, "_runtime_retiring", False)
        ):
            return
        try:
            if event.type == SessionEventType.DELAYED_RESPONSE_TRIGGERED:
                source_adapter = str(event.data.get("adapter_type", "") or "").strip()
                source_route = str(event.data.get("adapter_route_id", "") or "").strip()
                # Queue consumption is destructive.  Prefer the stable source
                # instance/account route; coarse type routing is retained only
                # for legacy items that predate adapter_route_id.
                if source_route:
                    if source_route != self.adapter_route_id:
                        return
                elif source_adapter and source_adapter != self.adapter_type:
                    return
                gid = str(event.data.get("group_id", ""))
                if gid in self._dispatch_delivery_active:
                    item_id = str(event.data.get("item_id", "") or "")
                    emitted = getattr(engine, "_delayed_event_emitted", None)
                    if item_id and isinstance(emitted, dict):
                        emitted.setdefault(gid, set()).discard(item_id)
                    return
                self._dispatch_delivery_active.add(gid)
                try:
                    dispatcher = self._get_dispatcher() if not gid.startswith("private_") else None
                    dispatch_lease_id = self._dispatch_leases.get(gid, "")
                    if dispatcher is not None and dispatch_lease_id != dispatcher.active_lease(gid):
                        dispatch_lease_id = ""
                        self._dispatch_leases.pop(gid, None)
                    if dispatcher is not None and not dispatch_lease_id:
                        item_id = str(
                            event.data.get("item_id", "") or event.data.get("agent_turn_id", "")
                        )
                        decision = dispatcher.admit(
                            event_id=f"delayed:{gid}:{item_id or 'unknown'}",
                            group_id=gid,
                            sender_type="system",
                            preferred_worker_id=dispatcher.worker_id,
                        )
                        if not decision.granted:
                            cancel_item = getattr(engine.delayed_queue, "cancel_item", None)
                            if callable(cancel_item) and item_id:
                                cancel_item(item_id)
                            self._dispatch_delivery_active.discard(gid)
                            return
                        dispatch_lease_id = decision.lease_id
                        self._dispatch_leases[gid] = dispatch_lease_id
                except Exception:
                    self._dispatch_delivery_active.discard(gid)
                    raise
                partial_sent_count = 0
                dispatch_sent = False
                response_parts: list[str] = []

                async def _send_partial(text: str) -> None:
                    nonlocal dispatch_sent, partial_sent_count
                    if partial_sent_count > 0:
                        await self._sleep_before_reply_part(text)
                    if gid.startswith("private_"):
                        uid = gid.replace("private_", "").replace("qq_", "")
                        sent = await self._send_private_text(uid, text)
                    elif gid in self._get_allowed_group_ids():
                        sent = await self._send_group_text(gid, text)
                    else:
                        raise RuntimeError(f"Partial reply target is not allowed: {gid}")
                    if not sent:
                        raise RuntimeError(f"Failed to send partial reply: {gid}")
                    dispatch_sent = True
                    response_parts.append(str(text))
                    partial_sent_count += 1

                try:
                    try:
                        results = await engine.tick_delayed_queue(
                            gid,
                            on_partial_reply=_send_partial,
                            adapter_type=self.adapter_type,
                            # Empty string explicitly selects old, unrouted
                            # queue items; None would mean every same-type item.
                            adapter_route_id=source_route,
                        )
                    except Exception as exc:
                        LOG.warning("Delayed queue tick failed (%s): %s", gid, exc)
                        results = []
                    for result in results:
                        reply = result.get("reply", "")
                        reply_refs = result.get("reply_references", [])
                        sticker_names = result.get("sticker_names", [])
                        poke_user_ids = result.get("poke_user_ids", [])
                        if gid.startswith("private_"):
                            uid = gid.replace("private_", "").replace("qq_", "")
                            if reply:
                                if partial_sent_count > 0:
                                    await self._sleep_before_reply_part(reply)
                                if reply:
                                    sent = await self._send_private_text(uid, reply, reply_refs)
                                    dispatch_sent = bool(sent) or dispatch_sent
                                    if sent:
                                        response_parts.append(str(reply))
                            sticker_sent = await self._send_stickers_after_reply(gid, sticker_names)
                            poke_sent = await self._send_pokes_after_reply(gid, poke_user_ids)
                            dispatch_sent = sticker_sent or poke_sent or dispatch_sent
                        elif gid in self._get_allowed_group_ids():
                            if reply:
                                if partial_sent_count > 0:
                                    await self._sleep_before_reply_part(reply)
                                if reply:
                                    sent = await self._send_group_text(gid, reply, reply_refs)
                                    dispatch_sent = bool(sent) or dispatch_sent
                                    if sent:
                                        response_parts.append(str(reply))
                            sticker_sent = await self._send_stickers_after_reply(gid, sticker_names)
                            poke_sent = await self._send_pokes_after_reply(gid, poke_user_ids)
                            dispatch_sent = sticker_sent or poke_sent or dispatch_sent
                finally:
                    # Clear the guard before lease finalization; even a
                    # dispatcher storage error must not wedge this group.
                    self._dispatch_delivery_active.discard(gid)
                    if dispatch_lease_id and dispatcher is not None:
                        dispatcher.finish(
                            dispatch_lease_id,
                            sent=dispatch_sent,
                            response_text="\n".join(response_parts),
                        )
                        if self._dispatch_leases.get(gid) == dispatch_lease_id:
                            self._dispatch_leases.pop(gid, None)
            elif event.type == SessionEventType.REMINDER_TRIGGERED:
                gid = str(event.data.get("group_id", ""))
                reply = event.data.get("reply", "")
                adapter_type = str(event.data.get("adapter_type", "") or "").strip()
                image_path = str(event.data.get("image_path", "")).strip()
                reply_refs = event.data.get("reply_references", [])
                sticker_names = event.data.get("sticker_names", [])
                poke_user_ids = event.data.get("poke_user_ids", [])
                target_types = event.data.get("adapter_types", [])
                adapter_route_id = str(event.data.get("adapter_route_id", "") or "").strip()
                if adapter_route_id and adapter_route_id != self.adapter_route_id:
                    return
                if isinstance(target_types, str):
                    target_types = [target_types]
                if not isinstance(target_types, (list, tuple, set)):
                    target_types = []
                normalized_target_types = {
                    str(value).strip() for value in target_types if str(value).strip()
                }
                # An explicit adapter type is authoritative.  The optional
                # adapter_types list is only an allow-set for blank-target
                # events; a list cannot override an explicit other adapter.
                if adapter_type:
                    adapter_matches = adapter_type == self.adapter_type
                elif normalized_target_types:
                    adapter_matches = self.adapter_type in normalized_target_types
                else:
                    # Legacy blank events without routing metadata are a
                    # broadcast to subscribed adapters.
                    adapter_matches = True
                destination_allowed = (
                    self._is_private_user_allowed(gid.removeprefix("private_").removeprefix("qq_"))
                    if gid.startswith("private_")
                    else self._is_group_allowed(gid)
                )
                if not adapter_matches:
                    return
                if not (reply or image_path or sticker_names or poke_user_ids):
                    self._ack_proactive_delivery(event, False)
                    return
                if not destination_allowed:
                    self._ack_proactive_delivery(event, False)
                    return
                if (
                    (reply or image_path or sticker_names or poke_user_ids)
                    and adapter_matches
                    and destination_allowed
                ):
                    reminder_id = str(event.data.get("reminder_id", "") or event.data.get("id", ""))
                    if not reminder_id:
                        fallback_id = json.dumps(
                            {
                                "group_id": gid,
                                "reply": reply,
                                "image_path": image_path,
                                "reply_references": reply_refs,
                                "sticker_names": sticker_names,
                                "poke_user_ids": poke_user_ids,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        reminder_id = hashlib.sha256(fallback_id.encode("utf-8")).hexdigest()[:24]
                    # Canonicalize equivalent private IDs before deriving a
                    # durable stable-event namespace.  Helpers selects one
                    # concrete route when multiple same-type adapters are
                    # eligible, so the namespace must represent the logical
                    # destination rather than the local adapter instance.
                    private_target = (
                        gid.removeprefix("private_").removeprefix("qq_")
                        if gid.startswith("private_")
                        else ""
                    )
                    dispatch_group_id = f"private:{private_target}" if private_target else gid
                    dispatch_event_id = f"reminder:{dispatch_group_id}:{reminder_id}"
                    local_event_key = f"{self.adapter_route_id}:{dispatch_event_id}"
                    self._prune_local_proactive_events()
                    event_lock = self._proactive_event_locks.setdefault(
                        local_event_key, asyncio.Lock()
                    )
                    async with event_lock:
                        if local_event_key in self._proactive_event_results:
                            self._ack_proactive_delivery(event, True)
                            return
                        dispatcher = self._get_dispatcher()
                        dispatch_lease_id = ""
                        heartbeat: asyncio.Task[Any] | None = None
                        accepted = False
                        delivery_started = False
                        delivery_uncertain = False
                        if dispatcher is not None:
                            decision = dispatcher.admit(
                                event_id=dispatch_event_id,
                                group_id=dispatch_group_id,
                                sender_type="system",
                                preferred_worker_id=dispatcher.worker_id,
                            )
                            if not decision.granted:
                                terminal = decision.reason in {
                                    "event_sent",
                                    "event_uncertain",
                                }
                                self._ack_proactive_delivery(event, terminal)
                                if terminal:
                                    self._remember_local_proactive_event(local_event_key)
                                return
                            dispatch_lease_id = decision.lease_id

                        def start_delivery() -> bool:
                            """Fence the event immediately before irreversible OneBot I/O."""
                            nonlocal delivery_started, heartbeat
                            if delivery_started:
                                return True
                            if dispatcher is not None and not dispatcher.begin_delivery(
                                dispatch_lease_id
                            ):
                                return False
                            delivery_started = True
                            if dispatcher is not None:
                                heartbeat = asyncio.create_task(
                                    self._keep_dispatch_lease_alive(
                                        dispatcher,
                                        dispatch_lease_id,
                                    )
                                )
                            return True

                        delivery_token = _PROACTIVE_DELIVERY_START.set(start_delivery)
                        try:
                            accepted = await self._deliver_proactive_payload(
                                group_id=gid,
                                reply=reply,
                                reply_refs=reply_refs,
                                image_path=image_path,
                                sticker_names=sticker_names,
                                poke_user_ids=poke_user_ids,
                            )
                        except DeliveryUncertainError as exc:
                            # A request crossed the irreversible-I/O fence but
                            # its platform result was not observed.  Replaying a
                            # stable ID could duplicate a visible side effect.
                            delivery_uncertain = True
                            LOG.warning("主动消息平台确认丢失，禁止重放: %s", exc)
                        except asyncio.CancelledError:
                            # Lock/rate-limit waits happen before start_delivery
                            # and are safe to retry.  Cancellation after the fence
                            # is terminal because platform I/O may have started.
                            delivery_uncertain = delivery_started
                            raise
                        finally:
                            _PROACTIVE_DELIVERY_START.reset(delivery_token)
                            if heartbeat is not None:
                                heartbeat.cancel()
                                await asyncio.gather(heartbeat, return_exceptions=True)
                            terminal = accepted or delivery_uncertain
                            if terminal:
                                # Keep a same-process guard even if durable
                                # finalization itself encounters storage trouble.
                                self._remember_local_proactive_event(local_event_key)
                            if dispatch_lease_id and dispatcher is not None:
                                dispatcher.finish(
                                    dispatch_lease_id,
                                    sent=accepted,
                                    uncertain=delivery_uncertain,
                                )
                            # Do not describe a lost platform ACK as a confirmed
                            # current attempt.  The next stable-ID retry observes
                            # the terminal uncertain marker without resending.
                            self._ack_proactive_delivery(event, accepted)
        except Exception as exc:
            if event.type == SessionEventType.REMINDER_TRIGGERED:
                self._ack_proactive_delivery(event, False)
            LOG.warning("事件处理异常: %s", exc, exc_info=True)

    # ─── 消息发送（引擎回调） ─────────────────────────────

    @staticmethod
    def _parse_ref_markers(text: str) -> tuple[str, list[dict[str, str]]]:
        """解析文本中的 [REF:...] 引用标记。

        Returns:
            (清理后的文本, 引用列表)
        """
        import re

        ref_pattern = re.compile(
            r'\[REF:index=(\d+)\s+msg_id="([^"]*)"\s+speaker="([^"]*)"\s+content="([^"]*)"\]'
        )
        refs: list[dict[str, str]] = []
        clean_text = text

        for match in ref_pattern.finditer(text):
            refs.append(
                {
                    "index": match.group(1),
                    "msg_id": match.group(2),
                    "speaker": match.group(3),
                    "content": match.group(4),
                }
            )
            clean_text = clean_text.replace(match.group(0), "", 1)

        return clean_text.strip(), refs

    async def _send_group_text(
        self, group_id: str, text: str, reply_refs: list[dict[str, str]] | None = None
    ) -> bool:
        key = str(group_id)
        async with self._get_reply_lock(key):
            if _ATOMIC_PROACTIVE_SEND.get():
                return await self._send_group_text_single_locked(
                    group_id,
                    text,
                    reply_refs,
                )
            # 最终兜底：按换行符拆分为多条消息，仅首条携带引用
            lines = [line for line in text.splitlines() if line.strip()]
            if len(lines) > 1:
                first = True
                for line in lines:
                    if not first:
                        await self._sleep_before_reply_part(line)
                    refs = reply_refs if first else None
                    ok = await self._send_group_text_single_locked(group_id, line, refs)
                    if not ok:
                        return False
                    first = False
                return True
            return await self._send_group_text_single_locked(group_id, text, reply_refs)

    async def _send_group_text_single(
        self, group_id: str, text: str, reply_refs: list[dict[str, str]] | None = None
    ) -> bool:
        key = str(group_id)
        async with self._get_reply_lock(key):
            return await self._send_group_text_single_locked(group_id, text, reply_refs)

    async def _send_group_text_single_locked(
        self, group_id: str, text: str, reply_refs: list[dict[str, str]] | None = None
    ) -> bool:
        text = text.rstrip("\n")
        try:
            # 如果有引用且有有效的 msg_id，使用 reply segment
            if reply_refs and reply_refs[0].get("msg_id"):
                msg_id = reply_refs[0]["msg_id"]
                segments: list[dict[str, Any]] = [
                    {"type": "reply", "data": {"id": msg_id}},
                ]
                segments.extend(self._group_text_to_segments(group_id, text))
                await self.send_group_msg(group_id, segments)
                LOG.info(
                    "回复群 %s (引用 msg_id=%s): %s",
                    group_id,
                    msg_id,
                    text[:120],
                )
            elif reply_refs:
                # 有引用但没有 msg_id，使用文本格式
                ref_lines = []
                for ref in reply_refs:
                    speaker = ref.get("speaker", "未知")
                    content = ref.get("content", "")
                    if len(content) > 80:
                        content = content[:80] + "..."
                    ref_lines.append(f"> {speaker}: {content}")
                formatted_reply = "\n".join(ref_lines) + "\n" + text
                await self.send_group_msg(
                    group_id, self._group_text_to_segments(group_id, formatted_reply)
                )
                LOG.info("回复群 %s (引用但无msg_id): %s", group_id, formatted_reply[:120])
            else:
                await self.send_group_msg(group_id, self._group_text_to_segments(group_id, text))
                LOG.info("回复群 %s: %s", group_id, text[:120])
            return True
        except _OneBotDeliveryUncertain:
            raise
        except Exception as exc:
            LOG.warning("发送群消息失败: %s", exc)
            return False

    def _group_text_to_segments(self, group_id: str, text: str) -> list[dict[str, Any]]:
        text = self._convert_fake_at_mentions(group_id, text)
        member_ids = self._valid_group_member_ids(group_id)
        message_group = parse_qq_at_mentions(text, valid_user_ids=member_ids)
        if message_group is None:
            return [{"type": "text", "data": {"text": text}}]
        return self._message_group_to_onebot(message_group)

    def _convert_fake_at_mentions(self, group_id: str, text: str) -> str:
        """将模型输出的 @昵称/@别称/@QQ号 转换为 [AT:QQ号] 格式。"""
        import re

        cached = self._group_member_cache.get(str(group_id))
        if not cached:
            return text
        _, members = cached
        if not members:
            return text

        # 构建 名称 → user_id 的映射（群名片、昵称、别称）
        name_to_id: dict[str, str] = {}
        # 合法 QQ 号集合（用于校验纯数字 @xxx）
        valid_ids: set[str] = set()
        for member in members:
            uid = str(member.get("user_id", "") or "").strip()
            if not uid:
                continue
            valid_ids.add(uid)
            for field_key in ("card", "nickname", "alias"):
                val = str(member.get(field_key, "") or "").strip()
                if val:
                    name_to_id[val] = uid

        if not name_to_id and not valid_ids:
            return text

        # 按名字长度降序排列，避免短名误匹配长名
        sorted_names = sorted(name_to_id.keys(), key=len, reverse=True)
        # 匹配 @xxx：排除 @{...} 格式，纯数字分支放前面优先匹配
        pattern = re.compile(r"@(?!\{)(\d{5,12}|[一-鿿\w][一-鿿\w ]{0,20})")

        def _replace(m: re.Match) -> str:
            raw = m.group(1).strip()
            # 纯数字：校验是否为合法群成员 QQ 号
            if raw.isdigit():
                return f"[AT:{raw}]" if raw in valid_ids else m.group(0)
            # 文字：匹配昵称/群名片/别称
            for known in sorted_names:
                if raw == known:
                    return f"[AT:{name_to_id[known]}]"
            return m.group(0)

        return pattern.sub(_replace, text)

    def _valid_group_member_ids(self, group_id: str) -> set[str] | None:
        cached = self._group_member_cache.get(str(group_id))
        if not cached:
            return None
        updated_at, members = cached
        if time.monotonic() - updated_at > self._group_metadata_ttl:
            return None
        ids = {str(member.get("user_id", "") or "").strip() for member in members}
        return {user_id for user_id in ids if user_id}

    async def _send_private_text(
        self, user_id: str, text: str, reply_refs: list[dict[str, str]] | None = None
    ) -> bool:
        key = f"private_{user_id}"
        async with self._get_reply_lock(key):
            if _ATOMIC_PROACTIVE_SEND.get():
                return await self._send_private_text_single_locked(user_id, text)
            # 最终兜底：按换行符拆分为多条消息
            lines = [line for line in text.splitlines() if line.strip()]
            if len(lines) > 1:
                first = True
                for line in lines:
                    if not first:
                        await self._sleep_before_reply_part(line)
                    ok = await self._send_private_text_single_locked(user_id, line)
                    if not ok:
                        return False
                    first = False
                return True
            return await self._send_private_text_single_locked(user_id, text)

    async def _send_private_text_single(self, user_id: str, text: str) -> bool:
        key = f"private_{user_id}"
        async with self._get_reply_lock(key):
            return await self._send_private_text_single_locked(user_id, text)

    async def _send_private_text_single_locked(self, user_id: str, text: str) -> bool:
        text = text.rstrip("\n")
        try:
            await self.send_private_msg(user_id, text)
            LOG.info("回复私聊 %s: %s", user_id, text[:120])
            return True
        except _OneBotDeliveryUncertain:
            raise
        except Exception as exc:
            LOG.warning("发送私聊消息失败: %s", exc)
            return False

    async def _send_stickers_after_reply(self, group_id: str, names: Any) -> bool:
        if not names or self._engine is None:
            return False
        if isinstance(names, str):
            sticker_names = [names.strip()] if names.strip() else []
        else:
            sticker_names = [str(name).strip() for name in names if str(name).strip()]
        if not sticker_names:
            return False
        try:
            result = await self._engine._send_stickers_by_names(
                group_id,
                sticker_names,
                adapter=self,
            )
            if not isinstance(result, dict) or result.get("success") is not True:
                LOG.warning(
                    "发送回复后的表情包失败: group=%s names=%s result=%s",
                    group_id,
                    sticker_names,
                    result,
                )
                return False
            return True
        except DeliveryUncertainError:
            raise
        except Exception as exc:
            LOG.warning("发送回复后的表情包失败: %s", exc)
            return False

    async def _send_pokes_after_reply(self, group_id: str, user_ids: Any) -> bool:
        if not user_ids or self._engine is None or group_id.startswith("private_"):
            return False
        if isinstance(user_ids, str):
            poke_user_ids = [user_ids.strip()] if user_ids.strip() else []
        else:
            poke_user_ids = [str(user_id).strip() for user_id in user_ids if str(user_id).strip()]
        sent_any = False
        for user_id in dict.fromkeys(poke_user_ids):
            try:
                result = await self.send_poke(user_id, group_id)
                if not isinstance(result, dict) or result.get("status") not in {
                    "ok",
                    "async",
                }:
                    LOG.warning(
                        "发送回复后的戳一戳失败: group=%s user=%s result=%s",
                        group_id,
                        user_id,
                        result,
                    )
                    continue
                sent_any = True
            except DeliveryUncertainError:
                raise
            except Exception as exc:
                LOG.warning("发送回复后的戳一戳失败: group=%s user=%s error=%s", group_id, user_id, exc)
        return sent_any

    async def _send_group_image(self, group_id: str, image_path: str) -> bool:
        """发送群聊图片并返回平台是否确认接受。"""
        image_reference = to_image_reference(image_path)
        segment: list[dict[str, Any]] = [{"type": "image", "data": {"file": image_reference}}]
        async with self._get_reply_lock(group_id):
            try:
                await self.send_group_msg(group_id, segment)
                LOG.info("回复群 %s 图片: %s", group_id, image_path)
                return True
            except _OneBotDeliveryUncertain:
                raise
            except Exception as exc:
                LOG.warning("发送群图片失败: %s", exc)
                return False

    async def _send_private_image(self, user_id: str, image_path: str) -> bool:
        """发送私聊图片并返回平台是否确认接受。"""
        image_reference = to_image_reference(image_path)
        segment: list[dict[str, Any]] = [{"type": "image", "data": {"file": image_reference}}]
        async with self._get_reply_lock(user_id):
            try:
                await self.send_private_msg(user_id, segment)
                LOG.info("回复私聊 %s 图片: %s", user_id, image_path)
                return True
            except _OneBotDeliveryUncertain:
                raise
            except Exception as exc:
                LOG.warning("发送私聊图片失败: %s", exc)
                return False

    def _get_reply_lock(self, key: str) -> asyncio.Lock:
        lock = self._reply_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._reply_locks[key] = lock
        return lock

    async def _sleep_before_reply_part(self, line: str) -> None:
        delay = self._reply_part_delay_seconds(line)
        if delay > 0:
            await asyncio.sleep(delay)

    def _reply_part_delay_seconds(self, line: str) -> float:
        if self.plugin_config.get("human_reply_delay_enabled", True) is False:
            return 0.0
        chars = len((line or "").strip())
        if chars <= 0:
            return 0.0
        chars_per_second = max(
            1.0,
            self._config_float("human_reply_chars_per_second", 5.0),
        )
        min_delay = max(0.0, self._config_float("human_reply_min_delay_seconds", 1.5))
        max_delay = max(min_delay, self._config_float("human_reply_max_delay_seconds", 7.0))
        return min(max(chars / chars_per_second, min_delay), max_delay)

    def _config_float(self, key: str, default: float) -> float:
        try:
            return float(self.plugin_config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _engine_ready(self) -> bool:
        """检查引擎是否已就绪。"""
        engine = self._engine
        if engine is None or bool(getattr(engine, "_runtime_retiring", False)):
            return False
        return getattr(engine, "is_ready", lambda: True)()

    def _log_not_ready(self) -> None:
        loop = asyncio.get_event_loop()
        now = loop.time()
        if now - self._last_not_ready_log >= self._NOT_READY_LOG_INTERVAL:
            self._last_not_ready_log = now
            LOG.warning(
                "引擎未就绪，跳过消息（每 %.0f 秒提示一次）",
                self._NOT_READY_LOG_INTERVAL,
            )

    # ─── 事件等待（供 setup wizard 使用）───────────────────

    async def wait_event(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=remaining)
                if predicate(event):
                    return event
            except asyncio.TimeoutError:
                raise
