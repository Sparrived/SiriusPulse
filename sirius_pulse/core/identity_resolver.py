"""Identity resolver: decouples framework from platform-specific user identifiers.

增强版解析链（四层）:
  L1: platform_id 精确匹配（confidence=1.0）
  L1.5: Bot 自身检测（confidence=1.0）
  L2: alias_index 精确匹配（confidence=0.9）
  L3: 模糊匹配（confidence=0.7-0.9）
  L4: 上下文推断（confidence=0.6）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from sirius_pulse.memory.user.simple import UserProfile, UserManager

if TYPE_CHECKING:
    from sirius_pulse.memory.biography.manager import BiographyManager

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
    """Resolves IdentityContext into framework UserProfiles without
    hard-coding any platform-specific logic.

    支持两种解析模式:
    - resolve(): 传统模式，返回 UserProfile（向后兼容）
    - resolve_with_alias(): 增强模式，返回 IdentityResolution（含置信度和来源）
    """

    def resolve(
        self,
        ctx: IdentityContext,
        user_manager: UserManager,
        group_id: str,
    ) -> UserProfile:
        """Resolve or create a user profile from identity context.

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
        identities: dict[str, str] = {}
        if ctx.platform and ctx.platform_uid:
            identities[ctx.platform] = ctx.platform_uid

        profile = UserProfile(
            user_id=ctx.user_id or ctx.speaker_name,
            name=ctx.speaker_name,
            identities=identities,
            metadata={"is_developer": ctx.is_developer},
        )
        user_manager.register_user(profile, group_id=group_id)
        return profile

    def resolve_with_alias(
        self,
        ctx: IdentityContext,
        user_manager: UserManager,
        group_id: str,
        biography_manager: BiographyManager | None = None,
        recent_speakers: list[str] | None = None,
    ) -> IdentityResolution:
        """增强版解析，整合 UserManager + BiographyManager 的能力。

        解析链:
        L1: platform_id 精确匹配（confidence=1.0）
        L1.5: Bot 自身检测（confidence=1.0）
        L2: alias_index 精确匹配（confidence=0.9）
        L3: 模糊匹配（confidence=0.7-0.9）
        L4: 上下文推断（confidence=0.6）

        Args:
            ctx: 身份上下文
            user_manager: 用户管理器
            group_id: 群组 ID
            biography_manager: 传记管理器（可选，用于别名消歧）
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

        # L1.5: Bot 自身检测（通过 platform_uid 或 speaker_name）
        if self._is_bot_identity(ctx, user_manager):
            return IdentityResolution(
                user_id="assistant",
                confidence=1.0,
                source="bot_identity",
                display_name=speaker,
            )

        # L2: alias_index 精确匹配（通过 BiographyManager）
        if biography_manager and speaker:
            alias_result = biography_manager.resolve_alias(
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
        if biography_manager and speaker:
            fuzzy_result = self._fuzzy_match(
                speaker, group_id, user_manager, biography_manager
            )
            if fuzzy_result:
                return fuzzy_result

        # L4: 上下文推断（基于最近发言者）
        if recent_speakers and speaker:
            context_result = self._context_inference(
                speaker, recent_speakers, group_id, user_manager
            )
            if context_result:
                return context_result

        # Fallback: 创建新用户
        identities: dict[str, str] = {}
        if ctx.platform and ctx.platform_uid:
            identities[ctx.platform] = ctx.platform_uid

        profile = UserProfile(
            user_id=ctx.user_id or speaker,
            name=speaker,
            identities=identities,
            metadata={"is_developer": ctx.is_developer},
        )
        user_manager.register_user(profile, group_id=group_id)
        return IdentityResolution(
            user_id=profile.user_id,
            confidence=0.0,
            source="unresolved",
            display_name=speaker,
        )

    def _is_bot_identity(self, ctx: IdentityContext, user_manager: UserManager) -> bool:
        """检测是否为 Bot 自身身份。

        通过检查 platform_uid 或 speaker_name 是否匹配已知的 bot 标识。
        """
        # 检查 platform_uid 是否为 bot（通常是 bot 自己的 UID）
        if ctx.platform_uid and ctx.platform:
            # Bot 通常没有 platform_uid，或者 UID 是特殊的
            pass

        # 检查 speaker_name 是否为 "assistant" 或其他 bot 标识
        bot_names = {"assistant", "bot", "机器人"}
        if ctx.speaker_name and ctx.speaker_name.strip().lower() in bot_names:
            return True

        return False

    def _fuzzy_match(
        self,
        speaker: str,
        group_id: str,
        user_manager: UserManager,
        biography_manager: BiographyManager,
    ) -> IdentityResolution | None:
        """模糊匹配：基于编辑距离或包含关系。

        匹配策略:
        - 编辑距离相似度 > 0.7
        - 包含关系（speaker 包含已知别名，或已知别名包含 speaker）
        """
        speaker_lower = speaker.strip().lower()
        if len(speaker_lower) < 2:
            return None

        # 获取当前群的所有已知用户
        group = user_manager._ensure_group(group_id)
        if not group:
            return None

        best_match: str | None = None
        best_score: float = 0.0

        for user_id, profile in group.items():
            # 检查用户名
            if profile.name:
                score = self._compute_similarity(speaker_lower, profile.name.lower())
                if score > best_score:
                    best_score = score
                    best_match = user_id

            # 检查别名
            for alias in profile.aliases:
                if alias:
                    score = self._compute_similarity(speaker_lower, alias.lower())
                    if score > best_score:
                        best_score = score
                        best_match = user_id

        # 也检查 BiographyManager 的别名索引
        if biography_manager:
            for alias_key in biography_manager._alias_index.keys():
                score = self._compute_similarity(speaker_lower, alias_key)
                if score > best_score:
                    # 找到最佳匹配的用户
                    entries = biography_manager._alias_index[alias_key]
                    if entries:
                        best_score = score
                        best_match = entries[0].user_id

        # 阈值：相似度 > 0.7 才认为是有效匹配
        if best_match and best_score > 0.7:
            # 置信度：基于相似度，但不超过 0.9
            confidence = min(0.9, best_score * 0.8)
            return IdentityResolution(
                user_id=best_match,
                confidence=confidence,
                source="alias_fuzzy",
                display_name=speaker,
            )

        return None

    def _compute_similarity(self, s1: str, s2: str) -> float:
        """计算两个字符串的相似度（基于 SequenceMatcher）。

        Returns:
            0.0~1.0 的相似度分数
        """
        if not s1 or not s2:
            return 0.0

        # 完全匹配
        if s1 == s2:
            return 1.0

        # 包含关系
        if s1 in s2 or s2 in s1:
            # 短字符串包含在长字符串中
            shorter = min(len(s1), len(s2))
            longer = max(len(s1), len(s2))
            if shorter >= 2:  # 至少 2 字符才认为有效
                return 0.8 + (shorter / longer) * 0.2

        # 编辑距离相似度
        return SequenceMatcher(None, s1, s2).ratio()

    def _context_inference(
        self,
        speaker: str,
        recent_speakers: list[str],
        group_id: str,
        user_manager: UserManager,
    ) -> IdentityResolution | None:
        """上下文推断：基于最近发言者推断身份。

        策略：如果 speaker 无法精确匹配，但最近有活跃用户，尝试推断。
        """
        if not recent_speakers:
            return None

        # 获取最近活跃的用户（排除重复）
        seen = set()
        for recent_user_id in recent_speakers:
            if recent_user_id in seen:
                continue
            seen.add(recent_user_id)

            # 检查这个用户是否存在
            group = user_manager._ensure_group(group_id)
            if recent_user_id in group:
                profile = group[recent_user_id]
                # 检查 speaker 是否与该用户的任何标识匹配
                if speaker.lower() in [profile.name.lower()] + [a.lower() for a in profile.aliases]:
                    return IdentityResolution(
                        user_id=recent_user_id,
                        confidence=0.6,
                        source="context_inference",
                        display_name=speaker,
                    )

        return None
