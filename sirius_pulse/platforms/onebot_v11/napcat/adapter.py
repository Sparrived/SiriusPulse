"""原生 NapCat OneBot v11 Adapter — 完整的平台集成。

职责：
    - 正向 WebSocket 连接、OneBot v11 API 调用
    - OneBot 事件 → ParsedEvent 解析（表情/图片/@ 转换）
    - 引擎事件总线监听（proactive/delayed/reminder 投递）
    - 消息回复发送（带锁）

继承 BaseAdapter，实现平台无关的消息发送接口。
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

import websockets
import websockets.exceptions

try:
    from websockets.asyncio.client import ClientConnection as WebSocketClientProtocol
except ImportError:
    from websockets import WebSocketClientProtocol  # type: ignore[assignment, attr-defined, no-redef]

from sirius_pulse.adapters.base import BaseAdapter
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
from sirius_pulse.core.qq_mentions import parse_qq_at_mentions
from sirius_pulse.models.models import Message, UnifiedUser

LOG = logging.getLogger("sirius.platforms.napcat")

EventHandler = Callable[[dict[str, Any]], Any]


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


class NapCatAdapter(BaseAdapter):
    """NapCat OneBot v11 正向 WebSocket 客户端 + 平台集成。

    同时承担原 NapCatBridge 的职责：事件→引擎→发送。
    """

    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 30.0
    _MAX_RECONNECT_ATTEMPTS = 5

    adapter_type = "napcat"
    _NOT_READY_LOG_INTERVAL = 30.0

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
        self._image_cache_dir = _wp / "image_cache"
        self._sticker_cache_dir = _wp / "sticker_cache"

        # 引擎集成（原 Bridge 字段）
        self.plugin_config = dict(config or {})
        self._enabled = True
        self._engine: Any = None
        self._last_not_ready_log: float = 0.0
        self._reply_locks: dict[str, asyncio.Lock] = {}
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._group_member_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._bot_admin_cache: dict[str, tuple[float, bool]] = {}
        self._group_metadata_ttl = 300.0

        # API 限流：每群/私聊独立，每秒最多 1 条消息
        self._last_api_call_at: dict[str, float] = {}
        self._api_send_lock = asyncio.Lock()
        self._event_bus_task: asyncio.Task | None = None

        # 消息处理锁：防止并发进入引擎 process_message 导致字典迭代时修改错误
        self._process_lock = asyncio.Lock()

    # ─── 生命周期 ─────────────────────────────────────────

    async def start_handling(self, engine: Any) -> None:
        """启动事件处理和引擎事件总线监听。

        调用者必须在外层先完成 runtime.start() 初始化引擎。
        此方法注册 _on_event 处理器并开始监听引擎事件总线。
        """
        self._engine = engine
        self._event_bus_task = asyncio.create_task(self._event_bus_listener())
        self.on_event(self._on_event)
        LOG.info("NapCatAdapter 平台集成已启动")

    async def stop_handling(self) -> None:
        """停止事件处理和引擎事件总线监听。"""
        self._running = False
        if self._event_bus_task is not None:
            self._event_bus_task.cancel()
            try:
                await self._event_bus_task
            except asyncio.CancelledError:
                pass
            self._event_bus_task = None
        self._engine = None
        LOG.info("NapCatAdapter 平台集成已停止")

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
        """自动重连循环：连接断开后指数退避重试。"""
        delay = self._RECONNECT_BASE_DELAY
        attempts = 0
        while self._running:
            if self.ws is None or _is_ws_closed(self.ws):
                if await self._connect_once():
                    delay = self._RECONNECT_BASE_DELAY
                    attempts = 0
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
                            "NapCat WS 重连次数耗尽 (%s 次)，停止重连",
                            self._MAX_RECONNECT_ATTEMPTS,
                        )
                        break
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self._RECONNECT_MAX_DELAY)
                    attempts += 1
            else:
                await asyncio.sleep(self.reconnect_interval)

    # ─── 事件分发 ─────────────────────────────────────────

    def on_event(self, handler: EventHandler) -> None:
        """注册事件处理器。"""
        self._event_handlers.append(handler)

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

        for handler in self._event_handlers:
            try:
                asyncio.create_task(handler(data))
            except Exception:
                LOG.exception("Event handler error")

    # ── 消息发送限流（每群/私聊独立） ────────────────────

    @staticmethod
    def _is_send_action(action: str) -> bool:
        return action in ("send_group_msg", "send_private_msg")

    def _send_channel_key(self, action: str, params: dict[str, Any]) -> str:
        if action == "send_group_msg":
            return f"group_{params.get('group_id', '')}"
        if action == "send_private_msg":
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
        try:
            await self.ws.send(json.dumps(payload))
        except Exception as exc:
            self._pending.pop(echo, None)
            raise RuntimeError(f"Failed to send API request: {exc}") from exc

        try:
            resp = await asyncio.wait_for(future, timeout=self.api_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            raise RuntimeError(f"API timeout: {action}")
        finally:
            self._pending.pop(echo, None)

        if resp.get("status") != "ok":
            retcode = resp.get("retcode", -1)
            wording = resp.get("wording", "unknown error")
            raise RuntimeError(f"API error: {action} retcode={retcode} {wording}")
        return resp

    # ─── BaseAdapter 接口实现 ──────────────────────────────

    async def send_group_message(
        self, group_id: str, message: MessageGroup | str
    ) -> dict[str, Any]:
        """发送群聊消息（平台无关接口）。"""
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

    async def upload_group_file(
        self, group_id: str | int, file_path: str, name: str = ""
    ) -> dict[str, Any]:
        """上传文件到群文件。"""
        return await self.call_api(
            "upload_group_file",
            {
                "group_id": int(group_id),
                "file": file_path,
                "name": name or Path(file_path).name,
            },
        )

    async def upload_private_file(
        self, user_id: str | int, file_path: str, name: str = ""
    ) -> dict[str, Any]:
        """上传文件到私聊。"""
        return await self.call_api(
            "upload_private_file",
            {
                "user_id": int(user_id),
                "file": file_path,
                "name": name or Path(file_path).name,
            },
        )

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
        if post_type != "message":
            return None

        msg_type = raw_event.get("message_type", "")
        uid = str(raw_event.get("user_id", ""))
        self_id = str(raw_event.get("self_id", ""))

        if msg_type == "group":
            gid = str(raw_event.get("group_id", ""))
        elif msg_type == "private":
            gid = f"private_{uid}"
        else:
            gid = ""

        nickname, card = self.extract_sender_names(raw_event)

        if msg_type == "group":
            prompt = await self._render_group_prompt(raw_event, self_id, gid)
        elif msg_type == "private":
            prompt = await self._render_private_prompt(raw_event)
        else:
            return None

        if not prompt:
            return None

        multimodal_inputs: list[dict[str, str]] = []
        # 提取 @ 提及目标（用于小跟班触发判断等平台级语义）
        at_user_ids: list[str] = []
        mention_all = False
        for seg in raw_event.get("message", []):
            if seg.get("type") == "image":
                data = seg.get("data", {})
                url = data.get("url", "") or data.get("file", "")
                sub_type = data.get("sub_type", "")
                if url:
                    is_sticker = str(sub_type) == "1"
                    local_path = await self.cache_image(str(url), is_sticker=is_sticker)
                    mm_item: dict[str, str] = {
                        "type": "image",
                        "value": local_path,
                        "file_path": local_path,
                    }
                    if is_sticker:
                        mm_item["sub_type"] = "1"
                    multimodal_inputs.append(mm_item)
            elif seg.get("type") == "at":
                at_qq = str(seg.get("data", {}).get("qq", ""))
                if at_qq == "all":
                    mention_all = True
                elif at_qq:
                    at_user_ids.append(at_qq)

        # 提取平台消息 ID（用于引用回复）
        msg_id = str(raw_event.get("message_id", ""))

        return ParsedEvent(
            group_id=gid,
            user_id=uid,
            self_id=self_id,
            message_type=msg_type,
            prompt=prompt,
            nickname=nickname,
            card=card,
            message_id=msg_id,
            multimodal_inputs=multimodal_inputs,
            at_user_ids=at_user_ids,
            mention_all=mention_all,
        )

    async def _render_group_prompt(self, event: dict[str, Any], self_id: str, group_id: str) -> str:
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
                quote_text = await self._resolve_quote_content(data)
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

    async def _resolve_quote_content(self, data: dict[str, Any]) -> str:
        """解析引用消息段，通过 get_msg API 获取被引用消息内容。

        Returns:
            格式化的引用文本，如 ``[引用消息 msg_id="123" speaker="张三"] 内容 [/引用消息]``
            获取失败时返回空字符串。
        """
        msg_id = str(data.get("id", ""))
        if not msg_id:
            return ""
        try:
            resp = await self.call_api("get_msg", {"message_id": int(msg_id)})
            msg_data = resp.get("data", {})
            # 提取被引用消息的文本内容
            raw_segments = msg_data.get("message", [])
            text_parts: list[str] = []
            for seg in raw_segments:
                if seg.get("type") == "text":
                    text_parts.append(seg.get("data", {}).get("text", ""))
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

    async def _render_private_prompt(self, event: dict[str, Any]) -> str:
        """将私聊 OneBot 消息段渲染为引擎可读的 prompt 文本。"""
        from ..protocol import _face_to_text, build_image_label

        parts: list[str] = []
        image_index = 1
        image_names: dict[str, int] = {}

        for seg in event.get("message", []):
            seg_type = seg.get("type")
            data = seg.get("data", {})
            if seg_type == "reply":
                quote_text = await self._resolve_quote_content(data)
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

    @staticmethod
    def extract_sender_names(event: dict[str, Any]) -> tuple[str, str]:
        from ..protocol import extract_sender_names

        return extract_sender_names(event)

    # ─── 事件入口 ─────────────────────────────────────────

    async def _on_event(self, event: dict[str, Any]) -> None:
        post_type = event.get("post_type")
        if post_type != "message":
            return
        self._event_queue.put_nowait(event)

        msg_type = event.get("message_type")
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
        await self._process_event(event)

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

    async def _process_event(self, event: dict[str, Any]) -> None:
        """统一消息处理：解析 → 引擎 → 发送。"""
        async with self._process_lock:
            await self._process_event_impl(event)

    async def _process_event_impl(self, event: dict[str, Any]) -> None:
        """实际的消息处理逻辑，受 _process_lock 保护。"""
        parsed = await self.parse_event(event)
        if parsed is None:
            return

        # 记录 Bot 自身的 platform_uid
        if self._engine is not None and parsed.self_id:
            self._engine._bot_platform_uids["qq_native_sirius_pulse"] = parsed.self_id
        if parsed.message_type == "group":
            await self._publish_group_metadata(parsed.group_id, parsed.self_id)

        # ── 小跟班模式：早期过滤 ──
        sidekick_cfg = self.plugin_config.get("sidekick", {})
        if sidekick_cfg.get("enabled"):
            handled = await self._try_sidekick_dispatch(parsed, sidekick_cfg)
            if handled:
                return

        speaker_name = parsed.card or parsed.nickname or f"qq_{parsed.user_id}"
        uid = f"qq_{parsed.user_id}"
        group_id = parsed.group_id

        peer_ai_ids = self.plugin_config.get("peer_ai_ids", [])
        is_peer_ai = str(parsed.user_id) in [str(v) for v in peer_ai_ids]

        participant = UnifiedUser(
            name=parsed.nickname or f"qq_{parsed.user_id}",
            user_id=uid,
            identities={"qq_native_sirius_pulse": parsed.user_id},
            aliases=[parsed.card] if parsed.card else [],
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
            adapter_type="napcat",
            sender_type="other_ai" if is_peer_ai else "human",
        )

        msg_preview = (parsed.prompt or "")[:200].replace("\n", " ")
        LOG.info(
            "[收到消息] %s | sender=%s(%s) uid=%s | content=%s",
            f"group={group_id}" if parsed.message_type == "group" else f"private={parsed.user_id}",
            parsed.nickname or "",
            parsed.card or "",
            parsed.user_id,
            msg_preview,
        )

        try:
            result = await self._engine.process_message(
                message=message,
                participants=[participant],
                group_id=group_id,
            )
            for partial in result.get("partial_replies", []):
                if partial:
                    if parsed.message_type == "group":
                        await self._send_group_text(group_id, partial)
                    else:
                        await self._send_private_text(parsed.user_id, partial)

            reply = result.get("reply")
            message_group = result.get("message_group")  # Plugin 多模态输出
            if message_group is not None:
                # 多模态消息：通过 MessageGroup 发送（图片/语音/文件等）
                if parsed.message_type == "group":
                    await self.send_group_message(group_id, message_group)
                else:
                    await self.send_private_message(parsed.user_id, message_group)
            elif reply:
                clean_reply = reply.strip()
                if clean_reply:
                    if parsed.message_type == "group":
                        await self._send_group_text(group_id, clean_reply)
                    else:
                        await self._send_private_text(parsed.user_id, clean_reply)
            await self._send_stickers_after_reply(group_id, result.get("sticker_names", []))
        except asyncio.CancelledError:
            raise
        except RuntimeError as exc:
            LOG.exception("引擎处理错误 (%s/%s): %s", group_id, parsed.user_id, exc)
        except Exception as exc:
            LOG.exception("消息处理异常 (%s/%s): %s", group_id, parsed.user_id, exc)

    # ─── 小跟班模式 ──────────────────────────────────────

    async def _try_sidekick_dispatch(
        self, parsed: ParsedEvent, cfg: dict[str, Any]
    ) -> bool:
        """尝试以小跟班模式处理消息。

        Returns True 表示消息已被小跟班处理（或被忽略），不应走普通路径。
        Returns False 表示不满足小跟班条件，应走普通路径。
        """
        # 忽略自己发的消息
        if parsed.self_id and parsed.user_id == parsed.self_id:
            return True

        host_qq_ids = [str(v) for v in cfg.get("host_qq_ids", [])]
        host_aliases = [str(v) for v in cfg.get("host_aliases", [])]
        allow_private = bool(cfg.get("allow_private_from_host", False))
        allow_text_alias = bool(cfg.get("allow_text_alias_trigger", False))
        require_at_self = bool(cfg.get("require_at_self", True))

        # 群聊：检查 allowed_group_ids
        if parsed.message_type == "group":
            allowed_groups = self._get_allowed_group_ids()
            if allowed_groups and parsed.group_id not in allowed_groups:
                return True  # 不在允许的群中，忽略
        elif parsed.message_type == "private":
            if not allow_private:
                return True  # 私聊默认忽略
        else:
            return True

        # 检查发送者是否为宿主
        is_host = str(parsed.user_id) in host_qq_ids
        if not is_host and host_aliases:
            sender_name = parsed.card or parsed.nickname or ""
            is_host = any(alias in sender_name for alias in host_aliases) if allow_text_alias else False
        if not is_host:
            return True  # 非宿主，忽略（以小跟班模式）

        # 群聊必须 @self
        if parsed.message_type == "group" and require_at_self:
            self_id = str(parsed.self_id)
            qq_number = str(self.plugin_config.get("qq_number", ""))
            mentioned_self = self_id in parsed.at_user_ids or (qq_number and qq_number in parsed.at_user_ids)
            if not mentioned_self:
                return True  # 宿主未 @ 小跟班

        # 满足所有条件：提取任务文本并分发
        task_text = self._extract_sidekick_task(parsed, cfg)
        if not task_text:
            return True

        LOG.info("[小跟班] 宿主 %s 指派任务: %s", parsed.user_id, task_text[:100])

        # 调用引擎的小跟班处理方法
        if self._engine is None or not self._engine_ready():
            LOG.warning("[小跟班] 引擎未就绪，忽略任务")
            return True

        try:
            result = await self._engine.process_sidekick_task(
                host_user_id=parsed.user_id,
                host_nickname=parsed.nickname or parsed.card or f"qq_{parsed.user_id}",
                task_text=task_text,
                group_id=parsed.group_id,
                message_type=parsed.message_type,
                platform_message_id=parsed.message_id,
                at_user_ids=parsed.at_user_ids,
                mention_all=parsed.mention_all,
            )
            # 发送回复
            reply = result.get("reply", "")
            if reply:
                if parsed.message_type == "group":
                    await self._send_group_text(parsed.group_id, reply)
                else:
                    await self._send_private_text(parsed.user_id, reply)
            await self._send_stickers_after_reply(
                parsed.group_id,
                result.get("sticker_names", []),
            )
        except Exception as exc:
            LOG.exception("[小跟班] 任务处理异常: %s", exc)

        return True

    def _extract_sidekick_task(self, parsed: ParsedEvent, cfg: dict[str, Any]) -> str:
        """从消息中提取小跟班任务文本，去除 @ 自我提及。"""
        if not cfg.get("strip_self_mention_from_task", True):
            return parsed.prompt

        text = parsed.prompt
        # 去除 @bot 的昵称标记（prompt 中渲染为 @bot_name 或 @qq_12345）
        persona_name = self._persona_name
        if persona_name:
            text = text.replace(f"@{persona_name}", "").strip()
        qq_number = str(self.plugin_config.get("qq_number", ""))
        if qq_number:
            text = text.replace(f"@qq_{qq_number}", "").strip()
        # 去除 @全体成员
        text = text.replace("@全体成员", "").strip()
        return text

    # ─── 事件总线监听 ────────────────────────────────────

    async def _event_bus_listener(self) -> None:
        engine = self._engine
        while self._running and engine is not None:
            try:
                async for event in engine.event_bus.subscribe():
                    if not self._running:
                        break
                    asyncio.create_task(self._handle_event(event))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                LOG.warning("事件总线监听异常: %s", exc)
                await asyncio.sleep(1)

    async def _handle_event(self, event: SessionEvent) -> None:
        engine = self._engine
        if engine is None:
            return
        try:
            if event.type == SessionEventType.PROACTIVE_RESPONSE_TRIGGERED:
                gid = str(event.data.get("group_id", ""))
                reply = event.data.get("reply", "")
                sticker_names = event.data.get("sticker_names", [])
                if gid in self._get_allowed_group_ids():
                    if not engine.is_proactive_enabled(gid):
                        return
                    if reply:
                        await self._send_group_text(gid, reply)
                    await self._send_stickers_after_reply(gid, sticker_names)
            elif event.type == SessionEventType.DELAYED_RESPONSE_TRIGGERED:
                gid = str(event.data.get("group_id", ""))

                async def _send_partial(text: str) -> None:
                    if gid.startswith("private_"):
                        uid = gid.replace("private_", "").replace("qq_", "")
                        sent = await self._send_private_text(uid, text)
                    elif gid in self._get_allowed_group_ids():
                        sent = await self._send_group_text(gid, text)
                    else:
                        raise RuntimeError(f"Partial reply target is not allowed: {gid}")
                    if not sent:
                        raise RuntimeError(f"Failed to send partial reply: {gid}")

                try:
                    results = await engine.tick_delayed_queue(gid, on_partial_reply=_send_partial)
                except Exception as exc:
                    LOG.warning("Delayed queue tick 失败 (%s): %s", gid, exc)
                    results = []
                for result in results:
                    reply = result.get("reply", "")
                    reply_refs = result.get("reply_references", [])
                    sticker_names = result.get("sticker_names", [])
                    if gid.startswith("private_"):
                        uid = gid.replace("private_", "").replace("qq_", "")
                        if reply:
                            await self._send_private_text(uid, reply, reply_refs)
                        await self._send_stickers_after_reply(gid, sticker_names)
                    elif gid in self._get_allowed_group_ids():
                        if reply:
                            await self._send_group_text(gid, reply, reply_refs)
                        await self._send_stickers_after_reply(gid, sticker_names)
            elif event.type == SessionEventType.DEVELOPER_CHAT_TRIGGERED:
                gid = str(event.data.get("group_id", ""))
                reply = event.data.get("reply", "")
                if reply and gid.startswith("private_"):
                    uid = gid.replace("private_", "").replace("qq_", "")
                    await self._send_private_text(uid, reply)
            elif event.type == SessionEventType.REMINDER_TRIGGERED:
                gid = str(event.data.get("group_id", ""))
                reply = event.data.get("reply", "")
                adapter_type = event.data.get("adapter_type", "")
                image_path = str(event.data.get("image_path", "")).strip()
                if reply and adapter_type == self.adapter_type:
                    if gid.startswith("private_"):
                        uid = gid.replace("private_", "").replace("qq_", "")
                        await self._send_private_text(uid, reply)
                        if image_path:
                            await self._send_private_image(uid, image_path)
                    elif gid in self._get_allowed_group_ids():
                        await self._send_group_text(gid, reply)
                        if image_path:
                            await self._send_group_image(gid, image_path)
        except Exception as exc:
            LOG.warning("事件处理异常: %s", exc)

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
        # 最终兜底：按换行符拆分为多条消息，仅首条携带引用
        lines = [line for line in text.split("\n") if line.strip()]
        if len(lines) > 1:
            first = True
            for line in lines:
                refs = reply_refs if first else None
                ok = await self._send_group_text_single(group_id, line, refs)
                if not ok:
                    return False
                first = False
            return True
        return await self._send_group_text_single(group_id, text, reply_refs)

    async def _send_group_text_single(
        self, group_id: str, text: str, reply_refs: list[dict[str, str]] | None = None
    ) -> bool:
        async with self._get_reply_lock(group_id):
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
        """将模型输出的 @昵称/@别称/@QQ号 转换为 @{QQ号} 格式。"""
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
                return f"@{{{raw}}}" if raw in valid_ids else m.group(0)
            # 文字：匹配昵称/群名片/别称
            for known in sorted_names:
                if raw == known:
                    return f"@{{{name_to_id[known]}}}"
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
        # 最终兜底：按换行符拆分为多条消息
        lines = [line for line in text.split("\n") if line.strip()]
        if len(lines) > 1:
            for line in lines:
                ok = await self._send_private_text_single(user_id, line)
                if not ok:
                    return False
            return True
        return await self._send_private_text_single(user_id, text)

    async def _send_private_text_single(self, user_id: str, text: str) -> bool:
        async with self._get_reply_lock(user_id):
            try:
                await self.send_private_msg(user_id, text)
                LOG.info("回复私聊 %s: %s", user_id, text[:120])
                return True
            except Exception as exc:
                LOG.warning("发送私聊消息失败: %s", exc)
                return False

    async def _send_stickers_after_reply(self, group_id: str, names: Any) -> None:
        if not names or self._engine is None:
            return
        if isinstance(names, str):
            sticker_names = [names.strip()] if names.strip() else []
        else:
            sticker_names = [str(name).strip() for name in names if str(name).strip()]
        if not sticker_names:
            return
        try:
            await self._engine._send_stickers_by_names(group_id, sticker_names)
        except Exception as exc:
            LOG.warning("发送回复后的表情包失败: %s", exc)

    async def _send_group_image(self, group_id: str, image_path: str) -> None:
        """发送群聊图片。"""
        segment: list[dict[str, Any]] = [{"type": "image", "data": {"file": image_path}}]
        async with self._get_reply_lock(group_id):
            try:
                await self.send_group_msg(group_id, segment)
                LOG.info("回复群 %s 图片: %s", group_id, image_path)
            except Exception as exc:
                LOG.warning("发送群图片失败: %s", exc)

    async def _send_private_image(self, user_id: str, image_path: str) -> None:
        """发送私聊图片。"""
        segment: list[dict[str, Any]] = [{"type": "image", "data": {"file": image_path}}]
        async with self._get_reply_lock(user_id):
            try:
                await self.send_private_msg(user_id, segment)
                LOG.info("回复私聊 %s 图片: %s", user_id, image_path)
            except Exception as exc:
                LOG.warning("发送私聊图片失败: %s", exc)

    def _get_reply_lock(self, key: str) -> asyncio.Lock:
        lock = self._reply_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._reply_locks[key] = lock
        return lock

    def _engine_ready(self) -> bool:
        """检查引擎是否已就绪。"""
        engine = self._engine
        if engine is None:
            return False
        return getattr(engine, "is_ready", lambda: True)()

    def _log_not_ready(self) -> None:
        loop = asyncio.get_event_loop()
        now = loop.time()
        if now - self._last_not_ready_log >= self._NOT_READY_LOG_INTERVAL:
            self._last_not_ready_log = now
            LOG.warning("引擎未就绪，跳过消息（每 %.0f 秒提示一次）", self._NOT_READY_LOG_INTERVAL)

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
