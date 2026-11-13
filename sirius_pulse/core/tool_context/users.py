"""平台用户查询：按平台账号或名字找人，以及 Bot 自己在各平台的身份。

实现 :class:`sirius_pulse.tools.engine_context.UserLookupAccess`。
"""

from __future__ import annotations

from typing import Any


class UserLookupMixin:
    """平台用户查询：按平台账号或名字找人，以及 Bot 自己在各平台的身份。"""

    _engine: Any

    @property
    def user_lookup(self) -> Any:
        """获取用户查找服务。"""
        return self._user_lookup

    def find_user_by_platform_uid(
        self,
        platform: str,
        platform_uid: str,
        group_id: str = "",
    ) -> dict[str, Any] | None:
        """通过平台 UID 查找用户。"""
        return self._user_lookup.find_by_platform_uid(platform, platform_uid, group_id)

    def find_user_by_name(
        self,
        name: str,
        group_id: str = "",
        *,
        fuzzy: bool = True,
    ) -> dict[str, Any] | None:
        """通过显示名或别名查找用户。"""
        return self._user_lookup.find_by_name(name, group_id, fuzzy=fuzzy)

    def get_user_info(self, user_id: str, group_id: str = "") -> dict[str, Any] | None:
        """获取用户详细信息。"""
        return self._user_lookup.get_info(user_id, group_id)

    def list_users(self, group_id: str = "") -> list[dict[str, Any]]:
        """列出群组中的所有用户。"""
        return self._user_lookup.list_users(group_id)

    def get_bot_id(self) -> str:
        """获取 Bot 自身的 user_id。"""
        return self._user_lookup.get_self_id()

    def get_bot_info(self, group_id: str = "") -> dict[str, Any] | None:
        """获取 Bot 自身的详细信息。"""
        return self._user_lookup.get_self_info(group_id)

    def get_bot_platform_uid(self, platform: str = "") -> str | None:
        """获取 Bot 在指定平台的 UID（如 QQ 号）。

        Args:
            platform: 平台标识（如 "qq_native_sirius_pulse"）。
                      为空时返回当前活跃平台的 UID。
        """
        return self._user_lookup.get_bot_platform_uid(platform)

    def get_bot_platform_uids(self) -> dict[str, str]:
        """获取 Bot 在所有平台的 UID。"""
        return self._user_lookup.get_bot_platform_uids()
