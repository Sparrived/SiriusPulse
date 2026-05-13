"""SiriusChat v1.3 — NapCat 桥接器（精简版）。

职责：
    - 接收 NapCat 事件 → adapter.parse_event() → engine.process_message()
    - 投递引擎回复到平台
    - 订阅引擎事件总线投递主动/延迟/提醒消息

所有 OneBot 协议解析已移至 NapCatAdapter。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable

from sirius_chat.models.models import Message, Participant
from sirius_chat.skills.executor import strip_skill_calls

from .adapter import NapCatAdapter
from ...runtime import EngineRuntime
from sirius_chat.core.events import SessionEvent, SessionEventType

LOG = logging.getLogger("sirius.platforms.napcat_bridge")


class NapCatBridge:
    """QQ 群聊/私聊 与 SiriusChat Engine 之间的薄桥接层。

    不做协议解析、不做图片缓存——这些已移至 NapCatAdapter。
    只做三件事：
        1. adapter.on_event → adapter.parse_event() → engine.process_message()
        2. 引擎回复 → adapter.send_group_msg() / send_private_msg()
        3. 引擎事件总线 → proactive/delayed/reminder → adapter.send_xxx()
    """

    _NOT_READY_LOG_INTERVAL = 30.0

    def __init__(
        self,
        adapter: NapCatAdapter,
        runtime: EngineRuntime,
        work_path: str | Path,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.adapter = adapter
        self.runtime = runtime
        self.work_path = Path(work_path).resolve()
        self.work_path.mkdir(parents=True, exist_ok=True)
        self.plugin_config = dict(config or {})
        self._enabled = True
        self._running = False
        self.adapter_type = "napcat"
        self._last_not_ready_log: float = 0.0
        self._reply_locks: dict[str, asyncio.Lock] = {}
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._event_bus_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        try:
            await self.runtime.start()
        except Exception as exc:
            LOG.error("引擎启动失败: %s", exc)

        self._event_bus_task = asyncio.create_task(self._event_bus_listener())
        self.adapter.on_event(self._on_event)
        LOG.info("NapCatBridge 已启动")

    async def stop(self) -> None:
        self._running = False
        if self._event_bus_task is not None:
            self._event_bus_task.cancel()
            try:
                await self._event_bus_task
            except asyncio.CancelledError:
                pass
            self._event_bus_task = None
        await self.runtime.stop()
        LOG.info("NapCatBridge 已停止")

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
        if not self.runtime.is_ready():
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
        if not self.runtime.is_ready():
            self._log_not_ready()
            return
        await self._process_event(event)

    async def _process_event(self, event: dict[str, Any]) -> None:
        """统一消息处理：解析 → 引擎 → 发送。"""
        parsed = await self.adapter.parse_event(event)
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
            result = await self.runtime.engine.process_message(
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
            if reply:
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
        while self._running:
            try:
                if not self.runtime.is_ready():
                    await asyncio.sleep(not_ready_backoff)
                    not_ready_backoff = min(not_ready_backoff * 2, 30.0)
                    continue
                not_ready_backoff = 1.0
                async for event in self.runtime.engine.event_bus.subscribe():
                    if not self._running:
                        break
                    asyncio.create_task(self._handle_event(event))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                LOG.warning("事件总线监听异常: %s", exc)
                await asyncio.sleep(1)

    async def _handle_event(self, event: SessionEvent) -> None:
        try:
            if event.type == SessionEventType.PROACTIVE_RESPONSE_TRIGGERED:
                gid = str(event.data.get("group_id", ""))
                reply = event.data.get("reply", "")
                if reply and gid in self._get_allowed_group_ids():
                    if not self.runtime.engine.is_proactive_enabled(gid):
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
                    results = await self.runtime.engine.tick_delayed_queue(
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

    # ─── 消息发送 ─────────────────────────────────────────

    async def _send_group_text(self, group_id: str, text: str) -> None:
        async with self._get_reply_lock(group_id):
            try:
                await self.adapter.send_group_msg(group_id, text)
                LOG.info("回复群 %s: %s", group_id, text[:120])
            except Exception as exc:
                LOG.warning("发送群消息失败: %s", exc)

    async def _send_private_text(self, user_id: str, text: str) -> None:
        async with self._get_reply_lock(user_id):
            try:
                await self.adapter.send_private_msg(user_id, text)
                LOG.info("回复私聊 %s: %s", user_id, text[:120])
            except Exception as exc:
                LOG.warning("发送私聊消息失败: %s", exc)

    def _get_reply_lock(self, key: str) -> asyncio.Lock:
        lock = self._reply_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._reply_locks[key] = lock
        return lock

    # ─── 事件等待 ─────────────────────────────────────────

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

    # ─── 配置辅助（委托给外部管理） ──────────────────────────

    def _log_not_ready(self) -> None:
        import asyncio as _asyncio
        now = _asyncio.get_event_loop().time()
        if now - self._last_not_ready_log >= self._NOT_READY_LOG_INTERVAL:
            self._last_not_ready_log = now
            LOG.warning("引擎未就绪，跳过消息（每 %.0f 秒提示一次）", self._NOT_READY_LOG_INTERVAL)

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
