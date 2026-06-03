"""传记视图：从证据账本自动派生用户传记。

不存储独立数据。优先从 provenance active/profile-safe claims 派生；
没有新账本数据时 fallback 到 EvolutionChain 的 active 三元组。
"""

from __future__ import annotations

import logging
from typing import Any

from sirius_pulse.memory.biography.models import UserBiography
from sirius_pulse.memory.evolution.chain import EvolutionChain
from sirius_pulse.memory.evolution.models import EvolutionRecord, RecordStatus
from sirius_pulse.memory.provenance.models import ClaimStatus, ClaimType, MemoryClaim
from sirius_pulse.memory.provenance.store import ProvenanceStore

logger = logging.getLogger(__name__)

__all__ = ["BiographyView"]

# 谓语分类映射
_IDENTITY_PREDICATES = {
    "是", "住在", "住在", "工作于", "就读于", "来自", "搬到",
    "职位", "职业", "专业", "学校", "公司",
}

_RELATIONSHIP_PREDICATES = {
    "认识", "是朋友", "是同事", "是同学", "是室友",
    "喜欢", "讨厌", "暗恋", "追求",
}

_PREFERENCE_PREDICATES = {
    "爱吃", "喜欢做", "习惯", "常用", "推荐",
    "爱好", "兴趣", "擅长",
}


class BiographyView:
    """传记视图：证据账本的投影。

    从 ProvenanceStore 的强归属 active claims 自动派生用户传记。
    旧 EvolutionChain 作为兼容 fallback。
    """

    def __init__(
        self,
        evolution_chain: EvolutionChain,
        user_manager: Any | None = None,
        provenance_store: ProvenanceStore | None = None,
    ) -> None:
        self._chain = evolution_chain
        self._user_manager = user_manager
        self._provenance = provenance_store
        self._cache: dict[str, UserBiography] = {}

        # 注册纠正回调：当演化链中的记录被 supersede 时，自动清除相关缓存
        self._chain.register_correction_callback(self._on_correction)

    def _on_correction(self, old_record: Any, _new_record_id: str) -> None:
        """纠正回调：清除受影响用户的传记缓存。"""
        subject = getattr(old_record, "subject", "")
        user_id = getattr(old_record, "subject_user_id", "")
        if user_id and user_id in self._cache:
            del self._cache[user_id]
        elif subject and subject in self._cache:
            del self._cache[subject]

    def get_biography(self, user_id: str) -> UserBiography:
        """获取用户传记（从证据账本实时计算）。

        如果缓存命中则直接返回；优先从 provenance claims 计算，
        没有强归属 claim 时 fallback 到 EvolutionChain。
        """
        if user_id in self._cache:
            return self._cache[user_id]

        if self._provenance is not None:
            claims = self._provenance.get_active_profile_claims(user_id)
            if claims:
                all_claims = self._provenance.get_claims_for_user(user_id, limit=1000)
                bio = self._synthesize_from_claims(user_id, claims, all_claims)
                self._cache[user_id] = bio
                return bio

        # 优先按 user_id 查询（别名系统关联）
        active_records = self._chain.get_active_by_user_id(user_id)
        all_records = self._chain.get_all_by_user_id(user_id)

        # fallback: 按 subject 查询（兼容没有 user_id 的旧数据）
        if not active_records:
            active_records = self._chain.get_active_by_subject(user_id)
            all_records = self._chain.get_all_by_subject(user_id)

        bio = self._synthesize(user_id, active_records, all_records)
        self._cache[user_id] = bio
        return bio

    def invalidate(self, user_id: str) -> None:
        """演化链更新时清除缓存。"""
        self._cache.pop(user_id, None)

    def invalidate_all(self) -> None:
        """清除所有缓存。"""
        self._cache.clear()

    def _synthesize(
        self,
        user_id: str,
        active_records: list[EvolutionRecord],
        all_records: list[EvolutionRecord],
    ) -> UserBiography:
        """从 active 三元组合成传记。"""

        # 按谓语分类
        identity_facts: list[EvolutionRecord] = []
        relationship_facts: list[EvolutionRecord] = []
        preference_facts: list[EvolutionRecord] = []
        other_facts: list[EvolutionRecord] = []

        for r in active_records:
            category = self._categorize(r.predicate)
            if category == "identity":
                identity_facts.append(r)
            elif category == "relationship":
                relationship_facts.append(r)
            elif category == "preference":
                preference_facts.append(r)
            else:
                other_facts.append(r)

        # 生成各部分
        name = self._get_name(user_id, identity_facts)
        identity_anchors = self._build_anchors(identity_facts, user_id)
        relationships = self._build_relationships(relationship_facts, user_id)
        short_bio = self._build_summary(
            name, identity_facts, relationship_facts, preference_facts
        )

        # 统计
        active_count = len(active_records)
        superseded_count = sum(
            1 for r in all_records if r.status == RecordStatus.SUPERSEDED
        )
        uncertain_count = sum(
            1 for r in all_records if r.status == RecordStatus.UNCERTAIN
        )

        return UserBiography(
            user_id=user_id,
            name=name,
            identity_anchors=identity_anchors,
            relationships=relationships,
            short_bio=short_bio,
            source_record_ids=[r.record_id for r in active_records],
            active_fact_count=active_count,
            superseded_fact_count=superseded_count,
            uncertain_fact_count=uncertain_count,
        )

    def _synthesize_from_claims(
        self,
        user_id: str,
        active_claims: list[MemoryClaim],
        all_claims: list[MemoryClaim],
    ) -> UserBiography:
        """从强归属 active claims 合成传记。"""
        identity_claims: list[MemoryClaim] = []
        relationship_claims: list[MemoryClaim] = []
        preference_claims: list[MemoryClaim] = []
        state_claims: list[MemoryClaim] = []

        for claim in active_claims:
            if claim.fact_type == ClaimType.IDENTITY:
                identity_claims.append(claim)
            elif claim.fact_type == ClaimType.RELATIONSHIP:
                relationship_claims.append(claim)
            elif claim.fact_type in (ClaimType.PREFERENCE, ClaimType.HABIT):
                preference_claims.append(claim)
            elif claim.fact_type == ClaimType.LONG_STATE:
                state_claims.append(claim)

        name = self._get_name_from_claims(user_id, identity_claims)
        identity_anchors = self._build_claim_anchors(identity_claims + state_claims)
        relationships = self._build_claim_relationships(relationship_claims)
        short_bio = self._build_claim_summary(
            name, identity_claims, relationship_claims, preference_claims, state_claims
        )

        superseded_count = sum(
            1 for c in all_claims if c.status == ClaimStatus.SUPERSEDED
        )
        uncertain_count = sum(
            1 for c in all_claims if c.status == ClaimStatus.CANDIDATE
        )

        return UserBiography(
            user_id=user_id,
            name=name,
            identity_anchors=identity_anchors,
            relationships=relationships,
            short_bio=short_bio,
            source_claim_ids=[c.claim_id for c in active_claims],
            active_fact_count=len(active_claims),
            superseded_fact_count=superseded_count,
            uncertain_fact_count=uncertain_count,
        )

    # ── 谓语分类 ──

    @staticmethod
    def _categorize(predicate: str) -> str:
        """将谓语分类到记忆类别。"""
        if any(p in predicate for p in _IDENTITY_PREDICATES):
            return "identity"
        if any(p in predicate for p in _RELATIONSHIP_PREDICATES):
            return "relationship"
        if any(p in predicate for p in _PREFERENCE_PREDICATES):
            return "preference"
        return "other"

    # ── 身份锚点 ──

    @staticmethod
    def _build_anchors(
        identity_facts: list[EvolutionRecord], _user_id: str
    ) -> list[str]:
        """从身份类三元组构建身份锚点。"""
        anchors: list[str] = []
        for r in identity_facts:
            # 格式: "住在深圳"、"是程序员"
            anchor = f"{r.predicate}{r.obj}"
            if anchor and anchor not in anchors:
                anchors.append(anchor)
        return anchors[:10]

    # ── 关系信息 ──

    @staticmethod
    def _build_relationships(
        relationship_facts: list[EvolutionRecord], _user_id: str
    ) -> list[dict[str, str]]:
        """从关系类三元组构建关系列表。"""
        relationships: list[dict[str, str]] = []
        seen: set[str] = set()

        for r in relationship_facts:
            key = f"{r.predicate}|{r.obj}"
            if key in seen:
                continue
            seen.add(key)

            relationships.append({
                "target": r.obj,
                "relation": r.predicate,
                "fact_hint": f"{r.predicate}{r.obj}",
            })

        return relationships[:10]

    # ── 传记摘要 ──

    @staticmethod
    def _build_summary(
        name: str,
        identity_facts: list[EvolutionRecord],
        relationship_facts: list[EvolutionRecord],
        preference_facts: list[EvolutionRecord],
    ) -> str:
        """从各类三元组构建传记摘要。"""
        parts: list[str] = []

        # 身份信息（添加主语）
        if identity_facts:
            identity_parts = [f"{name}{r.predicate}{r.obj}" for r in identity_facts[:3]]
            parts.append("；".join(identity_parts))

        # 关系信息（添加主语）
        if relationship_facts:
            rel_parts = [f"{name}{r.predicate}{r.obj}" for r in relationship_facts[:2]]
            parts.append("；".join(rel_parts))

        # 偏好信息（添加主语）
        if preference_facts:
            pref_parts = [f"{name}{r.predicate}{r.obj}" for r in preference_facts[:2]]
            parts.append("；".join(pref_parts))

        return "。".join(parts) if parts else ""

    # ── 工具方法 ──

    @staticmethod
    def _build_claim_anchors(claims: list[MemoryClaim]) -> list[str]:
        anchors: list[str] = []
        for claim in claims:
            value = claim.value or claim.object_value
            if value and value not in anchors:
                anchors.append(value)
        return anchors[:10]

    @staticmethod
    def _build_claim_relationships(claims: list[MemoryClaim]) -> list[dict[str, str]]:
        relationships: list[dict[str, str]] = []
        seen: set[str] = set()
        for claim in claims:
            target = claim.object_value or claim.value
            relation = claim.predicate or "关系"
            key = f"{relation}|{target}"
            if key in seen:
                continue
            seen.add(key)
            relationships.append({
                "target": target,
                "relation": relation,
                "fact_hint": claim.value or f"{relation}{target}",
                "claim_id": claim.claim_id,
            })
        return relationships[:10]

    @staticmethod
    def _build_claim_summary(
        name: str,
        identity_claims: list[MemoryClaim],
        relationship_claims: list[MemoryClaim],
        preference_claims: list[MemoryClaim],
        state_claims: list[MemoryClaim],
    ) -> str:
        parts: list[str] = []
        identity_values = [c.value for c in identity_claims[:3] if c.value]
        state_values = [c.value for c in state_claims[:2] if c.value]
        relationship_values = [c.value for c in relationship_claims[:2] if c.value]
        preference_values = [c.value for c in preference_claims[:2] if c.value]

        if identity_values:
            parts.append(f"{name}{'；'.join(identity_values)}")
        if state_values:
            parts.append(f"近期状态：{'；'.join(state_values)}")
        if relationship_values:
            parts.append(f"关系：{'；'.join(relationship_values)}")
        if preference_values:
            parts.append(f"偏好/习惯：{'；'.join(preference_values)}")
        return "。".join(parts)

    def _get_name_from_claims(
        self, user_id: str, identity_claims: list[MemoryClaim]
    ) -> str:
        if self._user_manager:
            user = self._user_manager.get_user(user_id)
            if user and user.name:
                return user.name
        for claim in identity_claims:
            if claim.predicate in ("叫", "名字") and claim.object_value:
                return claim.object_value
            if claim.subject_label and len(claim.subject_label) <= 10:
                return claim.subject_label
        return user_id

    def _get_name(
        self, user_id: str, identity_facts: list[EvolutionRecord]
    ) -> str:
        """获取用户显示名称。"""
        # 优先从 UnifiedUserManager 获取用户的QQ名
        if self._user_manager:
            user = self._user_manager.get_user(user_id)
            if user and user.name:
                return user.name
        
        # 尝试从 subject_user_id 字段获取真正的用户 ID，再查询名称
        if self._user_manager and identity_facts:
            for r in identity_facts:
                if r.subject_user_id:
                    user = self._user_manager.get_user(r.subject_user_id)
                    if user and user.name:
                        return user.name
        
        # 从 identity_facts 中查找名字（谓语为"叫"、"名字"的记录）
        for r in identity_facts:
            if r.predicate in ("叫", "名字"):
                return r.obj
        
        # 使用 subject 字段（LLM 提取的原始名称），但需要验证是否是合理的名称
        if identity_facts:
            subject = getattr(identity_facts[0], "subject", "")
            # 长度超过 10 个字符的 subject 通常是 LLM 提取的描述，不是真正的名称
            if subject and len(subject) <= 10:
                return subject
        
        return user_id
