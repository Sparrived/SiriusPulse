"""Identity resolver: decouples framework from platform-specific user identifiers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sirius_pulse.memory.user.simple import UserProfile, UserManager


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


class IdentityResolver:
    """Resolves IdentityContext into framework UserProfiles without
    hard-coding any platform-specific logic.
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
        profile = UserProfile(
            user_id=ctx.user_id or ctx.speaker_name,
            name=ctx.speaker_name,
            identities={
                ctx.platform: ctx.platform_uid
                for k, v in [(ctx.platform, ctx.platform_uid)]
                if k and v
            },
            metadata={"is_developer": ctx.is_developer},
        )
        user_manager.register_user(profile, group_id=group_id)
        return profile
