"""统一用户管理器 — 合并 UserManager 和 BiographyManager。

职责：
- 用户注册、解析、群隔离
- 别名索引和消歧
- 传记蒸馏和更新
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.memory.user.unified_models import (
    AliasEntry,
    RelationshipAnchor,
    UnifiedUser,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_since(iso_dt: str, default: float = 30.0) -> float:
    """计算从 iso 时间到现在过去了多少天。"""
    if not iso_dt:
        return default
    try:
        dt = datetime.fromisoformat(iso_dt.replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
    except (ValueError, TypeError):
        logger.warning("解析 ISO 时间失败", exc_info=True)
        return default


class UnifiedUserManager:
    """统一用户管理器。

    合并了 UserManager 和 BiographyManager 的功能：
    - 用户注册和解析（原 UserManager）
    - 别名索引和消歧（原 BiographyManager）
    - 传记蒸馏和更新（原 BiographyManager）
    """

    def __init__(
        self,
        work_path: Path | str | None = None,
        persona_name: str = "",
        persona_aliases: list[str] | None = None,
    ) -> None:
        # 工作路径（用于持久化）
        self._work_path = Path(work_path) if work_path else None

        # 人格身份（用于过滤 bot 名称不被注册为用户别名）
        self._persona_name = persona_name.strip().lower()
        self._persona_aliases = {a.strip().lower() for a in (persona_aliases or []) if a.strip()}

        # 用户存储：{user_id: UnifiedUser}
        self._users: dict[str, UnifiedUser] = {}

        # 群隔离存储：{group_id: {user_id: UnifiedUser}}
        self.entries: dict[str, dict[str, UnifiedUser]] = {}

        # 全局用户（跨群共享）
        self._global_users: dict[str, UnifiedUser] = {}

        # 索引
        self._speaker_index: dict[str, str] = {}  # normalized_name → user_id
        self._identity_index: dict[str, str] = {}  # "platform:uid" → user_id
        self._alias_index: dict[str, list[AliasEntry]] = {}  # alias → [AliasEntry]

        # 加载持久化数据
        if self._work_path:
            self._load_from_disk()

        # 启动时清理
        self._cleanup_on_startup()

    # ── 持久化 ─────────────────────────────────────────────

    def _load_from_disk(self) -> None:
        """从磁盘加载数据。"""
        if not self._work_path:
            return

        # 加载 user_manager.json（旧格式兼容）
        user_manager_path = self._work_path / "user_manager.json"
        if user_manager_path.exists():
            try:
                data = json.loads(user_manager_path.read_text(encoding="utf-8"))
                self._load_user_manager_data(data)
            except Exception as exc:
                logger.warning("加载 user_manager.json 失败: %s", exc)

        # 加载 alias_index.json
        alias_index_path = self._work_path / "alias_index.json"
        if alias_index_path.exists():
            try:
                data = json.loads(alias_index_path.read_text(encoding="utf-8"))
                self._load_alias_index(data)
            except Exception as exc:
                logger.warning("加载 alias_index.json 失败: %s", exc)

        # 加载传记卡（biography/*.json）
        biography_dir = self._work_path / "biography"
        if biography_dir.is_dir():
            for card_file in biography_dir.glob("*.json"):
                try:
                    data = json.loads(card_file.read_text(encoding="utf-8"))
                    user_id = data.get("user_id", "")
                    if user_id and user_id in self._users:
                        self._merge_biography_data(user_id, data)
                except Exception as exc:
                    logger.warning("加载传记卡 %s 失败: %s", card_file.name, exc)

    def _load_user_manager_data(self, data: dict[str, Any]) -> None:
        """加载 UserManager 格式的数据。"""
        # 加载全局用户
        global_data = data.get("global", {})
        for uid, payload in global_data.items():
            if isinstance(payload, dict):
                user = self._create_user_from_legacy(payload)
                self._global_users[uid] = user
                self._update_indices(user)

        # 加载群组用户
        entries_data = data.get("entries", {})
        for gid, group in entries_data.items():
            if gid not in self.entries:
                self.entries[gid] = {}
            for uid, payload in group.items():
                if not isinstance(payload, dict):
                    continue
                user = self._create_user_from_legacy(payload)
                self.entries[gid][uid] = user
                self._update_indices(user)

    def _create_user_from_legacy(self, data: dict[str, Any]) -> UnifiedUser:
        """从旧格式创建 UnifiedUser。"""
        return UnifiedUser(
            user_id=data.get("user_id", ""),
            name=data.get("name", ""),
            persona=data.get("persona", ""),
            identities=dict(data.get("identities", {})),
            aliases=list(data.get("aliases", [])),
            traits=list(data.get("traits", [])),
            metadata=dict(data.get("metadata", {})),
        )

    def _load_alias_index(self, data: dict[str, Any]) -> None:
        """加载别名索引。"""
        for alias_key, entries_data in data.items():
            if not isinstance(entries_data, list):
                continue
            self._alias_index[alias_key] = []
            for entry_data in entries_data:
                if isinstance(entry_data, dict):
                    self._alias_index[alias_key].append(AliasEntry.from_dict(entry_data))

    def _merge_biography_data(self, user_id: str, data: dict[str, Any]) -> None:
        """合并传记数据到用户。"""
        user = self._users.get(user_id) or self._global_users.get(user_id)
        if not user:
            return

        user.identity_anchors = list(data.get("identity_anchors", []))
        user.relationships = [
            RelationshipAnchor.from_dict(r) if isinstance(r, dict) else r
            for r in data.get("relationships", [])
        ]
        user.short_bio = data.get("short_bio", "")
        user.affinity_score = float(data.get("affinity_score", 0.0))
        user.pending_messages = list(data.get("pending_messages", []))
        user.pending_message_count = int(data.get("pending_message_count", 0))
        user.distilled_points = list(data.get("distilled_points", []))
        user.last_distill_at = data.get("last_distill_at", "")
        user.last_updated_at = data.get("last_updated_at", "")
        user.bio_token_estimate = int(data.get("bio_token_estimate", 0))
        user.bio_token_budget = int(data.get("bio_token_budget", 500))

    def save_to_disk(self) -> None:
        """保存数据到磁盘。"""
        if not self._work_path:
            return

        self._work_path.mkdir(parents=True, exist_ok=True)

        # 保存 user_manager.json
        user_manager_path = self._work_path / "user_manager.json"
        user_manager_path.write_text(
            json.dumps(self._serialize_user_manager(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 保存 alias_index.json
        alias_index_path = self._work_path / "alias_index.json"
        alias_index_path.write_text(
            json.dumps(self._serialize_alias_index(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _serialize_user_manager(self) -> dict[str, Any]:
        """序列化 UserManager 格式的数据。"""
        return {
            "entries": {
                gid: {uid: u.to_dict() for uid, u in group.items()}
                for gid, group in self.entries.items()
            },
            "global": {
                uid: u.to_dict() for uid, u in self._global_users.items()
            },
        }

    def _serialize_alias_index(self) -> dict[str, Any]:
        """序列化别名索引。"""
        return {
            alias: [e.to_dict() for e in entries]
            for alias, entries in self._alias_index.items()
        }

    def _cleanup_on_startup(self) -> None:
        """启动时清理。"""
        # 清理人格身份别名污染
        cleaned = self._cleanup_polluted_aliases()
        if cleaned:
            logger.info("启动时清理了 %d 个人格身份别名污染", cleaned)

        # 时间衰减和低置信度清理
        decayed = self._decay_all_aliases()
        if decayed:
            logger.info("启动时衰减+清理了 %d 个别名条目", decayed)

    def _cleanup_polluted_aliases(self) -> int:
        """清理被人格身份名称污染的别名条目。"""
        cleaned = 0
        to_remove = []

        for alias_key, entries in self._alias_index.items():
            if self._is_persona_identity(alias_key):
                to_remove.append(alias_key)
                cleaned += 1
                continue

            filtered = [
                e for e in entries
                if not self._is_persona_identity(e.user_name.lower())
            ]
            if len(filtered) < len(entries):
                cleaned += len(entries) - len(filtered)
                if filtered:
                    self._alias_index[alias_key] = filtered
                else:
                    to_remove.append(alias_key)

        for key in to_remove:
            del self._alias_index[key]

        return cleaned

    def _decay_all_aliases(self) -> int:
        """对所有别名执行时间衰减和低置信度清理。"""
        removed = 0
        to_remove = []

        for alias_key, entries in self._alias_index.items():
            filtered = []
            for entry in entries:
                days = _days_since(entry.last_seen_at)
                entry.confidence = AliasEntry.apply_time_decay(entry.confidence, days)
                if entry.confidence >= AliasEntry.DECAY_THRESHOLD:
                    filtered.append(entry)
                else:
                    removed += 1

            if filtered:
                self._alias_index[alias_key] = filtered
            else:
                to_remove.append(alias_key)

        for key in to_remove:
            del self._alias_index[key]

        return removed

    # ── 内部工具 ─────────────────────────────────────────────

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
        for label in (user.name, user.user_id, *user.aliases):
            if label:
                self._speaker_index[self._normalize(label)] = user.user_id
        for platform, external_uid in user.identities.items():
            if platform and external_uid:
                self._identity_index[self._identity_key(platform, external_uid)] = user.user_id

    def _is_persona_identity(self, alias_lower: str) -> bool:
        """判断一个别名是否属于人格自身的身份名称。"""
        return bool(self._persona_name and alias_lower == self._persona_name) or (
            alias_lower in self._persona_aliases
        )

    def _sync_to_global(self, user: UnifiedUser) -> None:
        """同步到全局用户。"""
        uid = user.user_id
        if not uid:
            return

        global_user = self._global_users.get(uid)
        if global_user is None:
            from dataclasses import replace
            self._global_users[uid] = replace(
                user,
                aliases=list(user.aliases),
                identities=dict(user.identities),
                metadata=dict(user.metadata),
            )
            return

        # 合并
        for alias in user.aliases:
            if alias not in global_user.aliases:
                global_user.aliases.append(alias)
        for platform, external_uid in user.identities.items():
            if platform and external_uid:
                global_user.identities[platform] = external_uid
        global_user.metadata.update(user.metadata)
        if user.name and not global_user.name:
            global_user.name = user.name

    def _seed_from_global(self, user_id: str, group_id: str) -> UnifiedUser | None:
        """从全局用户种子到群组。"""
        global_user = self._global_users.get(user_id)
        if global_user is None:
            return None

        from dataclasses import replace
        local = replace(
            global_user,
            aliases=list(global_user.aliases),
            identities=dict(global_user.identities),
            metadata=dict(global_user.metadata),
        )
        group = self._ensure_group(group_id)
        group[user_id] = local
        self._update_indices(local)
        return local

    # ── 公共 API：用户管理（原 UserManager）──────────────────

    def register_user(self, user: UnifiedUser, group_id: str = "default") -> None:
        """注册或更新用户。"""
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
        for alias in user.aliases:
            if alias not in existing.aliases:
                existing.aliases.append(alias)
        for platform, external_uid in user.identities.items():
            if platform and external_uid:
                existing.identities[platform] = external_uid
        existing.metadata.update(user.metadata)

        self._update_indices(existing)
        self._sync_to_global(existing)

    def resolve_user_id(
        self,
        *,
        speaker: str | None = None,
        platform: str | None = None,
        external_uid: str | None = None,
    ) -> str | None:
        """解析用户 ID。"""
        if platform and external_uid:
            resolved = self._identity_index.get(self._identity_key(platform, external_uid))
            if resolved:
                return resolved
        if speaker:
            return self._speaker_index.get(self._normalize(speaker))
        return None

    def get_user(self, user_id: str, group_id: str = "default") -> UnifiedUser | None:
        """获取用户。"""
        group = self._ensure_group(group_id)
        local = group.get(user_id)
        if local is not None:
            return local
        return self._seed_from_global(user_id, group_id)

    def list_users(self, group_id: str = "default") -> list[UnifiedUser]:
        """列出群组中的所有用户。"""
        return list(self._ensure_group(group_id).values())

    def get_global_user(self, user_id: str) -> UnifiedUser | None:
        """获取全局用户。"""
        return self._global_users.get(user_id)

    def list_global_users(self) -> list[UnifiedUser]:
        """列出所有全局用户。"""
        return list(self._global_users.values())

    # ── 公共 API：别名管理（原 BiographyManager）──────────────

    def resolve_alias(
        self,
        alias: str,
        *,
        group_id: str = "",
        recent_speakers: list[str] | None = None,
        at_user_id: str | None = None,
    ) -> tuple[str | None, float, list[str]]:
        """别名消歧解析。"""
        alias_lower = alias.strip().lower()
        entries = self._alias_index.get(alias_lower, [])
        if not entries:
            return None, 0.0, []

        self._decay_alias_key(alias_lower)

        # L1: 按群过滤
        if group_id:
            group_entries = [e for e in entries if group_id in e.groups]
            if not group_entries:
                group_entries = entries
        else:
            group_entries = entries

        if len(group_entries) == 1:
            entry = group_entries[0]
            return entry.user_id, entry.confidence, []

        # 多人冲突
        sorted_entries = sorted(group_entries, key=lambda e: e.confidence, reverse=True)

        # 信号1: @ 锚定
        if at_user_id:
            for e in group_entries:
                if e.user_id == at_user_id:
                    conf = min(0.98, e.confidence + 0.30)
                    return e.user_id, conf, [
                        x.user_id for x in group_entries if x.user_id != e.user_id
                    ]

        # 信号2: 最近活跃者
        if recent_speakers:
            seen = set()
            for speaker in recent_speakers:
                if speaker in seen:
                    continue
                seen.add(speaker)
                for e in group_entries:
                    if e.user_id == speaker:
                        conf = min(0.85, e.confidence + 0.20)
                        return e.user_id, conf, [
                            x.user_id for x in group_entries if x.user_id != e.user_id
                        ]

        # 信号3: 置信度显著领先
        if len(sorted_entries) >= 2:
            if sorted_entries[0].confidence > sorted_entries[1].confidence * 1.5:
                conf = min(0.70, sorted_entries[0].confidence)
                return sorted_entries[0].user_id, conf, [
                    x.user_id for x in sorted_entries[1:]
                ]

        return None, 0.0, [e.user_id for e in group_entries]

    def register_alias(
        self,
        alias: str,
        user_id: str,
        user_name: str,
        group_id: str = "",
        source: str = "napcat",
    ) -> None:
        """注册别名。"""
        if self._is_persona_identity(alias.strip().lower()):
            return

        alias_lower = alias.strip().lower()
        if not alias_lower:
            return

        if alias_lower not in self._alias_index:
            self._alias_index[alias_lower] = []

        # 检查是否已存在
        for entry in self._alias_index[alias_lower]:
            if entry.user_id == user_id:
                entry.mentioned_count += 1
                entry.confidence = AliasEntry.compute_confidence(entry.mentioned_count, entry.source)
                entry.last_seen_at = _now_iso()
                if group_id and group_id not in entry.groups:
                    entry.groups.append(group_id)
                return

        # 新增
        entry = AliasEntry(
            user_id=user_id,
            user_name=user_name,
            groups=[group_id] if group_id else [],
            mentioned_count=1,
            confidence=AliasEntry.compute_confidence(1, source),
            first_seen_at=_now_iso(),
            last_seen_at=_now_iso(),
            source=source,
        )
        self._alias_index[alias_lower].append(entry)

    def bump_alias_weight(self, alias: str, user_id: str, group_id: str) -> None:
        """增加别名权重。"""
        alias_lower = alias.strip().lower()
        if alias_lower not in self._alias_index:
            return

        for entry in self._alias_index[alias_lower]:
            if entry.user_id == user_id:
                entry.mentioned_count += 1
                entry.confidence = AliasEntry.compute_confidence(entry.mentioned_count, entry.source)
                entry.last_seen_at = _now_iso()
                if group_id not in entry.groups:
                    entry.groups.append(group_id)

    def get_aliases_for_group(self, group_id: str) -> dict[str, str]:
        """获取群组相关的别名速查表。"""
        result: dict[str, str] = {}
        for alias, entries in self._alias_index.items():
            for e in entries:
                if group_id in e.groups:
                    result[alias] = e.user_name
                    break
        return result

    def _decay_alias_key(self, alias_lower: str) -> None:
        """对单个别名键执行时间衰减。"""
        entries = self._alias_index.get(alias_lower, [])
        filtered = []
        for entry in entries:
            days = _days_since(entry.last_seen_at)
            entry.confidence = AliasEntry.apply_time_decay(entry.confidence, days)
            if entry.confidence >= AliasEntry.DECAY_THRESHOLD:
                filtered.append(entry)

        if filtered:
            self._alias_index[alias_lower] = filtered
        elif alias_lower in self._alias_index:
            del self._alias_index[alias_lower]

    # ── 公共 API：传记管理 ──────────────────────────────────

    def get_or_create_user(self, user_id: str, name: str = "") -> UnifiedUser:
        """获取或创建用户。"""
        user = self._global_users.get(user_id)
        if user is None:
            user = UnifiedUser(user_id=user_id, name=name or user_id)
            self._global_users[user_id] = user
        if name and not user.name:
            user.name = name
        return user

    def feed_messages(
        self,
        user_id: str,
        name: str,
        group_id: str,
        messages: list[str],
        discovered_aliases: list[str] | None = None,
    ) -> None:
        """追加原始消息到蒸馏队列。"""
        user = self.get_or_create_user(user_id, name)

        user.pending_messages.extend(messages)
        total_chars = sum(len(m) for m in user.pending_messages)
        while total_chars > 2000 and len(user.pending_messages) > 1:
            user.pending_messages.pop(0)
            total_chars = sum(len(m) for m in user.pending_messages)

        user.pending_message_count += len(messages)

        if discovered_aliases:
            for alias in discovered_aliases:
                self.register_alias(alias, user_id, name, group_id, source="llm_discovery")

    def get_pending_users(self) -> list[UnifiedUser]:
        """获取有待蒸馏消息的用户。"""
        return [u for u in self._global_users.values() if u.pending_messages]

    def save_user(self, user: UnifiedUser) -> None:
        """保存用户数据。"""
        self._global_users[user.user_id] = user
        # 持久化由调用方决定


__all__ = ["UnifiedUserManager"]
