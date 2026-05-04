"""SiriusChat v1.0 — NapCat 原生桥接器。

职责：
    - 接收 NapCat OneBot v11 事件（群聊/私聊）
    - 渲染 prompt、处理 multimodal（图片缓存）
    - 调用 EmotionalGroupChatEngine 生成回复
    - 后台 delayed / proactive / reminder 投递
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import aiohttp

from sirius_chat.models.models import Message, Participant
from sirius_chat.skills.executor import strip_skill_calls

from .napcat_adapter import NapCatAdapter
from .runtime import EngineRuntime
from sirius_chat.core.events import SessionEventType

LOG = logging.getLogger("sirius.platforms.napcat_bridge")


def _extract_text_from_segments(message: list[dict[str, Any]]) -> str:
    """从 OneBot 消息段数组中提取纯文本。"""
    parts: list[str] = []
    for seg in message:
        if seg.get("type") == "text":
            parts.append(seg.get("data", {}).get("text", ""))
    return "".join(parts).strip()


def _extract_image_urls(message: list[dict[str, Any]]) -> list[str]:
    """从消息段中提取所有图片 URL。"""
    urls: list[str] = []
    for seg in message:
        if seg.get("type") == "image":
            data = seg.get("data", {})
            url = data.get("url", "")
            if not url:
                url = data.get("file", "")
            if url:
                urls.append(url)
    return urls


class ConfigStore:
    """轻量级 JSON 配置存储。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._config: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._config = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                self._config = {}
        else:
            self._config = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._config, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._config[key] = value
        self._save()

    def __contains__(self, key: str) -> bool:
        return key in self._config


class NapCatBridge:
    """QQ 群聊/私聊与 SiriusChat Engine 的桥接器。"""

    _NOT_READY_LOG_INTERVAL = 30.0  # 引擎未就绪提示的日志间隔（秒）

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
        self._image_cache_dir = self.work_path / "image_cache"
        self._sticker_cache_dir = self.work_path / "sticker_cache"

        # Bridge 内部状态持久化
        # 旧文件 qq_bridge_config.json 已废弃，adapter 配置统一走 adapters.json
        self._state_path = self.work_path / "engine_state" / "bridge_state.json"
        self._store = ConfigStore(self._state_path)
        self._migrate_and_cleanup_old_bridge_config()

        for key, value in (
            ("setup_completed", False),
            ("setup_wizard_running", False),
        ):
            if key not in self._store:
                self._store.set(key, value)

        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._event_bus_task: asyncio.Task | None = None

    # ─── 生命周期 ─────────────────────────────────────────

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
        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

        msg_type = event.get("message_type")
        if msg_type == "group":
            await self._on_group_message(event)
        elif msg_type == "private":
            await self._on_private_message(event)

    # ─── 群聊处理 ─────────────────────────────────────────

    async def _on_group_message(self, event: dict[str, Any]) -> None:
        gid = str(event.get("group_id", ""))
        uid = str(event.get("user_id", ""))
        self_id = str(event.get("self_id", ""))
        LOG.info("收到群消息: group_id=%s user_id=%s", gid, uid)

        allowed_gids = self._get_allowed_group_ids()
        if gid not in allowed_gids:
            LOG.debug("群号不在白名单，跳过: %s", gid)
            return
        if uid == self_id:
            return
        if not self._enabled:
            return
        cfg = self._load_adapter_cfg()
        if cfg is not None and getattr(cfg, "enable_group_chat", True) is False:
            return

        prompt = await self._render_group_prompt(event)
        if not prompt:
            return

        if not self.runtime.is_ready():
            now = asyncio.get_event_loop().time()
            if now - self._last_not_ready_log >= self._NOT_READY_LOG_INTERVAL:
                self._last_not_ready_log = now
                LOG.warning("引擎未就绪，跳过消息（每 %.0f 秒提示一次）", self._NOT_READY_LOG_INTERVAL)
            return

        await self._process_message(gid, uid, prompt, event)

    # ─── 私聊处理 ─────────────────────────────────────────

    async def _on_private_message(self, event: dict[str, Any]) -> None:
        uid = str(event.get("user_id", ""))
        self_id = str(event.get("self_id", ""))
        LOG.info("收到私聊消息: user_id=%s", uid)

        if uid == self_id:
            return

        allowed_priv_uids = self._get_allowed_private_user_ids()
        if allowed_priv_uids and uid not in allowed_priv_uids:
            LOG.debug("私聊用户不在白名单，跳过: %s", uid)
            return

        if not self._enabled:
            return
        cfg = self._load_adapter_cfg()
        if cfg is not None and getattr(cfg, "enable_private_chat", True) is False:
            return

        prompt = await self._render_private_prompt(event)
        if not prompt:
            return

        if not self.runtime.is_ready():
            now = asyncio.get_event_loop().time()
            if now - self._last_not_ready_log >= self._NOT_READY_LOG_INTERVAL:
                self._last_not_ready_log = now
                LOG.warning("引擎未就绪，跳过消息（每 %.0f 秒提示一次）", self._NOT_READY_LOG_INTERVAL)
            return

        await self._process_message(f"private_{uid}", uid, prompt, event)

    # ─── 统一消息处理 ─────────────────────────────────────

    async def _process_message(
        self,
        group_id: str,
        user_id: str,
        prompt: str,
        event: dict[str, Any],
    ) -> None:
        memory_channel = "qq_native_sirius_chat"
        nickname, card = self._extract_sender_names(event)
        speaker_name = card or nickname or f"qq_{user_id}"
        uid = f"qq_{user_id}"

        msg_preview = prompt[:200].replace("\n", " ") if prompt else ""
        LOG.info(
            "[收到消息] %s | sender=%s(%s) uid=%s | content=%s",
            f"group={group_id}" if event.get("message_type") == "group" else f"private={user_id}",
            nickname or "",
            card or "",
            user_id,
            msg_preview,
        )

        peer_ai_ids = self.plugin_config.get("peer_ai_ids", [])
        is_peer_ai = str(user_id) in [str(v) for v in peer_ai_ids]

        metadata: dict[str, Any] = {
            "platform": "qq",
            "qq_uid": user_id,
            "is_developer": self._is_admin(user_id),
            "is_ai": is_peer_ai,
        }
        if event.get("message_type") == "group":
            metadata["group_id"] = group_id
        else:
            metadata["scope"] = "private"

        participant = Participant(
            name=nickname or f"qq_{user_id}",
            user_id=uid,
            identities={memory_channel: user_id},
            aliases=[card] if card else [],
            metadata=metadata,
        )

        multimodal_inputs: list[dict[str, str]] = []
        for seg in event.get("message", []):
            if seg.get("type") == "image":
                data = seg.get("data", {})
                url = data.get("url", "") or data.get("file", "")
                sub_type = data.get("sub_type", "")
                if url:
                    # sub_type=1 indicates animated sticker/emoji from QQ;
                    # mark it so downstream can skip vision analysis.
                    is_sticker = str(sub_type) == "1"
                    local_path = await self._cache_image(str(url), is_sticker=is_sticker)
                    mm_item: dict[str, str] = {
                        "type": "image",
                        "value": local_path,
                        "file_path": local_path,
                    }
                    if is_sticker:
                        mm_item["sub_type"] = "1"
                    multimodal_inputs.append(mm_item)

        message = Message(
            role="user",
            content=prompt,
            speaker=speaker_name,
            nickname=nickname,
            channel=memory_channel,
            channel_user_id=user_id,
            group_id=group_id,
            multimodal_inputs=multimodal_inputs,
            adapter_type="napcat",
            sender_type="other_ai" if is_peer_ai else "human",
        )

        try:
            result = await self.runtime.engine.process_message(
                message=message,
                participants=[participant],
                group_id=group_id,
            )
            for partial in result.get("partial_replies", []):
                if partial:
                    if event.get("message_type") == "group":
                        await self._send_group_text_raw(group_id, partial)
                    else:
                        await self._send_private_text_raw(user_id, partial)

            reply = result.get("reply")
            if reply:
                clean_reply = strip_skill_calls(reply).strip()
                if clean_reply:
                    if event.get("message_type") == "group":
                        await self._send_group_text_raw(group_id, clean_reply)
                    else:
                        await self._send_private_text_raw(user_id, clean_reply)
            LOG.info("%s 消息处理完成 | strategy=%s | speaker=%s", group_id, result.get("strategy"), speaker_name)
        except asyncio.CancelledError:
            raise
        except RuntimeError as exc:
            # 引擎内部错误（如模型调用失败），记录但不吞掉上下文
            LOG.error("引擎处理错误 (%s/%s): %s", group_id, user_id, exc)
        except Exception as exc:
            LOG.exception("消息处理异常 (%s/%s): %s", group_id, user_id, exc)

    # ─── 事件总线监听 ────────────────────────────────────

    async def _event_bus_listener(self) -> None:
        """订阅引擎事件总线，投递所有异步事件（主动消息、延迟回复、提醒等）。"""
        while self._running:
            try:
                if not self.runtime.is_ready():
                    await asyncio.sleep(1)
                    continue
                async for event in self.runtime.engine.event_bus.subscribe():
                    if not self._running:
                        break
                    if event.type == SessionEventType.PROACTIVE_RESPONSE_TRIGGERED:
                        gid = str(event.data.get("group_id", ""))
                        reply = event.data.get("reply", "")
                        if reply and gid in self._get_allowed_group_ids():
                            await self._send_group_text_raw(gid, reply)
                            LOG.info("Proactive 回复已发送: %s", reply[:80])
                    elif event.type == SessionEventType.DELAYED_RESPONSE_TRIGGERED:
                        gid = str(event.data.get("group_id", ""))
                        # 事件仅携带触发信号，需要调用 tick_delayed_queue 生成实际回复
                        async def _send_partial(text: str) -> None:
                            if gid.startswith("private_"):
                                uid = gid.replace("private_", "").replace("qq_", "")
                                await self._send_private_text_raw(uid, text)
                                LOG.info("Private partial 回复已发送: %s", text[:80])
                            elif gid in self._get_allowed_group_ids():
                                await self._send_group_text_raw(gid, text)
                                LOG.info("Partial 回复已发送: %s", text[:80])

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
                                    await self._send_private_text_raw(uid, reply)
                                    LOG.info("Private 回复已发送: %s", reply[:80])
                            elif gid in self._get_allowed_group_ids():
                                if reply:
                                    await self._send_group_text_raw(gid, reply)
                                    LOG.info("Delayed 回复已发送: %s", reply[:80])
                    elif event.type == SessionEventType.DEVELOPER_CHAT_TRIGGERED:
                        gid = str(event.data.get("group_id", ""))
                        reply = event.data.get("reply", "")
                        if reply and gid.startswith("private_"):
                            uid = gid.replace("private_", "").replace("qq_", "")
                            await self._send_private_text_raw(uid, reply)
                            LOG.info("Developer 主动私聊已发送: %s", reply[:80])
                    elif event.type == SessionEventType.REMINDER_TRIGGERED:
                        gid = str(event.data.get("group_id", ""))
                        reply = event.data.get("reply", "")
                        adapter_type = event.data.get("adapter_type", "")
                        if reply and adapter_type == self.adapter_type:
                            if gid.startswith("private_"):
                                uid = gid.replace("private_", "").replace("qq_", "")
                                await self._send_private_text_raw(uid, reply)
                                LOG.info("私聊提醒已发送: %s", reply[:80])
                            elif gid in self._get_allowed_group_ids():
                                await self._send_group_text_raw(gid, reply)
                                LOG.info("群提醒已发送: %s", reply[:80])
            except asyncio.CancelledError:
                break
            except Exception as exc:
                LOG.warning("事件总线监听异常: %s", exc)
                await asyncio.sleep(1)

    # ─── 消息渲染 ─────────────────────────────────────────

    async def _render_group_prompt(self, event: dict[str, Any]) -> str:
        parts: list[str] = []
        mention_cache: dict[str, str] = {}
        image_index = 1
        image_names: dict[str, int] = {}
        self_id = str(event.get("self_id", ""))
        group_id = str(event.get("group_id", ""))

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
                        display = self.runtime.get_persona_name()
                    else:
                        display = f"qq_{target_uid}"
                        try:
                            info = await self.adapter.get_group_member_info(group_id, target_uid)
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
            elif seg_type == "image":
                parts.append(self._build_image_label(seg, image_index, "图片", image_names))
                image_index += 1

        rendered = "".join(parts).strip()
        if rendered:
            return rendered
        return event.get("raw_message", "").strip()

    async def _render_private_prompt(self, event: dict[str, Any]) -> str:
        parts: list[str] = []
        image_index = 1
        image_names: dict[str, int] = {}
        for seg in event.get("message", []):
            seg_type = seg.get("type")
            data = seg.get("data", {})
            if seg_type == "text":
                parts.append(data.get("text", ""))
            elif seg_type == "image":
                parts.append(self._build_image_label(seg, image_index, "图片", image_names))
                image_index += 1
        rendered = "".join(parts).strip()
        if rendered:
            return rendered
        return event.get("raw_message", "").strip()

    # ─── 发送工具 ─────────────────────────────────────────

    def _get_reply_lock(self, key: str) -> asyncio.Lock:
        """获取指定 key（group_id 或 user_id）的独立发送锁。"""
        lock = self._reply_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._reply_locks[key] = lock
        return lock

    async def _send_group_text_raw(self, group_id: str, text: str) -> None:
        async with self._get_reply_lock(group_id):
            try:
                await self.adapter.send_group_msg(group_id, text)
                LOG.info("回复群 %s: %s", group_id, text[:120])
            except Exception as exc:
                LOG.warning("发送群消息失败: %s", exc)

    async def _send_private_text_raw(self, user_id: str, text: str) -> None:
        async with self._get_reply_lock(user_id):
            try:
                await self.adapter.send_private_msg(user_id, text)
                LOG.info("回复私聊 %s: %s", user_id, text[:120])
            except Exception as exc:
                LOG.warning("发送私聊消息失败: %s", exc)

    # ─── 图片缓存 ─────────────────────────────────────────

    async def _cache_image(self, url: str, *, is_sticker: bool = False) -> str:
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
                        # Preserve original URL metadata
                        (cache_dir / f"{content_hash}{ext}.url").write_text(
                            url, encoding="utf-8"
                        )
                        if not is_sticker:
                            await self._cleanup_cache_dir(cache_dir, max_files=200)
                        return str(cache_path)
        except Exception as exc:
            LOG.warning("图片下载异常: %s | %s", exc, url[:80])
        return url

    async def _cleanup_cache_dir(self, cache_dir: Path, max_files: int = 200) -> None:
        if not cache_dir.exists():
            return
        files = sorted(cache_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        if len(files) > max_files:
            for old_file in files[max_files:]:
                try:
                    old_file.unlink()
                except Exception:
                    pass

    async def _cleanup_image_cache(self, max_files: int = 200) -> None:
        """Backward-compatible wrapper for image cache cleanup."""
        await self._cleanup_cache_dir(self._image_cache_dir, max_files=max_files)

    # ─── 事件等待（供外部向导使用）─────────────────────────

    async def wait_event(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        """等待满足条件的 OneBot 事件。"""
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

    # ─── 配置辅助 ─────────────────────────────────────────

    def get_config(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def set_config(self, key: str, value: Any) -> None:
        self._store.set(key, value)

    @property
    def data(self) -> dict[str, Any]:
        """暴露底层配置字典。修改后需手动调用 save_data() 持久化。"""
        return self._store._config

    def save_data(self) -> None:
        self._store._save()

    def _load_adapter_cfg(self) -> Any | None:
        """Load the first NapCat adapter config from adapters.json."""
        adapters_path = self.work_path / "adapters.json"
        if not adapters_path.exists():
            return None
        try:
            from sirius_chat.persona_config import PersonaAdaptersConfig

            adapters_cfg = PersonaAdaptersConfig.load(adapters_path)
            if adapters_cfg.adapters:
                return adapters_cfg.adapters[0]
        except Exception:
            pass
        return None

    def _get_allowed_group_ids(self) -> list[str]:
        cfg = self._load_adapter_cfg()
        gids = getattr(cfg, "allowed_group_ids", None) if cfg is not None else None
        if gids is None:
            LOG.error("adapters.json 未配置 allowed_group_ids，群聊功能已禁用")
            return []
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

    def _get_allowed_private_user_ids(self) -> list[str]:
        cfg = self._load_adapter_cfg()
        uids = getattr(cfg, "allowed_private_user_ids", None) if cfg is not None else None
        if uids is None:
            uids = []
        if isinstance(uids, str):
            try:
                parsed = json.loads(uids)
                if isinstance(parsed, list):
                    return [str(u).strip() for u in parsed if u]
            except (json.JSONDecodeError, ValueError):
                pass
            if "," in uids:
                return [u.strip().strip("'\"[]()") for u in uids.split(",") if u.strip()]
            return [uids.strip()] if uids.strip() else []
        return [str(u).strip() for u in uids if u]

    def _migrate_and_cleanup_old_bridge_config(self) -> None:
        """Migrate setup state from deprecated qq_bridge_config.json, then delete it."""
        old_path = self.work_path / "qq_bridge_config.json"
        if not old_path.exists():
            return
        try:
            old_data = json.loads(old_path.read_text(encoding="utf-8"))
            for key in ("setup_completed", "setup_wizard_running", "setup_wizard_notified"):
                if key in old_data:
                    self._store.set(key, old_data[key])
            LOG.info("已从 qq_bridge_config.json 迁移 setup 状态到 %s", self._state_path)
        except Exception as exc:
            LOG.warning("迁移 qq_bridge_config.json 失败: %s", exc)
        try:
            old_path.unlink()
            LOG.info("已清理旧文件: %s", old_path)
        except OSError as exc:
            LOG.warning("删除 qq_bridge_config.json 失败: %s", exc)

    # ─── 权限与工具 ───────────────────────────────────────

    def _is_admin(self, uid: str) -> bool:
        return uid == self._root_user_id()

    def _root_user_id(self) -> str:
        return str(self.plugin_config.get("root", "")).strip()

    @staticmethod
    def _extract_sender_names(event: dict[str, Any]) -> tuple[str, str]:
        sender = event.get("sender", {})
        nickname = str(sender.get("nickname", "") or "").strip()
        card = str(sender.get("card", "") or "").strip()
        return nickname, card

    @staticmethod
    def _sanitize_image_name(name: str) -> str:
        from urllib.parse import unquote
        text = unquote(str(name or "").strip().strip("'\"")).replace("\r", " ").replace("\n", " ")
        text = text.replace("[", "(").replace("]", ")")
        return text[:80].strip()

    @staticmethod
    def _extract_image_name(seg: dict[str, Any], index: int, fallback_prefix: str = "未命名图片") -> str:
        data = seg.get("data", {})
        candidates = [
            data.get("filename", ""),
            data.get("file_name", ""),
            data.get("name", ""),
            data.get("file", ""),
            data.get("url", ""),
        ]
        for raw in candidates:
            text = str(raw or "").strip()
            if text:
                from urllib.parse import urlparse, unquote
                parsed = urlparse(text)
                for candidate in (parsed.path, text):
                    normalized = str(candidate or "").strip().replace("\\", "/").rstrip("/")
                    if not normalized or normalized.startswith(("data:", "base64:")):
                        continue
                    name = NapCatBridge._sanitize_image_name(normalized.split("/")[-1])
                    if name:
                        return name
        return f"{fallback_prefix}_{index}"

    @staticmethod
    def _dedupe_image_name(name: str, counter: dict[str, int]) -> str:
        seen = counter.get(name, 0) + 1
        counter[name] = seen
        if seen == 1:
            return name
        stem, dot, suffix = name.rpartition(".")
        if dot:
            return f"{stem}#{seen}.{suffix}"
        return f"{name}#{seen}"

    def _build_image_label(self, seg: dict[str, Any], index: int, label_prefix: str, counter: dict[str, int]) -> str:
        image_name = self._extract_image_name(seg, index)
        display_name = self._dedupe_image_name(image_name, counter)
        return f"[{label_prefix}: {display_name}]"
