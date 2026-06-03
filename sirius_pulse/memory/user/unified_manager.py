"""统一用户管理器。

职责：
- 用户注册、解析、群隔离
- 别名索引和消歧
- 传记蒸馏和更新

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
    AliasEntry,
    RelationshipAnchor,
    UnifiedUser,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _alias_source_rank(source: str) -> int:
    if source == "manual":
        return 3
    if source == "napcat":
        return 2
    if source == "llm_discovery":
        return 1
    return 0


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

    功能：
    - 用户注册和解析
    - 别名索引和消歧
    - 传记蒸馏和更新

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
        provenance_store: Any | None = None,
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

        # 别名索引（懒加载 + 写穿缓存）
        self._alias_index: dict[str, list[AliasEntry]] = {}
        self._aliases_loaded = False
        self._provenance_store = provenance_store

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

    def _ensure_aliases_loaded(self) -> None:
        """确保别名索引已加载。"""
        if self._aliases_loaded:
            return
        aliases_data = self._storage.get_all_aliases()
        logger.info("从 SQLite 加载别名: %d 个", len(aliases_data))
        for alias, entries_data in aliases_data.items():
            self._alias_index[alias] = [AliasEntry.from_dict(e) for e in entries_data]
        self._aliases_loaded = True

    def _save_alias_to_storage(self, alias: str, entry: AliasEntry) -> None:
        """将别名单条目写穿到 SQLite。"""
        self._storage.save_alias_entry({
            "alias": alias,
            "user_id": entry.user_id,
            "user_name": entry.user_name,
            "weight": entry.weight,
            "groups": entry.groups,
            "mentioned_count": entry.mentioned_count,
            "confidence": entry.confidence,
            "first_seen_at": entry.first_seen_at,
            "last_seen_at": entry.last_seen_at,
            "source": entry.source,
        })
        self._save_alias_claim(alias, entry)

    def set_provenance_store(self, provenance_store: Any | None) -> None:
        """Attach the evidence ledger used by online alias writes."""
        self._provenance_store = provenance_store

    def _save_alias_claim(self, alias: str, entry: AliasEntry) -> None:
        store = self._provenance_store
        if store is None or not alias or not entry.user_id:
            return

        from sirius_pulse.memory.provenance.models import (
            ClaimAttribution,
            ClaimStatus,
            ClaimType,
            ExtractionRun,
            MemoryClaim,
        )

        source_key = f"alias:{alias}:{entry.user_id}"
        existing = store.find_claim_by_source_record(source_key)
        existing_status = (
            getattr(existing.status, "value", existing.status)
            if existing else ""
        )
        if (
            existing_status in {ClaimStatus.REJECTED.value, ClaimStatus.SHADOW.value}
            and entry.source != "manual"
        ):
            return

        if entry.source == "manual":
            status = ClaimStatus.ACTIVE
            attribution = ClaimAttribution.MANUAL
        elif entry.source == "napcat":
            status = ClaimStatus.ACTIVE
            attribution = ClaimAttribution.SECOND_PERSON_CONFIRMED
        else:
            status = ClaimStatus.CANDIDATE
            attribution = ClaimAttribution.INFERRED

        run_id = existing.extraction_run_id if existing else ""
        if not run_id:
            run = store.save_run(ExtractionRun(
                task="alias_register",
                model="alias_index",
                prompt_version="provenance-v1",
                metadata={"source": entry.source},
            ))
            run_id = run.run_id

        kwargs = {
            "subject_user_id": entry.user_id,
            "subject_label": entry.user_name or entry.user_id,
            "fact_type": ClaimType.ALIAS,
            "value": alias,
            "predicate": "别名",
            "object_value": alias,
            "status": status,
            "attribution": attribution,
            "confidence": entry.confidence,
            "evidence_ids": list(existing.evidence_ids) if existing else [],
            "extraction_run_id": run_id,
            "source": "alias_index",
            "source_record_id": source_key,
            "source_group_id": ",".join(entry.groups),
            "observed_at": entry.last_seen_at or entry.first_seen_at,
            "metadata": {
                "groups": list(entry.groups),
                "mentioned_count": entry.mentioned_count,
                "alias_source": entry.source,
            },
        }
        if existing:
            kwargs["claim_id"] = existing.claim_id
        store.save_claim(MemoryClaim(**kwargs))

    def reload(self) -> None:
        """强制从 SQLite 重新加载所有数据。"""
        self._global_users.clear()
        self.entries.clear()
        self._speaker_index.clear()
        self._identity_index.clear()
        self._users_loaded = False
        self._alias_index.clear()
        self._aliases_loaded = False

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

        for alias, entries in self._alias_index.items():
            for entry in entries:
                self._save_alias_to_storage(alias, entry)

    # ── 启动清理 ─────────────────────────────────────────

    def _cleanup_on_startup(self) -> None:
        """启动时清理别名。"""
        cleaned = self._cleanup_polluted_aliases()
        if cleaned:
            logger.info("启动时清理了 %d 个人格身份别名污染", cleaned)
        decayed = self._decay_all_aliases()
        if decayed:
            logger.info("启动时衰减+清理了 %d 个别名条目", decayed)

    def _cleanup_polluted_aliases(self) -> int:
        """清理被人格身份名称污染的别名条目。"""
        self._ensure_aliases_loaded()
        cleaned = 0
        to_remove: list[tuple[str, str]] = []
        for alias, entries in self._alias_index.items():
            if self._is_persona_identity(alias):
                for entry in entries:
                    self._storage.delete_alias_entry(alias, entry.user_id)
                to_remove.append((alias, ""))
                cleaned += len(entries)
                continue
            persona_entries = [e for e in entries if self._is_persona_identity(e.user_id)]
            for entry in persona_entries:
                self._storage.delete_alias_entry(alias, entry.user_id)
                entries.remove(entry)
                cleaned += 1
        for alias, _ in to_remove:
            self._alias_index.pop(alias, None)
        return cleaned

    def _decay_all_aliases(self) -> int:
        """对所有别名条目应用时间衰减。"""
        self._ensure_aliases_loaded()
        decayed = 0
        empty_keys: list[str] = []
        for alias, entries in self._alias_index.items():
            filtered = []
            for entry in entries:
                days = _days_since(entry.last_seen_at)
                entry.confidence = AliasEntry.apply_time_decay(entry.confidence, days)
                if entry.confidence >= AliasEntry.DECAY_THRESHOLD:
                    filtered.append(entry)
                else:
                    self._storage.delete_alias_entry(alias, entry.user_id)
                    decayed += 1
            if filtered:
                self._alias_index[alias] = filtered
            else:
                empty_keys.append(alias)
        for alias in empty_keys:
            del self._alias_index[alias]
        return decayed

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
        """同步到全局用户缓存。"""
        uid = user.user_id
        if not uid:
            return

        global_user = self._global_users.get(uid)
        if global_user is None:
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
        """从全局用户缓存种子到群组。"""
        self._ensure_users_loaded()

        global_user = self._global_users.get(user_id)
        if global_user is None:
            return None

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
        for alias in user.aliases:
            if alias not in existing.aliases:
                existing.aliases.append(alias)
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

    def resolve_alias(
        self,
        alias: str,
        *,
        group_id: str = "",
        recent_speakers: list[str] | None = None,
        at_user_id: str | None = None,
    ) -> tuple[str | None, float, list[str]]:
        """别名消歧解析。从 _alias_index 读取。"""
        self._ensure_aliases_loaded()
        alias_lower = alias.strip().lower()
        entries = self._alias_index.get(alias_lower, [])
        if not entries:
            return None, 0.0, []
        self._decay_alias_key(alias_lower)
        if group_id:
            group_entries = [e for e in entries if group_id in e.groups]
            if not group_entries:
                group_entries = entries
        else:
            group_entries = entries
        if len(group_entries) == 1:
            entry = group_entries[0]
            return entry.user_id, entry.confidence, []
        sorted_entries = sorted(
            group_entries, key=lambda e: e.confidence, reverse=True
        )
        if at_user_id:
            for e in group_entries:
                if e.user_id == at_user_id:
                    conf = min(0.98, e.confidence + 0.30)
                    others = [
                        x.user_id for x in group_entries if x.user_id != e.user_id
                    ]
                    return e.user_id, conf, others
        if recent_speakers:
            seen: set[str] = set()
            for speaker in recent_speakers:
                if speaker in seen:
                    continue
                seen.add(speaker)
                for e in group_entries:
                    if e.user_id == speaker:
                        conf = min(0.85, e.confidence + 0.20)
                        others = [
                            x.user_id
                            for x in group_entries
                            if x.user_id != e.user_id
                        ]
                        return e.user_id, conf, others
        if len(sorted_entries) >= 2:
            if sorted_entries[0].confidence > sorted_entries[1].confidence * 1.5:
                conf = min(0.70, sorted_entries[0].confidence)
                others = [x.user_id for x in sorted_entries[1:]]
                return sorted_entries[0].user_id, conf, others
        return None, 0.0, [e.user_id for e in group_entries]

    def register_alias(
        self,
        alias: str,
        user_id: str,
        user_name: str,
        group_id: str = "",
        source: str = "napcat",
    ) -> None:
        """注册别名。直接操作 _alias_index + aliases 表。"""
        self._ensure_aliases_loaded()
        if self._is_persona_identity(alias.strip().lower()):
            return
        alias_lower = alias.strip().lower()
        if not alias_lower:
            return
        if alias_lower not in self._alias_index:
            self._alias_index[alias_lower] = []
        for entry in self._alias_index[alias_lower]:
            if entry.user_id == user_id:
                entry.mentioned_count += 1
                if _alias_source_rank(source) > _alias_source_rank(entry.source):
                    entry.source = source
                entry.confidence = AliasEntry.compute_confidence(
                    entry.mentioned_count, entry.source
                )
                entry.last_seen_at = _now_iso()
                if group_id and group_id not in entry.groups:
                    entry.groups.append(group_id)
                self._save_alias_to_storage(alias_lower, entry)
                return
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
        self._save_alias_to_storage(alias_lower, entry)

    def _decay_alias_key(self, alias_lower: str) -> None:
        """对单个别名应用时间衰减，移除低于阈值的条目。"""
        entries = self._alias_index.get(alias_lower, [])
        filtered = []
        for entry in entries:
            days = _days_since(entry.last_seen_at)
            entry.confidence = AliasEntry.apply_time_decay(entry.confidence, days)
            if entry.confidence >= AliasEntry.DECAY_THRESHOLD:
                filtered.append(entry)
            else:
                self._storage.delete_alias_entry(alias_lower, entry.user_id)
        if filtered:
            self._alias_index[alias_lower] = filtered
        elif alias_lower in self._alias_index:
            del self._alias_index[alias_lower]

    def get_aliases_for_group(self, group_id: str) -> dict[str, str]:
        """获取群组相关的别名速查表。"""
        self._ensure_aliases_loaded()
        result: dict[str, str] = {}
        for alias, entries in self._alias_index.items():
            for e in entries:
                if group_id in e.groups:
                    result[alias] = e.user_name
                    break
        return result

    def bump_alias_weight(self, alias: str, user_id: str, group_id: str) -> None:
        """增加别名权重。"""
        self._ensure_aliases_loaded()
        alias_lower = alias.strip().lower()
        if alias_lower not in self._alias_index:
            return
        for entry in self._alias_index[alias_lower]:
            if entry.user_id == user_id:
                entry.mentioned_count += 1
                entry.confidence = AliasEntry.compute_confidence(
                    entry.mentioned_count, entry.source
                )
                entry.last_seen_at = _now_iso()
                if group_id not in entry.groups:
                    entry.groups.append(group_id)
                self._save_alias_to_storage(alias_lower, entry)

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

        # 写穿到 SQLite
        self._save_user_to_storage(user)

    def get_pending_users(self) -> list[UnifiedUser]:
        """获取有待蒸馏消息的用户。"""
        self._ensure_users_loaded()
        return [u for u in self._global_users.values() if u.pending_messages]

    def save_user(self, user: UnifiedUser) -> None:
        """保存用户数据。"""
        self._global_users[user.user_id] = user
        self._save_user_to_storage(user)


__all__ = ["UnifiedUserManager"]
