"""Semantic profile persistence: JSON file store for group/user semantic data."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sirius_pulse.memory.semantic.models import GroupSemanticProfile, UserSemanticProfile
from sirius_pulse.utils.json_io import atomic_write_json

logger = logging.getLogger(__name__)


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    atomic_write_json(path, data)


class SemanticProfileStore:
    """Manages JSON persistence for semantic profiles.

    Layout::

        {base}/
        ├── groups/
        │   └── {group_id}.json
        └── users/
            └── {group_id}/
                └── {user_id}.json
    """

    def __init__(self, base_path: Path | str) -> None:
        self._base = Path(base_path) / "memory" / "semantic"
        self._groups_dir = self._base / "groups"
        self._users_dir = self._base / "users"
        self._groups_dir.mkdir(parents=True, exist_ok=True)
        self._users_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Group profiles
    # ------------------------------------------------------------------

    def load_group_profile(self, group_id: str) -> GroupSemanticProfile | None:
        path = self._groups_dir / f"{self._safe_name(group_id)}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return GroupSemanticProfile.from_dict(data)
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def save_group_profile(self, group_id: str, profile: GroupSemanticProfile) -> None:
        path = self._groups_dir / f"{self._safe_name(group_id)}.json"
        _atomic_write(path, profile.to_dict())

    # ------------------------------------------------------------------
    # User profiles
    # ------------------------------------------------------------------

    def load_user_profile(self, group_id: str, user_id: str) -> UserSemanticProfile | None:
        path = self._users_dir / self._safe_name(group_id) / f"{self._safe_name(user_id)}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return UserSemanticProfile.from_dict(data)
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def save_user_profile(self, group_id: str, user_id: str, profile: UserSemanticProfile) -> None:
        user_dir = self._users_dir / self._safe_name(group_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        path = user_dir / f"{self._safe_name(user_id)}.json"
        _atomic_write(path, profile.to_dict())

    def list_group_user_profiles(self, group_id: str) -> list[UserSemanticProfile]:
        user_dir = self._users_dir / self._safe_name(group_id)
        if not user_dir.exists():
            return []
        profiles: list[UserSemanticProfile] = []
        for path in user_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                profiles.append(UserSemanticProfile.from_dict(data))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        return profiles

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_name(name: str) -> str:
        import re

        base = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", name.strip())
        base = re.sub(r"_+", "_", base).strip("_")
        return base or "default"
