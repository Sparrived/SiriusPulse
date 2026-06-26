"""演化链验证中枢。

别称系统的验证、存储、追溯。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.memory.alias_policy import validate_person_alias
from sirius_pulse.memory.evolution.models import (
    EvolutionRecord,
    MetaTag,
    RecordStatus,
)
from sirius_pulse.memory.evolution.store import EvolutionStore

logger = logging.getLogger(__name__)

__all__ = ["EvolutionChain", "ALIAS_PREDICATE"]

# 别称谓语常量
ALIAS_PREDICATE = "别名"


class EvolutionChain:
    """演化链验证中枢。

    管理别称记录的注册、解析、衰减和清理。
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        conn: Any | None = None,
        read_only: bool = False,
    ) -> None:
        self._store = EvolutionStore(db_path=db_path, conn=conn, read_only=read_only)

        # 内存索引：subject → list[record_id]（仅 active）
        self._subject_index: dict[str, list[str]] = {}
        # 缓存：record_id → EvolutionRecord
        self._record_cache: dict[str, EvolutionRecord] = {}
        # 别称缓存：alias_lower → list[EvolutionRecord]（仅 active 别称记录）
        self._alias_cache: dict[str, list[EvolutionRecord]] = {}
        self._correction_callbacks: list[Any] = []

        # 启动时加载索引
        self._rebuild_index()

    def close(self) -> None:
        """关闭存储连接。"""
        self._store.close()

    def register_correction_callback(self, callback: Any) -> None:
        """Register a callback fired when an active record is corrected."""
        if callable(callback) and callback not in self._correction_callbacks:
            self._correction_callbacks.append(callback)

    def _notify_correction(self, record: EvolutionRecord, new_record_id: str = "") -> None:
        for callback in list(self._correction_callbacks):
            try:
                callback(record, new_record_id)
            except Exception:
                logger.debug("Evolution correction callback failed", exc_info=True)

    # ── 公开 API：查询 ──

    def get_active_by_subject(self, subject: str) -> list[EvolutionRecord]:
        """获取某主体的所有 active 记录。"""
        return self._store.get_active_by_subject(subject)

    def get_all_by_subject(self, subject: str) -> list[EvolutionRecord]:
        """获取某主体的所有记录（含 shadow）。"""
        return self._store.get_all_by_subject(subject)

    def get_active_by_user_id(self, user_id: str) -> list[EvolutionRecord]:
        """按 user_id 获取所有 active 记录（别名系统关联）。"""
        return self._store.get_active_by_user_id(user_id)

    def get_all_by_user_id(self, user_id: str) -> list[EvolutionRecord]:
        """按 user_id 获取所有记录。"""
        return self._store.get_all_by_user_id(user_id)

    def get_uncertain_records(self, limit: int = 50) -> list[EvolutionRecord]:
        """获取所有待验证的记录。"""
        return self._store.get_uncertain_records(limit)

    def get_history(self, record_id: str) -> list[EvolutionRecord]:
        """获取某条记录的完整演化链（前驱 + 后继）。"""
        chain: list[EvolutionRecord] = []
        visited: set[str] = set()

        # 向前追溯
        current_id: str | None = record_id
        while current_id and current_id not in visited:
            visited.add(current_id)
            record = self._store.get_record(current_id)
            if record:
                chain.append(record)
                current_id = record.superseded_by
            else:
                break

        # 向后追溯（从原始记录开始）
        if chain:
            original = chain[-1]
            for sup_id in original.supersedes:
                if sup_id not in visited:
                    sup_record = self._store.get_record(sup_id)
                    if sup_record:
                        chain.insert(0, sup_record)

        return chain

    # ── 公开 API：别称管理 ──

    def register_alias(
        self,
        alias: str,
        user_id: str,
        user_name: str,
        group_id: str = "",
        source: str = "napcat",
    ) -> bool:
        """注册别称到演化链。

        如同一 (alias_lower, user_id) 已存在则增强验证；
        否则创建新的别称记录。

        Args:
            alias: 别称文本
            user_id: 关联的用户 ID
            user_name: 用户显示名（记录 subject）
            group_id: 来源群组 ID
            source: 来源类型，例如 "model_skill" 或 "manual"
        """
        ok, alias_lower, reason = validate_person_alias(alias)
        if not ok:
            logger.info("拒绝别称注册: %s (%s)", alias, reason)
            return False

        # 在缓存中查找同一 (alias_lower, user_id) 的已有记录
        existing_records = [r for r in self._alias_cache.get(alias_lower, []) if r.is_active]
        same_record: EvolutionRecord | None = None
        for record in existing_records:
            if record.subject_user_id == user_id and record.is_active:
                same_record = record
                continue
            record.status = RecordStatus.SHADOW
            record.add_correction(
                old_value=record.subject_user_id,
                new_value=user_id,
                reason="同一别称只能映射到一个用户",
            )
            self._store.save_record(record)

        if same_record is not None:
            # 已存在 → 增强验证
            same_record.add_verification("mention", group_id, confidence_delta=0.05)
            same_record.source_group_id = group_id
            self._store.save_record(same_record)
            self._alias_cache[alias_lower] = [same_record]
            logger.debug(
                "别称验证增强: %s → %s (confidence=%.2f)",
                alias_lower,
                user_id,
                same_record.confidence,
            )
            return True

        self._alias_cache.pop(alias_lower, None)

        # 不存在 → 创建新记录
        if source == "napcat":
            confidence = 0.50
            source_type = MetaTag.STATED
        else:
            confidence = 0.30
            source_type = MetaTag.INFERRED

        record = EvolutionRecord(
            subject=user_name,
            subject_user_id=user_id,
            predicate=ALIAS_PREDICATE,
            obj=alias_lower,
            status=RecordStatus.ACTIVE,
            confidence=confidence,
            initial_confidence=confidence,
            source_type=source_type,
            source_group_id=group_id,
        )
        self._persist_record(record)
        logger.debug(
            "别称注册: %s → %s (%s, confidence=%.2f)",
            alias_lower,
            user_id,
            source,
            confidence,
        )
        return True

    def resolve_alias(
        self,
        alias: str,
        group_id: str = "",
        recent_speakers: list[str] | None = None,
        at_user_id: str | None = None,
    ) -> tuple[str | None, float, list[str]]:
        """解析别称到 user_id。

        消歧策略：@ 锚定 > 最近活跃 > 置信度领先（1.5x 阈值）。

        Args:
            alias: 待解析的别称
            group_id: 当前群组 ID（用于过滤）
            recent_speakers: 最近发言者 user_id 列表
            at_user_id: @ 指定的 user_id

        Returns:
            (user_id | None, confidence, disambiguation_candidates)
        """
        if not alias or not alias.strip():
            return None, 0.0, []

        alias_lower = alias.strip().lower()
        records = self._alias_cache.get(alias_lower, [])
        if not records:
            return None, 0.0, []
        records = self._coalesce_alias_records(alias_lower, records)

        # 按 group_id 过滤：仅保留与当前群组相关的记录
        if group_id:
            filtered = [r for r in records if self._record_matches_group(r, group_id)]
            # 如果过滤后为空，回退到全量
            if filtered:
                records = filtered

        if not records:
            return None, 0.0, []

        # 单命中 → 直接返回
        if len(records) == 1:
            rec = records[0]
            return rec.subject_user_id, rec.confidence, []

        # 多命中 → 消歧
        candidates: list[str] = []
        scored: list[tuple[EvolutionRecord, float]] = []
        recent = set(recent_speakers or [])

        for rec in records:
            score = rec.confidence
            # @ 锚定加分
            if at_user_id and rec.subject_user_id == at_user_id:
                score += 0.30
            # 最近活跃者加分
            if rec.subject_user_id in recent:
                score += 0.20
            scored.append((rec, score))
            candidates.append(rec.subject_user_id)

        # 按得分降序
        scored.sort(key=lambda x: x[1], reverse=True)
        best_rec, best_score = scored[0]
        second_score = scored[1][1] if len(scored) > 1 else 0.0

        # 置信度领先 1.5x 阈值
        if best_score >= second_score * 1.5 or at_user_id:
            return best_rec.subject_user_id, best_score, candidates

        # 无法消歧
        return None, best_score, candidates

    def get_aliases_for_group(self, group_id: str) -> dict[str, str]:
        """获取群组内的所有别称映射。

        Returns:
            {alias_obj: user_name}
        """
        result: dict[str, str] = {}
        for alias_lower, records in self._alias_cache.items():
            for record in records:
                if record.is_active and record.source_group_id == group_id:
                    result[alias_lower] = record.subject
                    break  # 每个别称取第一个匹配即可
        return result

    def get_user_aliases(self, user_id: str) -> list[str]:
        """获取用户的所有 active 别称。

        Returns:
            别称列表（obj 字段）
        """
        records = self._store.get_by_predicate_and_user_id(
            ALIAS_PREDICATE, user_id, status=RecordStatus.ACTIVE
        )
        return [r.obj for r in records]

    def bump_alias(self, alias: str, user_id: str, group_id: str) -> None:
        """在活跃事件中对别称记录进行 bump 验证。

        每次别称被提及使用时调用，增强置信度。
        """
        if not alias or not alias.strip():
            return

        alias_lower = alias.strip().lower()
        records = self._alias_cache.get(alias_lower, [])
        for record in records:
            if record.subject_user_id == user_id and record.is_active:
                record.add_verification("bump", group_id, confidence_delta=0.05)
                self._store.save_record(record)
                logger.debug(
                    "别称 bump: %s → %s (confidence=%.2f)",
                    alias_lower,
                    user_id,
                    record.confidence,
                )
                return

    def reject_alias(self, alias: str, user_id: str) -> bool:
        """拒绝/删除指定别称记录。

        将匹配的别称记录标记为 REJECTED 并从缓存中移除。

        Returns:
            是否找到并拒绝了记录
        """
        if not alias or not alias.strip():
            return False

        alias_lower = alias.strip().lower()
        records = self._alias_cache.get(alias_lower, [])
        found = False

        for record in records:
            if record.subject_user_id == user_id and record.is_active:
                record.status = RecordStatus.REJECTED
                record.add_correction(
                    old_value=record.obj,
                    new_value="",
                    reason="通过 Web UI 手动删除",
                )
                self._store.save_record(record)
                self._record_cache[record.record_id] = record

                # 从索引和缓存中移除
                subject_records = self._subject_index.get(record.subject, [])
                if record.record_id in subject_records:
                    subject_records.remove(record.record_id)

                found = True
                logger.info("拒绝别称: %s → %s (REJECTED)", alias_lower, user_id)

        if found:
            # 清理缓存
            self._alias_cache[alias_lower] = [
                r
                for r in records
                if not (r.subject_user_id == user_id and r.status == RecordStatus.REJECTED)
            ]
            if not self._alias_cache[alias_lower]:
                del self._alias_cache[alias_lower]

        return found

    def shadow_alias(self, alias: str, user_id: str) -> bool:
        """将指定别称记录标记为 shadow 状态。

        Shadow 状态的记录不参与召回，但保留可追溯性。

        Returns:
            是否找到并标记了记录
        """
        if not alias or not alias.strip():
            return False

        alias_lower = alias.strip().lower()
        records = self._alias_cache.get(alias_lower, [])
        found = False

        for record in records:
            if record.subject_user_id == user_id and record.is_active:
                record.status = RecordStatus.SHADOW
                record.add_correction(
                    old_value=record.obj,
                    new_value="",
                    reason="通过 Web UI 标记为 shadow",
                )
                self._store.save_record(record)
                self._record_cache[record.record_id] = record

                # 从索引中移除
                subject_records = self._subject_index.get(record.subject, [])
                if record.record_id in subject_records:
                    subject_records.remove(record.record_id)

                found = True
                logger.info("Shadow 别称: %s → %s", alias_lower, user_id)

        if found:
            # 清理缓存
            self._alias_cache[alias_lower] = [
                r
                for r in records
                if not (r.subject_user_id == user_id and r.status == RecordStatus.SHADOW)
            ]
            if not self._alias_cache[alias_lower]:
                del self._alias_cache[alias_lower]

        return found

    def decay_alias_records(self) -> None:
        """对别称记录进行时间衰减。

        基于最后一次验证时间计算天数，
        confidence *= 0.95^days，
        低于 0.10 的标记为 SHADOW。
        """
        all_active = [
            self._record_cache[rid]
            for rids in self._subject_index.values()
            for rid in rids
            if rid in self._record_cache
        ]
        now = datetime.now(timezone.utc)
        for record in all_active:
            if record.predicate != ALIAS_PREDICATE:
                continue

            # 取最后一次验证时间，无验证则用提取时间
            last_time_str = record.extracted_at
            if record.verifications:
                last_time_str = record.verifications[-1].get("verified_at", last_time_str)

            try:
                last_time = datetime.fromisoformat(last_time_str.replace("Z", "+00:00"))
                days = (now - last_time).days
            except (ValueError, TypeError):
                continue

            if days <= 0:
                continue

            # 衰减
            record.confidence *= 0.95**days

            if record.confidence < 0.10:
                record.status = RecordStatus.SHADOW
                logger.debug(
                    "别称衰减至 SHADOW: %s → %s (confidence=%.3f)",
                    record.obj,
                    record.subject_user_id,
                    record.confidence,
                )
                # 从别称缓存中移除
                alias_key = record.obj.lower()
                alias_list = self._alias_cache.get(alias_key, [])
                self._alias_cache[alias_key] = [
                    r for r in alias_list if r.record_id != record.record_id
                ]
                if not self._alias_cache[alias_key]:
                    del self._alias_cache[alias_key]
            else:
                logger.debug(
                    "别称衰减: %s → %s (confidence=%.3f, days=%d)",
                    record.obj,
                    record.subject_user_id,
                    record.confidence,
                    days,
                )

            self._store.save_record(record)

    def cleanup_polluted_aliases(self, persona_name: str, persona_aliases: list[str]) -> None:
        """清理被污染的别称记录。

        将 obj 等于人格名称或人格别名的记录标记为 REJECTED。
        """
        polluted_keys = {persona_name.lower()}
        for pa in persona_aliases:
            polluted_keys.add(pa.lower())

        all_active = [
            self._record_cache[rid]
            for rids in self._subject_index.values()
            for rid in rids
            if rid in self._record_cache
        ]
        for record in all_active:
            if record.predicate != ALIAS_PREDICATE:
                continue
            if record.obj.lower() not in polluted_keys:
                continue

            record.status = RecordStatus.REJECTED
            record.add_correction(
                old_value=record.obj,
                new_value="",
                reason=f"别称与人格名/别名冲突: {record.obj}",
            )
            self._store.save_record(record)

            # 从索引和缓存中移除
            subject_records = self._subject_index.get(record.subject, [])
            if record.record_id in subject_records:
                subject_records.remove(record.record_id)

            alias_key = record.obj.lower()
            alias_list = self._alias_cache.get(alias_key, [])
            self._alias_cache[alias_key] = [
                r for r in alias_list if r.record_id != record.record_id
            ]
            if not self._alias_cache[alias_key]:
                del self._alias_cache[alias_key]

            self._record_cache[record.record_id] = record

            logger.info(
                "清理污染别称: %s → %s (REJECTED)",
                record.obj,
                record.subject_user_id,
            )

    # ── 内部：别称辅助 ──

    @staticmethod
    def _record_matches_group(record: EvolutionRecord, group_id: str) -> bool:
        """检查记录是否与指定群组相关。

        匹配条件：source_group_id 相同，或 verifications 中包含该 group_id。
        """
        if record.source_group_id == group_id:
            return True
        for v in record.verifications:
            if v.get("details") == group_id:
                return True
        return False

    def _coalesce_alias_records(
        self, alias_lower: str, records: list[EvolutionRecord]
    ) -> list[EvolutionRecord]:
        """Keep one active record per alias and shadow weaker duplicates."""
        active = [record for record in records if record.is_active]
        if len(active) <= 1:
            self._alias_cache[alias_lower] = active
            return active
        active.sort(key=lambda record: record.confidence, reverse=True)
        winner = active[0]
        for record in active[1:]:
            record.status = RecordStatus.SHADOW
            record.add_correction(
                old_value=record.subject_user_id,
                new_value=winner.subject_user_id,
                reason="同一别称只能映射到一个用户",
            )
            self._store.save_record(record)
        self._alias_cache[alias_lower] = [winner]
        return [winner]

    # ── 内部：记录操作 ──

    def _persist_record(self, record: EvolutionRecord) -> None:
        """持久化记录并更新索引（含别称缓存）。"""
        self._store.save_record(record)
        self._record_cache[record.record_id] = record

        if record.is_active:
            if record.subject not in self._subject_index:
                self._subject_index[record.subject] = []
            if record.record_id not in self._subject_index[record.subject]:
                self._subject_index[record.subject].append(record.record_id)

            # 别称记录同步到别称缓存
            if record.predicate == ALIAS_PREDICATE:
                alias_key = record.obj.lower()
                if alias_key not in self._alias_cache:
                    self._alias_cache[alias_key] = []
                # 避免重复添加
                existing_ids = {r.record_id for r in self._alias_cache[alias_key]}
                if record.record_id not in existing_ids:
                    self._alias_cache[alias_key].append(record)

    def _rebuild_index(self) -> None:
        """从数据库重建内存索引（含别称缓存）。"""
        self._subject_index.clear()
        self._record_cache.clear()
        self._alias_cache.clear()

        subjects = self._store.get_all_subjects()
        for subject in subjects:
            records = self._store.get_active_by_subject(subject)
            self._subject_index[subject] = [r.record_id for r in records]
            for record in records:
                self._record_cache[record.record_id] = record

        # 构建别称缓存：alias_lower → active 别称记录列表
        all_active_records = [
            self._record_cache[rid]
            for rids in self._subject_index.values()
            for rid in rids
            if rid in self._record_cache
        ]
        for record in all_active_records:
            if record.predicate == ALIAS_PREDICATE:
                alias_key = record.obj.lower()
                if alias_key not in self._alias_cache:
                    self._alias_cache[alias_key] = []
                self._alias_cache[alias_key].append(record)
        for alias_key, records in list(self._alias_cache.items()):
            self._coalesce_alias_records(alias_key, records)
