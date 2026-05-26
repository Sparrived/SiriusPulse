"""平台适配器抽象基类。

所有平台适配器（NapCat、Discord 等）必须继承 BaseAdapter 并实现其抽象方法。
Plugin 通过 self.ctx.adapter 直接调用适配器方法，无需中间代理层。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from sirius_pulse.adapters.models import MessageGroup, ParsedEvent

logger = logging.getLogger(__name__)


class BaseAdapter(ABC):
    """平台适配器抽象基类。

    每个方法签名使用平台无关的 MessageGroup/标准类型。
    具体实现负责将其转换为平台特定的底层格式。
    """

    adapter_type: str = ""  # 子类必须覆写，如 "napcat"、"discord"

    # ── 图片缓存（通用，子类可覆写 _cache_image_headers） ──

    async def cache_image(self, url: str, *, is_sticker: bool = False) -> str:
        """下载并缓存图片到本地。

        跨平台通用：HTTP 下载 + MD5 去重 + 本地文件存储。
        子类可覆写 _cache_image_headers() 来适配不同平台的请求头要求。
        """
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
                async with session.get(url, headers=self._cache_image_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 10 * 1024 * 1024:
                            logger.warning("图片过大(%d bytes)，跳过缓存: %s", len(data), url[:80])
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
            logger.warning("图片下载异常: %s | %s", exc, url[:80])
        return url

    @staticmethod
    def _cache_image_headers() -> dict[str, str]:
        """图片缓存请求头（子类可覆写以适配平台要求）。"""
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.0"
            ),
            "Referer": "",
        }

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

    # ── 事件解析（平台 → 统一格式） ──

    @abstractmethod
    async def parse_event(self, raw_event: dict[str, Any]) -> ParsedEvent | None:
        """将平台原始事件解析为统一的 ParsedEvent。

        Args:
            raw_event: 平台原始事件字典

        Returns:
            ParsedEvent 或 None（如果事件不需要处理）
        """
        ...

    # ── 消息发送（核心） ──

    @abstractmethod
    async def send_group_message(
        self, group_id: str, message: MessageGroup | str
    ) -> dict[str, Any]:
        """发送群聊消息。"""
        ...

    @abstractmethod
    async def send_private_message(
        self, user_id: str, message: MessageGroup | str
    ) -> dict[str, Any]:
        """发送私聊消息。"""
        ...

    # ── 消息操作 ──

    async def delete_message(self, message_id: str) -> dict[str, Any]:
        """撤回消息。默认通过 call_api 实现。"""
        return await self.call_api("delete_msg", {"message_id": int(message_id)})

    # ── 群信息 ──

    async def get_group_member_list(self, group_id: str) -> list[dict[str, Any]]:
        """获取群成员列表。"""
        return []

    async def get_group_member_info(
        self, group_id: str, user_id: str, no_cache: bool = False
    ) -> dict[str, Any]:
        """获取群成员信息。"""
        return {}

    async def get_group_info(self, group_id: str) -> dict[str, Any]:
        """获取群信息。"""
        return {}

    async def get_group_msg_history(
        self, group_id: str, message_seq: int | None = None, count: int = 20
    ) -> list[dict[str, Any]]:
        """获取群聊历史消息。

        Args:
            group_id: 群号
            message_seq: 起始消息序号，None 表示从最新开始
            count: 获取数量

        Returns:
            消息列表，每个元素包含 message_id, user_id, time, raw_message 等字段
        """
        return []

    async def get_login_info(self) -> dict[str, Any]:
        """获取登录信息。"""
        return {}

    # ── 群管理 ──

    async def set_group_kick(
        self, group_id: str, user_id: str, reject_add_request: bool = False
    ) -> dict[str, Any]:
        """踢出群成员。"""
        return await self.call_api(
            "set_group_kick",
            {"group_id": int(group_id), "user_id": int(user_id),
             "reject_add_request": reject_add_request},
        )

    async def set_group_ban(
        self, group_id: str, user_id: str, duration: int = 1800
    ) -> dict[str, Any]:
        """禁言群成员。"""
        return await self.call_api(
            "set_group_ban",
            {"group_id": int(group_id), "user_id": int(user_id), "duration": duration},
        )

    async def set_group_whole_ban(self, group_id: str, enable: bool = True) -> dict[str, Any]:
        """全员禁言。"""
        return await self.call_api(
            "set_group_whole_ban", {"group_id": int(group_id), "enable": enable}
        )

    async def set_group_admin(
        self, group_id: str, user_id: str, enable: bool = True
    ) -> dict[str, Any]:
        """设置/取消群管理员。"""
        return await self.call_api(
            "set_group_admin",
            {"group_id": int(group_id), "user_id": int(user_id), "enable": enable},
        )

    async def set_group_card(
        self, group_id: str, user_id: str, card: str = ""
    ) -> dict[str, Any]:
        """设置群名片。"""
        return await self.call_api(
            "set_group_card",
            {"group_id": int(group_id), "user_id": int(user_id), "card": card},
        )

    async def set_group_name(self, group_id: str, name: str) -> dict[str, Any]:
        """设置群名称。"""
        return await self.call_api(
            "set_group_name", {"group_id": int(group_id), "group_name": name}
        )

    # ── 文件 ──

    async def upload_group_file(
        self, group_id: str, file_path: str, name: str = ""
    ) -> dict[str, Any]:
        """上传群文件。"""
        return {}

    async def upload_private_file(
        self, user_id: str, file_path: str, name: str = ""
    ) -> dict[str, Any]:
        """上传私聊文件。"""
        return {}

    # ── 通用 API ──

    @abstractmethod
    async def call_api(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """调用平台底层 API。"""
        ...
