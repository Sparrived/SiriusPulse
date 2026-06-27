"""统一用户管理器。

职责：
- 用户注册、解析、群隔离

存储：SQLite（懒加载 + 写穿缓存）
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.memory.storage import MemoryStorage
from sirius_pulse.memory.user.unified_models import (
    RelationshipAnchor,
    UnifiedUser,
)

logger = logging.getLogger(__name__)


class UnifiedUserManager:
    """统一用户管理器。

    功能：
    - 用户注册和解析

    存储策略：懒加载 + 写穿缓存
    - 启动时不加载数据
    - 查询时按需从 SQLite 读取并缓存
    - 写入时同时更新缓存和 SQLite
    """

    def __init__(
        self,
        work_path: Path | str | None = None,
        persona_name: str = "",
        persona_aliases: list[str] | None = None,
        db_path: Path | str | None = None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self._work_path = Path(work_path) if work_path else None

        # 人格身份（用于过滤 bot 名称不被注册为用户别名）
        self._persona_name = persona_name.strip().lower()
        self._persona_aliases = {a.strip().lower() for a in (persona_aliases or []) if a.strip()}

        # SQLite 存储：优先使用共享连接
        if conn is not None:
            self._storage = MemoryStorage(conn=conn)
        elif db_path:
            self._storage = MemoryStorage(db_path)
        elif self._work_path:
            self._storage = MemoryStorage(self._work_path / "memory.db")
        else:
            self._storage = MemoryStorage(Path(":memory:"))

        # 内存缓存（懒加载）
        self.entries: dict[str, dict[str, UnifiedUser]] = {}
        self._global_users: dict[str, UnifiedUser] = {}
        self._speaker_index: dict[str, str] = {}
        self._identity_index: dict[str, str] = {}

        # 懒加载标记
        self._users_loaded = False


    # ── 懒加载 ─────────────────────────────────────────

    def _ensure_users_loaded(self) -> None:
        """确保用户数据已加载。"""
        if self._users_loaded:
            return

        users_data = self._storage.list_users()
        logger.info("从 SQLite 加载用户: %d 个", len(users_data))
        for user_data in users_data:
            user = UnifiedUser.from_dict(user_data)
            self._global_users[user.user_id] = user
            self._update_indices(user)

        # 加载群组成员关系
        for user_id in self._global_users:
            groups = self._storage.get_user_groups(user_id)
            for group_id in groups:
                if group_id not in self.entries:
                    self.entries[group_id] = {}
                self.entries[group_id][user_id] = self._global_users[user_id]

        self._users_loaded = True

    def reload(self) -> None:
        """强制从 SQLite 重新加载所有数据。"""
        self._global_users.clear()
        self.entries.clear()
        self._speaker_index.clear()
        self._identity_index.clear()
        self._users_loaded = False

    def close(self) -> None:
        """关闭存储连接。"""
        self._storage.close()

    # ── 写穿缓存 ─────────────────────────────────────────

    def _save_user_to_storage(self, user: UnifiedUser) -> None:
        """保存用户到 SQLite。"""
        self._storage.save_user(user.to_dict())
        for platform, platform_uid in user.identities.items():
            if platform and platform_uid:
                self._storage.save_identity(platform, platform_uid, user.user_id)

    def save_to_disk(self) -> None:
        """保存所有缓存数据到 SQLite。"""
        for user in self._global_users.values():
            self._save_user_to_storage(user)

        for group_id, group in self.entries.items():
            for user_id in group:
                self._storage.add_group_member(group_id, user_id)

    # ── 内部工具 ─────────────────────────────────────────

    @staticmethod
    def _normalize(label: str) -> str:
        return label.strip().lower()

    @staticmethod
    def _identity_key(platform: str, external_uid: str) -> str:
        return f"{platform.strip().lower()}:{external_uid.strip().lower()}"

    def _ensure_group(self, group_id: str) -> dict[str, UnifiedUser]:
        if group_id not in self.entries:
            self.entries[group_id] = {}
        return self.entries[group_id]

    def _update_indices(self, user: UnifiedUser) -> None:
        """更新索引。"""
        for label in (user.name, user.user_id):
            if label:
                self._speaker_index[self._normalize(label)] = user.user_id
        for platform, external_uid in user.identities.items():
            if platform and external_uid:
                self._identity_index[self._identity_key(platform, external_uid)] = user.user_id

    def _sync_to_global(self, user: UnifiedUser) -> None:
        """同步到全局用户缓存。"""
        uid = user.user_id
        if not uid:
            return

        global_user = self._global_users.get(uid)
        if global_user is None:
            self._global_users[uid] = replace(
                user,
                identities=dict(user.identities),
                metadata=dict(user.metadata),
            )
            return

        # 合并
        for platform, external_uid in user.identities.items():
            if platform and external_uid:
                global_user.identities[platform] = external_uid
        global_user.metadata.update(user.metadata)
        if user.name and not global_user.name:
            global_user.name = user.name

    def _seed_from_global(self, user_id: str, group_id: str) -> UnifiedUser | None:
        """从全局用户缓存种子到群组。"""
        self._ensure_users_loaded()

        global_user = self._global_users.get(user_id)
        if global_user is None:
            return None

        local = replace(
            global_user,
            identities=dict(global_user.identities),
            metadata=dict(global_user.metadata),
        )
        group = self._ensure_group(group_id)
        group[user_id] = local
        self._update_indices(local)
        return local

    # ── 公共 API：用户管理 ──────────────────────────────────

    def register_user(self, user: UnifiedUser, group_id: str = "default") -> None:
        """注册或更新用户。"""
        self._ensure_users_loaded()

        if not user.user_id:
            user.user_id = user.name or "unknown"

        uid = user.user_id
        group = self._ensure_group(group_id)
        existing = group.get(uid)

        if existing is None:
            seeded = self._seed_from_global(uid, group_id)
            if seeded is not None:
                existing = seeded
            else:
                group[uid] = user
                existing = user

        # 合并
        if user.name and (not existing.name or existing.name == uid):
            existing.name = user.name
        for platform, external_uid in user.identities.items():
            if platform and external_uid:
                existing.identities[platform] = external_uid
        existing.metadata.update(user.metadata)

        self._update_indices(existing)
        self._sync_to_global(existing)

        # 写穿到 SQLite
        self._save_user_to_storage(existing)
        self._storage.add_group_member(group_id, existing.user_id)

    def resolve_user_id(
        self,
        *,
        speaker: str | None = None,
        platform: str | None = None,
        external_uid: str | None = None,
    ) -> str | None:
        """解析用户 ID。"""
        self._ensure_users_loaded()

        if platform and external_uid:
            resolved = self._identity_index.get(self._identity_key(platform, external_uid))
            if resolved:
                return resolved
            # 回退到 SQLite
            resolved = self._storage.get_user_by_identity(platform, external_uid)
            if resolved:
                self._identity_index[self._identity_key(platform, external_uid)] = resolved
                return resolved
        if speaker:
            return self._speaker_index.get(self._normalize(speaker))
        return None

    def get_user(self, user_id: str, group_id: str = "default") -> UnifiedUser | None:
        """获取用户。"""
        self._ensure_users_loaded()

        group = self._ensure_group(group_id)
        local = group.get(user_id)
        if local is not None:
            return local
        return self._seed_from_global(user_id, group_id)

    def list_users(self, group_id: str = "default") -> list[UnifiedUser]:
        """列出群组中的所有用户。"""
        self._ensure_users_loaded()
        return list(self._ensure_group(group_id).values())

    def get_global_user(self, user_id: str) -> UnifiedUser | None:
        """获取全局用户。"""
        self._ensure_users_loaded()
        return self._global_users.get(user_id)

    def list_global_users(self) -> list[UnifiedUser]:
        """列出所有全局用户。"""
        self._ensure_users_loaded()
        return list(self._global_users.values())

    # ── 公共 API：别名管理 ──────────────────────────────────

    # ── 公共 API：传记管理 ──────────────────────────────────

    def get_or_create_user(self, user_id: str, name: str = "") -> UnifiedUser:
        """获取或创建用户。"""
        self._ensure_users_loaded()

        user = self._global_users.get(user_id)
        if user is None:
            user = UnifiedUser(user_id=user_id, name=name or user_id)
            self._global_users[user_id] = user
            # 写穿到 SQLite
            self._save_user_to_storage(user)
        if name and not user.name:
            user.name = name
        return user

    def save_user(self, user: UnifiedUser) -> None:
        """保存用户数据。"""
        self._global_users[user.user_id] = user
        self._save_user_to_storage(user)


__all__ = ["UnifiedUserManager"]
