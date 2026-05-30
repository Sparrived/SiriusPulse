"""Simplified user data models and manager (v2 refactor)."""

from __future__ import annotations

from typing import Any

from sirius_pulse.memory.user.models import UserProfile


class UserManager:
    """Manages user profiles with group-isolated storage and cross-group awareness.

    Structure:
        - entries: {group_id: {user_id: UserProfile}}  (group-local)
        - _global_users: {user_id: UserProfile}         (cross-group shared)

    When a user appears in a new group for the first time, their global profile
    (name, aliases, identities, metadata) is automatically copied to the group.
    Group-local updates do not flow back to global unless explicitly merged.
    """

    def __init__(self) -> None:
        self.entries: dict[str, dict[str, UserProfile]] = {}
        self._global_users: dict[str, UserProfile] = {}
        self._speaker_index: dict[str, str] = {}
        self._identity_index: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(label: str) -> str:
        return label.strip().lower()

    @staticmethod
    def _identity_key(platform: str, external_uid: str) -> str:
        return f"{platform.strip().lower()}:{external_uid.strip().lower()}"

    def _ensure_group(self, group_id: str) -> dict[str, UserProfile]:
        if group_id not in self.entries:
            self.entries[group_id] = {}
        return self.entries[group_id]

    def _update_indices(self, profile: UserProfile) -> None:
        for label in (profile.name, profile.user_id, *profile.aliases):
            if label:
                self._speaker_index[self._normalize(label)] = profile.user_id
        for platform, external_uid in profile.identities.items():
            if platform and external_uid:
                self._identity_index[self._identity_key(platform, external_uid)] = profile.user_id

    def _sync_to_global(self, profile: UserProfile) -> None:
        """Merge a group-local profile into the global shared profile."""
        uid = profile.user_id
        if not uid:
            return
        global_profile = self._global_users.get(uid)
        if global_profile is None:
            # Deep copy to avoid mutating the local reference directly
            from dataclasses import replace
            self._global_users[uid] = replace(
                profile,
                aliases=list(profile.aliases),
                identities=dict(profile.identities),
                metadata=dict(profile.metadata),
            )
            return

        # Merge aliases
        for alias in profile.aliases:
            if alias not in global_profile.aliases:
                global_profile.aliases.append(alias)
        # Merge identities
        for platform, external_uid in profile.identities.items():
            if platform and external_uid:
                global_profile.identities[platform] = external_uid
        # Merge metadata (local wins on conflict for simple types)
        global_profile.metadata.update(profile.metadata)
        # Update name if global is empty
        if profile.name and not global_profile.name:
            global_profile.name = profile.name

    def _seed_from_global(self, user_id: str, group_id: str) -> UserProfile | None:
        """If a global profile exists but the group-local one does not,
        copy the global profile into the group and return it."""
        global_profile = self._global_users.get(user_id)
        if global_profile is None:
            return None
        from dataclasses import replace
        local = replace(
            global_profile,
            aliases=list(global_profile.aliases),
            identities=dict(global_profile.identities),
            metadata=dict(global_profile.metadata),
        )
        group = self._ensure_group(group_id)
        group[user_id] = local
        self._update_indices(local)
        return local

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_user(self, profile: UserProfile, group_id: str = "default") -> None:
        """Register or update a user in a group, and sync to global profile."""
        if not profile.user_id:
            profile.user_id = profile.name or "unknown"

        uid = profile.user_id
        group = self._ensure_group(group_id)
        existing = group.get(uid)

        if existing is None:
            # Try seed from global first
            seeded = self._seed_from_global(uid, group_id)
            if seeded is not None:
                existing = seeded
            else:
                group[uid] = profile
                existing = profile

        # Merge incoming data into local profile
        if profile.name and (not existing.name or existing.name == uid):
            existing.name = profile.name
        for alias in profile.aliases:
            if alias not in existing.aliases:
                existing.aliases.append(alias)
        for platform, external_uid in profile.identities.items():
            if platform and external_uid:
                existing.identities[platform] = external_uid
        existing.metadata.update(profile.metadata)

        self._update_indices(existing)
        self._sync_to_global(existing)

    def resolve_user_id(
        self,
        *,
        speaker: str | None = None,
        platform: str | None = None,
        external_uid: str | None = None,
    ) -> str | None:
        """Resolve user ID from speaker name, platform identity, or external UID."""
        if platform and external_uid:
            resolved = self._identity_index.get(self._identity_key(platform, external_uid))
            if resolved:
                return resolved
        if speaker:
            return self._speaker_index.get(self._normalize(speaker))
        return None

    def get_user(self, user_id: str, group_id: str = "default") -> UserProfile | None:
        """Get user profile by exact ID within a group.
        Falls back to global profile if not present locally."""
        group = self._ensure_group(group_id)
        local = group.get(user_id)
        if local is not None:
            return local
        return self._seed_from_global(user_id, group_id)

    def list_users(self, group_id: str = "default") -> list[UserProfile]:
        """List all users in a group."""
        return list(self._ensure_group(group_id).values())

    def get_global_user(self, user_id: str) -> UserProfile | None:
        """Get the cross-group shared profile for a user."""
        return self._global_users.get(user_id)

    def list_global_users(self) -> list[UserProfile]:
        """List all users known across any group."""
        return list(self._global_users.values())

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict. Includes both group-local and global profiles."""
        return {
            "entries": {
                gid: {uid: p.to_dict() for uid, p in group.items()}
                for gid, group in self.entries.items()
            },
            "global": {
                uid: p.to_dict() for uid, p in self._global_users.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserManager":
        """Deserialize from dict."""
        mgr = cls()
        # Load global first so group seeding can reference it
        global_data = data.get("global", {})
        for uid, payload in global_data.items():
            if isinstance(payload, dict):
                profile = UserProfile.from_dict(payload)
                mgr._global_users[uid] = profile
                mgr._update_indices(profile)

        entries_data = data.get("entries", {})
        for gid, group in entries_data.items():
            for uid, payload in group.items():
                if not isinstance(payload, dict):
                    continue
                profile = UserProfile.from_dict(payload)
                # Seed from global if available, otherwise use local
                seeded = mgr._seed_from_global(profile.user_id, gid)
                if seeded is not None:
                    # Merge local-specific overrides on top of global seed
                    if profile.name and (not seeded.name or seeded.name == profile.user_id):
                        seeded.name = profile.name
                    for alias in profile.aliases:
                        if alias not in seeded.aliases:
                            seeded.aliases.append(alias)
                    for platform, external_uid in profile.identities.items():
                        if platform and external_uid:
                            seeded.identities[platform] = external_uid
                    seeded.metadata.update(profile.metadata)
                else:
                    mgr.entries[gid][profile.user_id] = profile
                    mgr._update_indices(profile)
        return mgr
