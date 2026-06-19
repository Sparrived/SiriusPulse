"""Identity resolver: decouples framework from platform-specific user identifiers.

增强版解析链（四层）:
  L1: platform_id 精确匹配（confidence=1.0）
  L1.5: Bot 自身检测（confidence=1.0）
  L2: 已确认别名精确匹配（confidence 来自别名记录）
  L3: 模糊匹配（confidence=0.7-0.9）
  L4: 上下文推断（confidence=0.6）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sirius_pulse.memory.user.unified_manager import UnifiedUserManager
    from sirius_pulse.memory.user.unified_models import UnifiedUser

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IdentityContext:
    """Platform-agnostic identity context provided by external callers (plugins).

    Attributes:
        speaker_name: Human-readable display name.
        user_id: Framework-unified user ID (if already bound).
        platform_uid: Platform-native UID (e.g. QQ number, Discord ID).
        platform: Platform identifier (e.g. "qq", "discord", "wechat").
        is_developer: Whether this user has developer privileges.
    """

    speaker_name: str
    user_id: str | None = None
    platform_uid: str | None = None
    platform: str | None = None
    is_developer: bool = False


@dataclass(slots=True)
class IdentityResolution:
    """身份解析结果，包含置信度和来源信息。

    Attributes:
        user_id: 解析到的用户 ID
        confidence: 置信度 (0.0~1.0)
        source: 解析来源 (platform_id / bot_identity / alias_exact / alias_fuzzy / context_inference / unresolved)
        display_name: 显示名称
    """

    user_id: str
    confidence: float = 0.0
    source: str = "unresolved"
    display_name: str = ""


class IdentityResolver:
    """Resolves IdentityContext into framework UnifiedUsers without
    hard-coding any platform-specific logic.

    使用 UnifiedUserManager 统一管理用户身份和别名。
    """

    def resolve(
        self,
        ctx: IdentityContext,
        user_manager: UnifiedUserManager,
        group_id: str,
    ) -> UnifiedUser:
        """Resolve or create a user from identity context.

        Lookup order:
        1. Framework user_id (if provided)
        2. (platform, platform_uid) pair
        3. speaker_name
        4. Create new user if none found
        """
        group = user_manager._ensure_group(group_id)

        # 1. Exact user_id match
        if ctx.user_id and ctx.user_id in group:
            return group[ctx.user_id]

        # 2. Platform identity match
        if ctx.platform and ctx.platform_uid:
            resolved = user_manager.resolve_user_id(
                platform=ctx.platform,
                external_uid=ctx.platform_uid,
            )
            if resolved and resolved in group:
                return group[resolved]

        # 3. Speaker name match
        resolved = user_manager.resolve_user_id(speaker=ctx.speaker_name)
        if resolved and resolved in group:
            return group[resolved]

        # 4. Create new user
        from sirius_pulse.memory.user.unified_models import UnifiedUser

        identities: dict[str, str] = {}
        if ctx.platform and ctx.platform_uid:
            identities[ctx.platform] = ctx.platform_uid

        user = UnifiedUser(
            user_id=ctx.user_id or ctx.speaker_name,
            name=ctx.speaker_name,
            identities=identities,
            metadata={"is_developer": ctx.is_developer},
        )
        user_manager.register_user(user, group_id=group_id)
        return user

    def resolve_with_alias(
        self,
        ctx: IdentityContext,
        user_manager: UnifiedUserManager,
        group_id: str,
        recent_speakers: list[str] | None = None,
    ) -> IdentityResolution:
        """增强版解析，整合 UnifiedUserManager 的能力。

        解析链:
        L1: platform_id 精确匹配（confidence=1.0）
        L1.5: Bot 自身检测（confidence=1.0）
        L2: 已确认别名精确匹配（confidence 来自别名记录）
        L3: 模糊匹配（confidence=0.7-0.9）
        L4: 上下文推断（confidence=0.6）

        Args:
            ctx: 身份上下文
            user_manager: 统一用户管理器
            group_id: 群组 ID
            recent_speakers: 最近发言者列表（用于上下文推断）

        Returns:
            IdentityResolution 包含解析结果、置信度和来源
        """
        speaker = ctx.speaker_name.strip() if ctx.speaker_name else ""

        # L1: platform_id 精确匹配
        if ctx.platform and ctx.platform_uid:
            resolved = user_manager.resolve_user_id(
                platform=ctx.platform,
                external_uid=ctx.platform_uid,
            )
            if resolved:
                group = user_manager._ensure_group(group_id)
                if resolved in group:
                    return IdentityResolution(
                        user_id=resolved,
                        confidence=1.0,
                        source="platform_id",
                        display_name=speaker,
                    )

        # L1.5: Bot 自身检测
        if self._is_bot_identity(ctx):
            return IdentityResolution(
                user_id="assistant",
                confidence=1.0,
                source="bot_identity",
                display_name=speaker,
            )

        # L2: 已确认别名精确匹配
        if speaker:
            alias_result = user_manager.resolve_alias(
                speaker,
                group_id=group_id,
                recent_speakers=recent_speakers,
            )
            if alias_result and alias_result[0] and alias_result[1] > 0.5:
                return IdentityResolution(
                    user_id=alias_result[0],
                    confidence=alias_result[1],
                    source="alias_exact",
                    display_name=speaker,
                )

        # L3: 模糊匹配
        if speaker:
            fuzzy_result = self._fuzzy_match(speaker, group_id, user_manager)
            if fuzzy_result:
                return fuzzy_result

        # L4: 上下文推断
        if recent_speakers and speaker:
            context_result = self._context_inference(
                speaker, recent_speakers, group_id, user_manager
            )
            if context_result:
                return context_result

        # Fallback: 创建新用户
        from sirius_pulse.memory.user.unified_models import UnifiedUser

        identities: dict[str, str] = {}
        if ctx.platform and ctx.platform_uid:
            identities[ctx.platform] = ctx.platform_uid

        user = UnifiedUser(
            user_id=ctx.user_id or speaker,
            name=speaker,
            identities=identities,
            metadata={"is_developer": ctx.is_developer},
        )
        user_manager.register_user(user, group_id=group_id)
        return IdentityResolution(
            user_id=user.user_id,
            confidence=0.0,
            source="unresolved",
            display_name=speaker,
        )

    def _is_bot_identity(self, ctx: IdentityContext) -> bool:
        """检测是否为 Bot 自身身份。"""
        bot_names = {"assistant", "bot", "机器人"}
        if ctx.speaker_name and ctx.speaker_name.strip().lower() in bot_names:
            return True
        return False

    def _fuzzy_match(
        self,
        speaker: str,
        group_id: str,
        user_manager: UnifiedUserManager,
    ) -> IdentityResolution | None:
        """模糊匹配：基于编辑距离或包含关系。"""
        speaker_lower = speaker.strip().lower()
        if len(speaker_lower) < 2:
            return None

        group = user_manager._ensure_group(group_id)
        if not group:
            return None

        best_match: str | None = None
        best_score: float = 0.0

        for user_id, user in group.items():
            if user.name:
                score = self._compute_similarity(speaker_lower, user.name.lower())
                if score > best_score:
                    best_score = score
                    best_match = user_id

        # 检查别名索引
        for alias_key in user_manager._alias_index.keys():
            score = self._compute_similarity(speaker_lower, alias_key)
            if score > best_score:
                entries = user_manager._alias_index[alias_key]
                if entries:
                    best_score = score
                    best_match = entries[0].user_id

        if best_match and best_score > 0.7:
            confidence = min(0.9, best_score * 0.8)
            return IdentityResolution(
                user_id=best_match,
                confidence=confidence,
                source="alias_fuzzy",
                display_name=speaker,
            )

        return None

    def _compute_similarity(self, s1: str, s2: str) -> float:
        """计算两个字符串的相似度。"""
        if not s1 or not s2:
            return 0.0

        if s1 == s2:
            return 1.0

        if s1 in s2 or s2 in s1:
            shorter = min(len(s1), len(s2))
            longer = max(len(s1), len(s2))
            if shorter >= 2:
                return 0.8 + (shorter / longer) * 0.2

        return SequenceMatcher(None, s1, s2).ratio()

    def _context_inference(
        self,
        speaker: str,
        recent_speakers: list[str],
        group_id: str,
        user_manager: UnifiedUserManager,
    ) -> IdentityResolution | None:
        """上下文推断：基于最近发言者推断身份。"""
        if not recent_speakers:
            return None

        seen = set()
        for recent_user_id in recent_speakers:
            if recent_user_id in seen:
                continue
            seen.add(recent_user_id)

            group = user_manager._ensure_group(group_id)
            if recent_user_id in group:
                user = group[recent_user_id]
                if speaker.lower() == user.name.lower():
                    return IdentityResolution(
                        user_id=recent_user_id,
                        confidence=0.6,
                        source="context_inference",
                        display_name=speaker,
                    )

        return None
