"""平台适配器抽象基类。

所有平台适配器（NapCat、Discord 等）必须继承 BaseAdapter 并实现其抽象方法。
Plugin 通过 self.ctx.adapter 直接调用适配器方法，无需中间代理层。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from sirius_chat.adapters.models import MessageGroup


class BaseAdapter(ABC):
    """平台适配器抽象基类。

    每个方法签名使用平台无关的 MessageGroup/标准类型。
    具体实现负责将其转换为平台特定的底层格式。
    """

    adapter_type: str = ""  # 子类必须覆写，如 "napcat"、"discord"

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
