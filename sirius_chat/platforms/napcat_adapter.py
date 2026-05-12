"""原生 NapCat OneBot v11 Adapter。

通过正向 WebSocket 接收事件，并在同一连接上发送 API 调用（OneBot v11 标准）。
支持自动重连、心跳检测、并发请求隔离（echo 机制）。

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
    """轻量级 NapCat OneBot v11 正向 WebSocket 客户端。"""

    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 30.0
    _MAX_RECONNECT_ATTEMPTS = 5  # 0 = 无限重试

    adapter_type = "napcat"

    def __init__(
        self,
        ws_url: str,
        token: str | None = None,
        reconnect_interval: float = 5.0,
        api_timeout: float = 30.0,
        work_path: str | Path | None = None,
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

    # ─── 生命周期 ─────────────────────────────────────────

    async def connect(self) -> None:
        """建立 WebSocket 连接并启动监听循环。"""
        self._running = True
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def close(self) -> None:
        """关闭连接并清理资源（取消所有在途 API 调用）。"""
        self._running = False
        # 取消所有 pending futures，避免在途调用挂死
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
        from .napcat_protocol import ParsedEvent

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
        from .napcat_protocol import (
            _face_to_text, build_image_label, extract_sender_names,
        )

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
        from .napcat_protocol import _face_to_text, build_image_label

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

    async def cache_image(self, url: str, *, is_sticker: bool = False) -> str:
        """下载并缓存 OneBot 图片到本地。"""
        import hashlib
        import aiohttp

        if not url.startswith(("http://", "https://")):
            return url

        cache_dir = self._sticker_cache_dir if is_sticker else self._image_cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(url.split("?")[0]).suffix or ".jpg"
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.0"
                    ),
                    "Referer": "https://multimedia.nt.qq.com.cn/",
                }
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 10 * 1024 * 1024:
                            LOG.warning("图片过大(%d bytes)，跳过缓存: %s", len(data), url[:80])
                            return url
                        content_hash = hashlib.md5(data).hexdigest()
                        cache_path = cache_dir / f"{content_hash}{ext}"
                        if cache_path.exists():
                            return str(cache_path)
                        cache_path.write_bytes(data)
                        (cache_dir / f"{content_hash}{ext}.url").write_text(url, encoding="utf-8")
                        if not is_sticker:
                            await self._cleanup_cache(cache_dir, max_files=200)
                        return str(cache_path)
        except Exception as exc:
            LOG.warning("图片下载异常: %s | %s", exc, url[:80])
        return url

    async def _cleanup_cache(self, cache_dir: Path, max_files: int = 200) -> None:
        """清理缓存目录，保留最近 max_files 个文件。"""
        if not cache_dir.exists():
            return
        files = sorted(
            [f for f in cache_dir.iterdir() if not f.name.endswith(".url")],
            key=lambda p: p.stat().st_mtime, reverse=True
        )
        if len(files) > max_files:
            for old_file in files[max_files:]:
                try:
                    old_file.unlink()
                except Exception:
                    pass

    # ─── 配置与权限 ───────────────────────────────────────────

    @property
    def _persona_name(self) -> str:
        """人格名称（由外部设置）。"""
        return getattr(self, "_persona_name_val", "") or ""

    def set_persona_name(self, name: str) -> None:
        """设置人格名称（在 bridge start 时注入）。"""
        self._persona_name_val = name

    @staticmethod
    def extract_sender_names(event: dict[str, Any]) -> tuple[str, str]:
        from .napcat_protocol import extract_sender_names
        return extract_sender_names(event)
