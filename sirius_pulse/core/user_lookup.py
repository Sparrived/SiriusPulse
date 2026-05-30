"""用户查找服务 —— 为插件和技能提供统一的用户查找能力。

组合模式：EngineProxy 和 SkillEngineContextImpl 各持有一个 UserLookupService 实例。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sirius_pulse.core.identity_resolver import IdentityResolver
    from sirius_pulse.memory.biography.manager import BiographyManager
    from sirius_pulse.memory.user.simple import UserManager

logger = logging.getLogger(__name__)


class UserLookupService:
    """统一的用户查找服务。

    通过组合模式注入到 EngineProxy 和 SkillEngineContextImpl，
    避免代码重复。
    """

    def __init__(
        self,
        identity_resolver: IdentityResolver,
        user_manager: UserManager,
        biography_manager: BiographyManager | None = None,
        engine: Any = None,
    ) -> None:
        self._identity_resolver = identity_resolver
        self._user_manager = user_manager
        self._biography_manager = biography_manager
        self._engine = engine

    def find_by_platform_uid(
        self,
        platform: str,
        platform_uid: str,
        group_id: str = "",
    ) -> dict[str, Any] | None:
        """通过平台 UID 查找用户。

        Args:
            platform: 平台标识（如 "qq", "discord"）
            platform_uid: 平台用户 UID（如 QQ 号）
            group_id: 群组 ID（可选，用于群隔离）

        Returns:
            用户信息字典，未找到返回 None
            {"user_id": "...", "name": "...", "confidence": 1.0, "source": "platform_id"}
        """
        try:
            from sirius_pulse.core.identity_resolver import IdentityContext

            ctx = IdentityContext(
                speaker_name="",
                platform_uid=platform_uid,
                platform=platform,
            )
            resolution = self._identity_resolver.resolve_with_alias(
                ctx, self._user_manager, group_id or "default"
            )
            if not resolution.user_id or resolution.source == "unresolved":
                return None
            profile = self._user_manager.get_user(
                resolution.user_id, group_id or "default"
            )
            return {
                "user_id": resolution.user_id,
                "name": profile.name if profile else resolution.display_name,
                "confidence": resolution.confidence,
                "source": resolution.source,
            }
        except Exception:
            logger.warning("find_by_platform_uid 失败", exc_info=True)
            return None

    def find_by_name(
        self,
        name: str,
        group_id: str = "",
        *,
        fuzzy: bool = True,
    ) -> dict[str, Any] | None:
        """通过显示名或别名查找用户。

        Args:
            name: 用户显示名或别名
            group_id: 群组 ID（可选）
            fuzzy: 是否启用模糊匹配（默认启用）

        Returns:
            用户信息字典，未找到返回 None
            {"user_id": "...", "name": "...", "confidence": 0.9, "source": "alias_exact"}
        """
        if not name:
            return None
        try:
            from sirius_pulse.core.identity_resolver import IdentityContext

            ctx = IdentityContext(speaker_name=name)
            resolution = self._identity_resolver.resolve_with_alias(
                ctx,
                self._user_manager,
                group_id or "default",
                biography_manager=self._biography_manager if fuzzy else None,
            )
            if not resolution.user_id or resolution.source == "unresolved":
                return None
            profile = self._user_manager.get_user(
                resolution.user_id, group_id or "default"
            )
            return {
                "user_id": resolution.user_id,
                "name": profile.name if profile else resolution.display_name,
                "confidence": resolution.confidence,
                "source": resolution.source,
            }
        except Exception:
            logger.warning("find_by_name 失败", exc_info=True)
            return None

    def get_info(self, user_id: str, group_id: str = "") -> dict[str, Any] | None:
        """获取用户详细信息。

        Args:
            user_id: 用户 ID
            group_id: 群组 ID（可选）

        Returns:
            用户详细信息字典，未找到返回 None
        """
        try:
            profile = self._user_manager.get_user(user_id, group_id or "default")
            if profile is None:
                return None
            return {
                "user_id": profile.user_id,
                "name": profile.name,
                "aliases": profile.aliases,
                "identities": profile.identities,
                "is_developer": profile.is_developer,
            }
        except Exception:
            logger.warning("get_info 失败", exc_info=True)
            return None

    def list_users(self, group_id: str = "") -> list[dict[str, Any]]:
        """列出群组中的所有用户。

        Args:
            group_id: 群组 ID（可选）

        Returns:
            用户信息列表
        """
        try:
            users = self._user_manager.list_users(group_id or "default")
            return [
                {
                    "user_id": u.user_id,
                    "name": u.name,
                    "aliases": u.aliases,
                    "is_developer": u.is_developer,
                }
                for u in users
            ]
        except Exception:
            logger.warning("list_users 失败", exc_info=True)
            return []

    def get_self_id(self) -> str:
        """获取 Bot 自身的 user_id。

        Returns:
            Bot 的 user_id，通常是 "assistant"
        """
        return "assistant"

    def get_self_info(self, group_id: str = "") -> dict[str, Any] | None:
        """获取 Bot 自身的详细信息。

        Args:
            group_id: 群组 ID（可选）

        Returns:
            Bot 信息字典，未找到返回 None
        """
        return self.get_info("assistant", group_id)

    def get_bot_platform_uid(self, platform: str = "") -> str | None:
        """获取 Bot 在指定平台的 UID（如 QQ 号）。

        Args:
            platform: 平台标识（如 "qq_native_sirius_pulse"）。
                      为空时返回当前活跃平台的 UID。

        Returns:
            Bot 的平台 UID，未找到返回 None
        """
        if self._engine is None:
            return None
        bot_uids = getattr(self._engine, "_bot_platform_uids", {})
        if not bot_uids:
            return None
        if platform:
            return bot_uids.get(platform)
        # 返回当前活跃平台的 UID
        current_adapter = getattr(self._engine, "_current_adapter_type", "")
        if current_adapter:
            return bot_uids.get(current_adapter)
        # 返回任意一个
        return next(iter(bot_uids.values()), None)

    def get_bot_platform_uids(self) -> dict[str, str]:
        """获取 Bot 在所有平台的 UID。

        Returns:
            {platform: uid} 字典
        """
        if self._engine is None:
            return {}
        return dict(getattr(self._engine, "_bot_platform_uids", {}))
