"""演化链验证中枢。

所有信息的验证、存储、追溯、纠正。
是整个记忆系统的"真理之源"。

验证流程：
1. 矛盾检测（3层级：结构化/语义/时序）
2. 置信度比较
3. 决策：ADD / SUPERSEDE / UPDATE / REJECT / MARK_UNCERTAIN
4. 级联纠正（如有）
5. 通知下游刷新
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sirius_pulse.memory.evolution.models import (
    EvolutionAction,
    EvolutionRecord,
    MetaTag,
    RecordStatus,
    SituationSource,
    Triple,
    ValidationResult,
)
from sirius_pulse.memory.evolution.store import EvolutionStore

logger = logging.getLogger(__name__)

__all__ = ["EvolutionChain", "ALIAS_PREDICATE"]


# 置信度门槛：低于此值的三元组直接拒绝
MIN_CONFIDENCE_THRESHOLD = 0.3

# 别称谓语常量
ALIAS_PREDICATE = "别名"


class EvolutionChain:
    """演化链验证中枢。

    独立于其他记忆模块，是整个系统的"真理之源"。
    所有信息进入系统前必须经过此验证。
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        conn: Any | None = None,
        embedding_client: Any | None = None,
        read_only: bool = False,
    ) -> None:
        self._store = EvolutionStore(db_path=db_path, conn=conn, read_only=read_only)
        self._embedding_client = embedding_client

        # 内存索引：subject → list[record_id]（仅 active）
        self._subject_index: dict[str, list[str]] = {}
        # 缓存：record_id → EvolutionRecord
        self._record_cache: dict[str, EvolutionRecord] = {}
        # 别称缓存：alias_lower → list[EvolutionRecord]（仅 active 别称记录）
        self._alias_cache: dict[str, list[EvolutionRecord]] = {}

        # 纠正回调：当记录被 supersede 时通知下游
        self._on_correction_callbacks: list[Callable] = []

        # 启动时加载索引
        self._rebuild_index()

    def close(self) -> None:
        """关闭存储连接。"""
        self._store.close()

    # ── 公开 API：验证与提交 ──

    async def validate_and_commit(
        self,
        new_triples: list[Triple],
        source: SituationSource,
    ) -> ValidationResult:
        """验证新三元组并提交到演化链。

        验证流程：
        1. 基础校验（字段完整性、置信度门槛）
        2. 矛盾检测（3层级）
        3. 决策：ADD / SUPERSEDE / REJECT / MARK_UNCERTAIN
        4. 持久化
        5. 更新索引和缓存

        Returns:
            ValidationResult 包含接受和拒绝的记录。
        """
        result = ValidationResult()

        for triple in new_triples:
            # Step 1: 基础校验
            rejection = self._validate_basic(triple)
            if rejection:
                result.rejected_triples.append(triple)
                result.rejection_reasons.append(rejection)
                continue

            # Step 2: 矛盾检测
            conflicts = self._find_conflicts(triple)

            # Step 3: 决策
            if not conflicts:
                # 无矛盾 → ADD
                record = self._create_record(triple, source)
                self._persist_record(record)
                result.records.append(record)
                result.actions.append(EvolutionAction.ADD)
            else:
                # 有矛盾 → 比较置信度决定动作
                action, record = await self._resolve_conflict(
                    triple, conflicts, source
                )
                result.records.append(record)
                result.actions.append(action)

        return result

    async def user_correct(
        self,
        subject: str,
        predicate: str,
        obj: str,
        source_message_id: str,
        group_id: str = "",
    ) -> EvolutionRecord:
        """用户显式纠正：最高优先级，confidence 直接拉满。

        用户亲口纠正的信息，置信度设为 1.0，
        并 supersede 所有与之矛盾的旧记录。
        """
        triple = Triple(
            subject=subject,
            predicate=predicate,
            obj=obj,
            confidence=1.0,
            meta_tag=MetaTag.USER_CORRECTED,
            source_message_id=source_message_id,
        )
        source = SituationSource(
            type="user_corrected",
            group_id=group_id,
            message_ids=[source_message_id],
        )

        # 查找矛盾
        conflicts = self._find_conflicts(triple)

        record = self._create_record(triple, source)

        if conflicts:
            for old in conflicts:
                self._supersede_record(old, record.record_id)
            record.supersedes = [c.record_id for c in conflicts]

        self._persist_record(record)
        return record

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

    def filter_active(self, triples: list[Triple]) -> list[Triple]:
        """过滤：只返回演化链中 active 状态的三元组。"""
        active_keys: set[str] = set()
        for subject in {t.subject for t in triples}:
            for record in self._store.get_active_by_subject(subject):
                active_keys.add(record.content_key)

        return [t for t in triples if t.content_key in active_keys]

    def register_correction_callback(self, callback: Callable) -> None:
        """注册纠正回调：当记录被 supersede 时调用。"""
        self._on_correction_callbacks.append(callback)

    # ── 公开 API：别称管理 ──

    def register_alias(
        self,
        alias: str,
        user_id: str,
        user_name: str,
        group_id: str = "",
        source: str = "napcat",
    ) -> None:
        """注册别称到演化链。

        如同一 (alias_lower, user_id) 已存在则增强验证；
        否则创建新的别称记录。

        Args:
            alias: 别称文本
            user_id: 关联的用户 ID
            user_name: 用户显示名（记录 subject）
            group_id: 来源群组 ID
            source: 来源类型，"napcat" 或 "llm_discovery"
        """
        alias_stripped = alias.strip()
        if not alias_stripped:
            return

        alias_lower = alias_stripped.lower()

        # 在缓存中查找同一 (alias_lower, user_id) 的已有记录
        existing_records = self._alias_cache.get(alias_lower, [])
        for record in existing_records:
            if record.subject_user_id == user_id and record.is_active:
                # 已存在 → 增强验证
                record.add_verification(
                    "mention", group_id, confidence_delta=0.05
                )
                record.source_group_id = group_id
                self._store.save_record(record)
                logger.debug(
                    "别称验证增强: %s → %s (confidence=%.2f)",
                    alias_lower, user_id, record.confidence,
                )
                return

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
            alias_lower, user_id, source, confidence,
        )

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

        # 按 group_id 过滤：仅保留与当前群组相关的记录
        if group_id:
            filtered = [
                r for r in records
                if self._record_matches_group(r, group_id)
            ]
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
                if (
                    record.is_active
                    and record.source_group_id == group_id
                ):
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

    def bump_alias(
        self, alias: str, user_id: str, group_id: str
    ) -> None:
        """在活跃事件中对别称记录进行 bump 验证。

        每次别称被提及使用时调用，增强置信度。
        """
        if not alias or not alias.strip():
            return

        alias_lower = alias.strip().lower()
        records = self._alias_cache.get(alias_lower, [])
        for record in records:
            if record.subject_user_id == user_id and record.is_active:
                record.add_verification(
                    "bump", group_id, confidence_delta=0.05
                )
                self._store.save_record(record)
                logger.debug(
                    "别称 bump: %s → %s (confidence=%.2f)",
                    alias_lower, user_id, record.confidence,
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
                r for r in records if not (
                    r.subject_user_id == user_id and r.status == RecordStatus.REJECTED
                )
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
                last_time_str = record.verifications[-1].get(
                    "verified_at", last_time_str
                )

            try:
                last_time = datetime.fromisoformat(
                    last_time_str.replace("Z", "+00:00")
                )
                days = (now - last_time).days
            except (ValueError, TypeError):
                continue

            if days <= 0:
                continue

            # 衰减
            record.confidence *= 0.95 ** days

            if record.confidence < 0.10:
                record.status = RecordStatus.SHADOW
                logger.debug(
                    "别称衰减至 SHADOW: %s → %s (confidence=%.3f)",
                    record.obj, record.subject_user_id, record.confidence,
                )
                # 从别称缓存中移除
                alias_key = record.obj.lower()
                alias_list = self._alias_cache.get(alias_key, [])
                self._alias_cache[alias_key] = [
                    r for r in alias_list
                    if r.record_id != record.record_id
                ]
                if not self._alias_cache[alias_key]:
                    del self._alias_cache[alias_key]
            else:
                logger.debug(
                    "别称衰减: %s → %s (confidence=%.3f, days=%d)",
                    record.obj, record.subject_user_id,
                    record.confidence, days,
                )

            self._store.save_record(record)

    def cleanup_polluted_aliases(
        self, persona_name: str, persona_aliases: list[str]
    ) -> None:
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
                r for r in alias_list
                if r.record_id != record.record_id
            ]
            if not self._alias_cache[alias_key]:
                del self._alias_cache[alias_key]

            self._record_cache[record.record_id] = record

            logger.info(
                "清理污染别称: %s → %s (REJECTED)",
                record.obj, record.subject_user_id,
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

    # ── 内部：基础校验 ──

    def _validate_basic(self, triple: Triple) -> str | None:
        """基础校验，返回拒绝原因或 None。"""
        if not triple.subject or not triple.subject.strip():
            return "主语为空"
        if not triple.predicate or not triple.predicate.strip():
            return "谓语为空"
        if not triple.obj or not triple.obj.strip():
            return "宾语为空"
        if triple.confidence < MIN_CONFIDENCE_THRESHOLD:
            return f"置信度过低 ({triple.confidence:.2f} < {MIN_CONFIDENCE_THRESHOLD})"
        return None

    # ── 内部：矛盾检测（3层级）──

    def _find_conflicts(self, new_triple: Triple) -> list[EvolutionRecord]:
        """三层矛盾检测。

        Layer 1: 结构化矛盾（同主体、同谓语、不同宾语）
        Layer 2: 语义矛盾（embedding 相似度高且逻辑矛盾）
        Layer 3: 时序矛盾（同一属性随时间变化）
        """
        conflicts: list[EvolutionRecord] = []
        seen_ids: set[str] = set()

        # Layer 1: 结构化矛盾
        for record in self._find_exact_contradiction(new_triple):
            if record.record_id not in seen_ids:
                conflicts.append(record)
                seen_ids.add(record.record_id)

        # Layer 2: 语义矛盾（需要 embedding 支持）
        if self._embedding_client:
            for record in self._find_semantic_contradiction(new_triple):
                if record.record_id not in seen_ids:
                    conflicts.append(record)
                    seen_ids.add(record.record_id)

        # Layer 3: 时序矛盾
        for record in self._find_temporal_contradiction(new_triple):
            if record.record_id not in seen_ids:
                conflicts.append(record)
                seen_ids.add(record.record_id)

        return conflicts

    def _find_exact_contradiction(
        self, new_triple: Triple
    ) -> list[EvolutionRecord]:
        """Layer 1: 结构化矛盾。

        同一主体、同一谓语，但宾语不同。
        例: (小明, 住, 北京) vs (小明, 住, 深圳)
        """
        existing = self._store.find_by_content(
            subject=new_triple.subject,
            predicate=new_triple.predicate,
            status=RecordStatus.ACTIVE,
        )

        conflicts = []
        for record in existing:
            if record.obj != new_triple.obj:
                conflicts.append(record)

        return conflicts

    def _find_semantic_contradiction(
        self, new_triple: Triple
    ) -> list[EvolutionRecord]:
        """Layer 2: 语义矛盾。

        通过 embedding 相似度检测语义上矛盾的信息。
        例: (小明, 是, 单身) vs (小明, 有, 女朋友)
        需要 embedding_client 支持。
        """
        if not self._embedding_client:
            return []

        # 获取该主体的所有 active 记录
        existing = self._store.get_active_by_subject(new_triple.subject)
        if not existing:
            return []

        # 计算新三元组的 embedding
        new_text = f"{new_triple.subject} {new_triple.predicate} {new_triple.obj}"
        try:
            new_embedding = self._embedding_client.embed(new_text)
        except Exception:
            return []

        conflicts = []
        for record in existing:
            existing_text = f"{record.subject} {record.predicate} {record.obj}"
            try:
                existing_embedding = self._embedding_client.embed(existing_text)
            except Exception:
                continue

            # 计算相似度
            similarity = self._cosine_similarity(new_embedding, existing_embedding)

            # 高相似度 + 不同内容 → 可能矛盾
            if similarity > 0.8 and record.content_key != new_triple.content_key:
                # 简单的矛盾信号：包含否定词或反义
                if self._has_contradiction_signal(
                    new_triple.predicate, record.predicate
                ):
                    conflicts.append(record)

        return conflicts

    def _find_temporal_contradiction(
        self, new_triple: Triple
    ) -> list[EvolutionRecord]:
        """Layer 3: 时序矛盾。

        同一属性随时间变化。
        例: 三年前的 (小明, 住, 北京) vs 现在的 (小明, 住, 深圳)
        """
        existing = self._store.find_by_content(
            subject=new_triple.subject,
            predicate=new_triple.predicate,
            status=RecordStatus.ACTIVE,
        )

        conflicts = []
        for record in existing:
            if record.obj != new_triple.obj:
                # 检查时间跨度
                try:
                    old_time = datetime.fromisoformat(
                        record.extracted_at.replace("Z", "+00:00")
                    )
                    new_time = datetime.now(timezone.utc)
                    days_diff = (new_time - old_time).days

                    # 超过 7 天的不同宾语 → 时序矛盾
                    if days_diff > 7:
                        conflicts.append(record)
                except (ValueError, TypeError):
                    # 时间解析失败，按结构化矛盾处理
                    conflicts.append(record)

        return conflicts

    # ── 内部：冲突解决 ──

    async def _resolve_conflict(
        self,
        new_triple: Triple,
        conflicts: list[EvolutionRecord],
        source: SituationSource,
    ) -> tuple[EvolutionAction, EvolutionRecord]:
        """解决冲突：比较置信度，决定动作。"""

        # 计算冲突记录的最高置信度
        max_old_confidence = max(c.confidence for c in conflicts)

        if new_triple.confidence > max_old_confidence:
            # 新信息更可信 → SUPERSEDE
            record = self._create_record(new_triple, source)
            for old in conflicts:
                self._supersede_record(old, record.record_id)
            record.supersedes = [c.record_id for c in conflicts]
            self._persist_record(record)
            return EvolutionAction.SUPERSEDE, record

        elif new_triple.confidence < max_old_confidence * 0.5:
            # 新信息置信度远低于旧信息 → REJECT
            record = self._create_record(new_triple, source)
            record.status = RecordStatus.REJECTED
            self._persist_record(record)
            return EvolutionAction.REJECT, record

        else:
            # 置信度相近 → MARK_UNCERTAIN，等待进一步验证
            record = self._create_record(new_triple, source)
            record.status = RecordStatus.UNCERTAIN
            self._persist_record(record)
            return EvolutionAction.MARK_UNCERTAIN, record

    # ── 内部：记录操作 ──

    def _create_record(
        self, triple: Triple, source: SituationSource
    ) -> EvolutionRecord:
        """从三元组和来源创建演化链记录。"""
        return EvolutionRecord(
            subject=triple.subject,
            subject_user_id=triple.subject_user_id,
            predicate=triple.predicate,
            obj=triple.obj,
            status=RecordStatus.ACTIVE,
            confidence=triple.confidence,
            initial_confidence=triple.confidence,
            source_type=triple.meta_tag,
            source_situation_id="",
            source_group_id=source.group_id,
            source_message_ids=[triple.source_message_id]
            if triple.source_message_id
            else [],
            extracted_by_model=source.model,
        )

    def _supersede_record(
        self, old_record: EvolutionRecord, new_record_id: str
    ) -> None:
        """将旧记录标记为 superseded。"""
        old_record.status = RecordStatus.SUPERSEDED
        old_record.superseded_by = new_record_id
        old_record.add_correction(
            old_value=f"{old_record.subject} {old_record.predicate} {old_record.obj}",
            new_value="",
            reason=f"被 {new_record_id} 取代",
            cascade_affected=[new_record_id],
        )
        self._store.save_record(old_record)

        # 更新缓存
        self._record_cache[old_record.record_id] = old_record

        # 从索引中移除
        subject_records = self._subject_index.get(old_record.subject, [])
        if old_record.record_id in subject_records:
            subject_records.remove(old_record.record_id)

        # 从别称缓存中移除
        if old_record.predicate == ALIAS_PREDICATE:
            alias_key = old_record.obj.lower()
            alias_list = self._alias_cache.get(alias_key, [])
            self._alias_cache[alias_key] = [
                r for r in alias_list if r.record_id != old_record.record_id
            ]
            if not self._alias_cache[alias_key]:
                del self._alias_cache[alias_key]

        # 通知下游
        for callback in self._on_correction_callbacks:
            try:
                callback(old_record, new_record_id)
            except Exception as exc:
                logger.error("纠正回调执行失败: %s", exc)

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

    # ── 内部：工具方法 ──

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """计算两个向量的余弦相似度。"""
        if not a or not b or len(a) != len(b):
            return 0.0

        dot_product = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot_product / (norm_a * norm_b)

    @staticmethod
    def _has_contradiction_signal(pred_a: str, pred_b: str) -> bool:
        """检测两个谓语之间是否存在矛盾信号。

        简单规则：相同谓语但不同宾语本身就可能是矛盾。
        更复杂的矛盾检测需要 LLM 支持。
        """
        # 同一谓语 → 可能是同一属性的不同值
        if pred_a == pred_b:
            return True

        # 包含否定词
        negations = {"不", "没", "无", "非", "别", "未"}
        if any(n in pred_a for n in negations) != any(
            n in pred_b for n in negations
        ):
            return True

        return False
