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
import json
import logging
from pathlib import Path
from typing import Any, Callable

import websockets
import websockets.exceptions

from sirius_chat.adapters.base import BaseAdapter
from sirius_chat.adapters.models import (
    MessageGroup, TextSegment, AtSegment,
    ImageSegment, VoiceSegment, FileSegment, ReplySegment,
)
from sirius_chat.models.models import Message, Participant
from sirius_chat.skills.executor import strip_skill_calls
from sirius_chat.core.events import SessionEvent, SessionEventType

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

        self.ws: websockets.WebSocketClientProtocol | None = None
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
        self._event_bus_task: asyncio.Task | None = None

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
                    if self._MAX_RECONNECT_ATTEMPTS > 0 and attempts >= self._MAX_RECONNECT_ATTEMPTS:
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

    # ─── API 调用 ─────────────────────────────────────────

    async def call_api(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """通过 WebSocket 发送 OneBot API 请求并等待响应。"""
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
                segments.append({"type": "file", "data": {"file": seg.file_path, "name": seg.name or Path(seg.file_path).name}})
        return segments

    # ─── 事件解析（OneBot → 引擎格式） ──────────────────────

    async def parse_event(self, event: dict[str, Any]) -> "ParsedEvent | None":
        """将原始 OneBot 事件解析为引擎可消费的结构化格式。

        包含：表情→文字转换、@→昵称替换、图片标签生成。
        """
        from sirius_chat.adapters.models import ParsedEvent

        post_type = event.get("post_type")
        if post_type != "message":
            return None

        msg_type = event.get("message_type", "")
        gid = str(event.get("group_id", ""))
        uid = str(event.get("user_id", ""))
        self_id = str(event.get("self_id", ""))

        if msg_type == "private":
            gid = f"private_{uid}"

        nickname, card = self._extract_sender_names(event)

        if msg_type == "group":
            prompt = await self._render_group_prompt(event, self_id, gid)
        elif msg_type == "private":
            prompt = await self._render_private_prompt(event)
        else:
            return None

        if not prompt:
            return None

        multimodal_inputs: list[dict[str, str]] = []
        for seg in event.get("message", []):
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

        return ParsedEvent(
            group_id=gid,
            user_id=uid,
            self_id=self_id,
            message_type=msg_type,
            prompt=prompt,
            nickname=nickname,
            card=card,
            multimodal_inputs=multimodal_inputs,
        )

    async def _render_group_prompt(
        self, event: dict[str, Any], self_id: str, group_id: str
    ) -> str:
        """将群聊 OneBot 消息段渲染为引擎可读的 prompt 文本。"""
        from ..protocol import _face_to_text, build_image_label, extract_sender_names

        parts: list[str] = []
        mention_cache: dict[str, str] = {}
        image_index = 1
        image_names: dict[str, int] = {}

        for seg in event.get("message", []):
            seg_type = seg.get("type")
            data = seg.get("data", {})
            if seg_type == "text":
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

    async def _render_private_prompt(self, event: dict[str, Any]) -> str:
        """将私聊 OneBot 消息段渲染为引擎可读的 prompt 文本。"""
        from ..protocol import _face_to_text, build_image_label

        parts: list[str] = []
        image_index = 1
        image_names: dict[str, int] = {}

        for seg in event.get("message", []):
            seg_type = seg.get("type")
            data = seg.get("data", {})
            if seg_type == "text":
                parts.append(data.get("text", ""))
            elif seg_type == "face":
                parts.append(_face_to_text(data))
            elif seg_type == "image":
                label = "动画表情" if str(data.get("sub_type", "")) == "1" else "图片"
                parts.append(build_image_label(seg, image_index, label, image_names))
                image_index += 1

        return "".join(parts).strip()

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
        gid = str(event.get("group_id", ""))
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
        parsed = await self.parse_event(event)
        if parsed is None:
            return

        speaker_name = parsed.card or parsed.nickname or f"qq_{parsed.user_id}"
        uid = f"qq_{parsed.user_id}"
        group_id = parsed.group_id

        peer_ai_ids = self.plugin_config.get("peer_ai_ids", [])
        is_peer_ai = str(parsed.user_id) in [str(v) for v in peer_ai_ids]

        participant = Participant(
            name=parsed.nickname or f"qq_{parsed.user_id}",
            user_id=uid,
            identities={"qq_native_sirius_chat": parsed.user_id},
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
            channel="qq_native_sirius_chat",
            channel_user_id=parsed.user_id,
            group_id=group_id,
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
                clean_reply = strip_skill_calls(reply).strip()
                if clean_reply:
                    if parsed.message_type == "group":
                        await self._send_group_text(group_id, clean_reply)
                    else:
                        await self._send_private_text(parsed.user_id, clean_reply)
        except asyncio.CancelledError:
            raise
        except RuntimeError as exc:
            LOG.error("引擎处理错误 (%s/%s): %s", group_id, parsed.user_id, exc)
        except Exception as exc:
            LOG.exception("消息处理异常 (%s/%s): %s", group_id, parsed.user_id, exc)

    # ─── 事件总线监听 ────────────────────────────────────

    async def _event_bus_listener(self) -> None:
        not_ready_backoff = 1.0
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
                if reply and gid in self._get_allowed_group_ids():
                    if not engine.is_proactive_enabled(gid):
                        return
                    await self._send_group_text(gid, reply)
            elif event.type == SessionEventType.DELAYED_RESPONSE_TRIGGERED:
                gid = str(event.data.get("group_id", ""))

                async def _send_partial(text: str) -> None:
                    if gid.startswith("private_"):
                        uid = gid.replace("private_", "").replace("qq_", "")
                        await self._send_private_text(uid, text)
                    elif gid in self._get_allowed_group_ids():
                        await self._send_group_text(gid, text)

                try:
                    results = await engine.tick_delayed_queue(
                        gid, on_partial_reply=_send_partial
                    )
                except Exception as exc:
                    LOG.warning("Delayed queue tick 失败 (%s): %s", gid, exc)
                    results = []
                for result in results:
                    reply = result.get("reply", "")
                    if gid.startswith("private_"):
                        uid = gid.replace("private_", "").replace("qq_", "")
                        if reply:
                            await self._send_private_text(uid, reply)
                    elif gid in self._get_allowed_group_ids():
                        if reply:
                            await self._send_group_text(gid, reply)
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
                if reply and adapter_type == self.adapter_type:
                    if gid.startswith("private_"):
                        uid = gid.replace("private_", "").replace("qq_", "")
                        await self._send_private_text(uid, reply)
                    elif gid in self._get_allowed_group_ids():
                        await self._send_group_text(gid, reply)
        except Exception as exc:
            LOG.warning("事件处理异常: %s", exc)

    # ─── 消息发送（引擎回调） ─────────────────────────────

    async def _send_group_text(self, group_id: str, text: str) -> None:
        async with self._get_reply_lock(group_id):
            try:
                await self.send_group_msg(group_id, text)
                LOG.info("回复群 %s: %s", group_id, text[:120])
            except Exception as exc:
                LOG.warning("发送群消息失败: %s", exc)

    async def _send_private_text(self, user_id: str, text: str) -> None:
        async with self._get_reply_lock(user_id):
            try:
                await self.send_private_msg(user_id, text)
                LOG.info("回复私聊 %s: %s", user_id, text[:120])
            except Exception as exc:
                LOG.warning("发送私聊消息失败: %s", exc)

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
