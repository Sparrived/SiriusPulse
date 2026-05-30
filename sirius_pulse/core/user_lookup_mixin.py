"""用户查找公共 Mixin —— 供 Plugin、Skill、WebUI 等模块共用。

提供统一的用户查找 API，基于 IdentityResolver 实现。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class UserLookupMixin:
    """用户查找能力 Mixin。

    使用前提：混入类需要提供以下属性：
    - _engine: 引擎实例（包含 identity_resolver 和 user_manager）
    """

    _engine: Any

    def find_user_by_platform_uid(
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
        if self._engine is None:
            return None
        try:
            from sirius_pulse.core.identity_resolver import IdentityContext

            ctx = IdentityContext(
                speaker_name="",
                platform_uid=platform_uid,
                platform=platform,
            )
            resolution = self._engine.identity_resolver.resolve_with_alias(
                ctx,
                self._engine.user_manager,
                group_id or "default",
            )
            if not resolution.user_id or resolution.source == "unresolved":
                return None
            profile = self._engine.user_manager.get_user(
                resolution.user_id, group_id or "default"
            )
            return {
                "user_id": resolution.user_id,
                "name": profile.name if profile else resolution.display_name,
                "confidence": resolution.confidence,
                "source": resolution.source,
            }
        except Exception:
            logger.warning("find_user_by_platform_uid 失败", exc_info=True)
            return None

    def find_user_by_name(
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
        if self._engine is None or not name:
            return None
        try:
            from sirius_pulse.core.identity_resolver import IdentityContext

            ctx = IdentityContext(speaker_name=name)
            biography_manager = getattr(self._engine, "biography_manager", None)
            resolution = self._engine.identity_resolver.resolve_with_alias(
                ctx,
                self._engine.user_manager,
                group_id or "default",
                biography_manager=biography_manager if fuzzy else None,
            )
            if not resolution.user_id or resolution.source == "unresolved":
                return None
            profile = self._engine.user_manager.get_user(
                resolution.user_id, group_id or "default"
            )
            return {
                "user_id": resolution.user_id,
                "name": profile.name if profile else resolution.display_name,
                "confidence": resolution.confidence,
                "source": resolution.source,
            }
        except Exception:
            logger.warning("find_user_by_name 失败", exc_info=True)
            return None

    def get_user_info(self, user_id: str, group_id: str = "") -> dict[str, Any] | None:
        """获取用户详细信息。

        Args:
            user_id: 用户 ID
            group_id: 群组 ID（可选）

        Returns:
            用户详细信息字典，未找到返回 None
        """
        if self._engine is None:
            return None
        try:
            profile = self._engine.user_manager.get_user(
                user_id, group_id or "default"
            )
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
            logger.warning("get_user_info 失败", exc_info=True)
            return None

    def list_users(self, group_id: str = "") -> list[dict[str, Any]]:
        """列出群组中的所有用户。

        Args:
            group_id: 群组 ID（可选）

        Returns:
            用户信息列表
        """
        if self._engine is None:
            return []
        try:
            users = self._engine.user_manager.list_users(group_id or "default")
            return [
                {
                    "user_id": u.user_id,
                    "name": u.name,
                    "aliases": u.aliases,
                    "identities": u.identities,
                    "is_developer": u.is_developer,
                }
                for u in users
            ]
        except Exception:
            logger.warning("list_users 失败", exc_info=True)
            return []