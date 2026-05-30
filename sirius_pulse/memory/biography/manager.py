"""人物传记管理器 — 全局跨群人物认知管理。

两层凝练架构：
  层1（蒸馏）：原始群聊消息 → LLM 提取关于目标用户的要点 → distilled_points
  层2（传记更新）：足量 distilled_points → LLM 重写 UserPersonaCard

职责：
- 加载/保存全局 UserPersonaCard（一个 user_id 一张卡）
- 维护全局别名速查表（index.json，一对多）
- 别名消歧（群上下文 + 权重 + 活跃度）
- 攒原始消息 → 蒸馏 → 传记更新
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.memory.biography.models import AliasEntry, RelationshipAnchor, UserPersonaCard
from sirius_pulse.memory.biography.store import BiographyStore

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


# ══════════════════════════════════════════════════════════════════
# 层1 Prompt：从原始群聊消息蒸馏关于目标用户的要点
# ══════════════════════════════════════════════════════════════════

def _build_distill_prompt(
    *,
    user_name: str,
    persona_name: str,
    persona_aliases: set[str],
    messages: list[str],
) -> str:
    msgs_text = "\n".join(f"【{i+1}】{m}" for i, m in enumerate(messages))

    # 构建人格身份名称列表，用于提示 LLM 避免混淆
    all_persona_names = [persona_name] + sorted(persona_aliases)
    persona_names_str = "、".join(f"「{n}」" for n in all_persona_names)

    return (
        f"你是一个信息提炼助手。以下是一段群聊对话记录，请从中提取关于 {user_name} 的关键信息。\n\n"
        f"观察者 AI 人格名称：{persona_name}\n\n"
        f"=== 群聊对话记录 ===\n"
        f"{msgs_text}\n\n"
        f"对话中每条消息都标注了说话人（\"说话人: 内容\"格式）。请提炼 {user_name} 相关的信息，"
        f"每条要点简洁（不超过 40 字），按重要性排列，最多 5 条。\n\n"
        f"提取角度：\n"
        f"1. {user_name} 自己说的话中透露的自身信息\n"
        f"2. 其他人谈论 {user_name} 时透露的信息（含代称/外号指代）\n"
        f"3. {user_name} 与他人的互动中体现的关系信息\n\n"
        f"注意：只提取与 {user_name} 相关的内容，忽略不相关的闲聊。\n\n"
        f"关于别名发现（discovered_aliases）—— 以下名称属于观察者 AI 自身，"
        f"绝对不是 {user_name} 的别名：\n"
        f"{persona_names_str}\n"
        f"- 当有人在消息中说出以上名称时，那是在呼叫或提及观察者 AI，不是在自称\n"
        f"- 如果你看到类似「用户A: {persona_name}，帮我画个图」这样的消息，"
        f"这是用户A在呼叫观察者 AI，{persona_name} 不是用户A的别名\n"
        f"- 仅当对话中明确用某个称呼来指代 {user_name} 本人时，才将其列入别名\n"
        f"- 不要把对话中提及的其他人的名字当作别名\n"
        f"- 如果不确定，宁可不列，也不要错误注册\n\n"
        f"如果没有发现新的别名，discovered_aliases 留空数组即可\n\n"
        f"严格输出 JSON：\n"
        f'{{"points": ["要点1", "要点2", ...], "discovered_aliases": ["别名1", ...]}}'
    )


# ══════════════════════════════════════════════════════════════════
# 层2 Prompt：从蒸馏要点重写完整传记卡
# ══════════════════════════════════════════════════════════════════

def _build_update_prompt(
    *,
    user_name: str,
    persona_name: str,
    persona_aliases: set[str],
    old_bio: str,
    old_anchors: list[str],
    old_aliases: list[str],
    old_relationships: list[RelationshipAnchor],
    points: list[str],
) -> str:
    old_rels_lines: list[str] = []
    for r in old_relationships:
        parts = [f"  - {r.target_name}"]
        if r.relation:
            parts.append(f"（关系：{r.relation}）")
        parts.append(f"：{r.fact_hint}")
        if r.target_user_id:
            parts.append(f" [用户ID={r.target_user_id}]")
        if r.last_mentioned_at:
            parts.append(f" [最近提及={r.last_mentioned_at[:10]}]")
        old_rels_lines.append("".join(parts))
    old_rels_text = "\n".join(old_rels_lines) if old_rels_lines else "（无）"

    points_text = "\n".join(f"【{i+1}】{p}" for i, p in enumerate(points))

    old_aliases_text = "、".join(old_aliases) if old_aliases else "（无）"

    # 构建人格身份名称列表
    all_persona_names = [persona_name] + sorted(persona_aliases)
    persona_names_str = "、".join(f"「{n}」" for n in all_persona_names)

    return (
        f"你是人物传记维护助手。以下是从多段群聊中浓缩的关于 {user_name} 的要点，"
        f"请据此更新你对该用户的认知档案。\n\n"
        f"观察者 AI 名称：{persona_name}\n"
        f"{user_name} 是群聊中的真实人类用户，不是 AI，也不拥有人格名称。\n\n"
        f"重要：以下名称属于观察者 AI 自身，{user_name} 不是这些名称的主人：\n"
        f"{persona_names_str}\n"
        f"如果蒸馏要点中提到「{persona_name} 称 xxx」或「xxx 称 {user_name} 为 {persona_name}」，"
        f"那是观察者 AI 与用户的互动，不代表 {user_name} 就是 {persona_name}。\n\n"
        f"=== 现有的《{user_name}》档案 ===\n"
        f"短期传记：\n{old_bio or '（尚无传记）'}\n\n"
        f"已知别名：{old_aliases_text}\n\n"
        f"已知锚点：\n{chr(10).join(f'- {a}' for a in old_anchors) if old_anchors else '（无）'}\n\n"
        f"已知关系：\n{old_rels_text}\n\n"
        f"=== 近期的认知要点（从群聊蒸馏而来） ===\n"
        f"{points_text}\n\n"
        f"请综合旧档案和新要点，输出 {user_name} 的更新后完整档案。注意：\n"
        f"- 如果旧信息与新要点冲突，以新要点为准\n"
        f"- 如果新要点没有涉及旧档案中的某条信息，保留旧信息（除非明显过时）\n"
        f"- 传记不超过 500 字\n"
        f"- 锚点每条不超过 20 字，最多 5 条\n"
        f"- 传记中不应将 {user_name} 描述为 AI、bot、或拥有「人格名」的非人类身份\n"
        f"- affinity_score 反映 {user_name} 对观察者 AI（{persona_name}）的整体态度：\n"
        f"   1.0=非常友好/亲近, 0.5=比较友好, 0.0=中立/未知, -0.5=冷淡/疏远, -1.0=敌对/厌恶\n\n"
        f"严格输出 JSON：\n"
        f'{{"short_bio": "浓缩传记全文（不超过500字）", '
        f'"aliases": ["别名1", ...], '
        f'"identity_anchors": ["锚点1", ...], '
        f'"affinity_score": 0.5, '
        f'"relationships": [{{'
        f'"target": "对方名字", '
        f'"target_user_id": "对方user_id（如已知）", '
        f'"relation": "关系类型", '
        f'"fact_hint": "事实描述（含判断依据）", '
        f'"mentioned_count": 互动次数, '
        f'"last_mentioned_at": "最后提及的ISO时间"}}]}}'
    )


def _parse_update_result(raw: str) -> dict[str, Any] | None:
    """解析 LLM 传记更新结果，失败返回 None。"""
    raw = raw.strip()
    if not raw:
        return None
    # 尝试截取 JSON 块
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("传记更新响应 JSON 解析失败: %.100s...", raw)
        return None


class BiographyManager:
    """管理所有用户的全局传记卡（跨群收敛）。"""

    def __init__(
        self,
        work_path: Path | str,
        persona_name: str = "",
        persona_aliases: list[str] | None = None,
    ) -> None:
        self._store = BiographyStore(work_path)

        # 记录人格自身身份，用于隔离 bot 名称不被注册为其他用户的别名
        self._persona_name = persona_name.strip().lower()
        self._persona_aliases = {a.strip().lower() for a in (persona_aliases or []) if a.strip()}

        self._cards: dict[str, UserPersonaCard] = {}
        self._alias_index: dict[str, list[AliasEntry]] = {}

        # 启动时加载别名索引
        self._alias_index = self._store.load_alias_index()
        # 启动时自动清理已被人格身份名称污染的别名条目
        cleaned = self.cleanup_polluted_aliases()
        if cleaned:
            logger.info("传记管理器启动时自动清理了 %d 个人格身份别名污染", cleaned)
        # 启动时对所有别名执行时间衰减和低置信度清理
        decayed_removed = self.decay_all_aliases()
        if decayed_removed:
            logger.info("传记管理器启动时衰减+清理了 %d 个别名条目", decayed_removed)
        # 延迟加载卡片（按需）

    # ── 别名消歧（三层） ─────────────────────────────────────

    def resolve_alias(
        self,
        alias: str,
        *,
        group_id: str = "",
        recent_speakers: list[str] | None = None,
        at_user_id: str | None = None,
    ) -> tuple[str | None, float, list[str]]:
        """别名消歧解析——基于别名条目真实置信度的三层信号。

        消歧策略：
          L1：群过滤 → 单候选则返回其置信度
          L2：多人冲突 → @锚定 / 最近活跃 / 置信度差距 三种信号
          L3：无法确定 → confidence=0.0

        Returns:
            (resolved_user_id | None, confidence 0~1, [alternative_user_ids])
            confidence=0 表示无法确定。
        """
        alias_lower = alias.strip().lower()
        entries = self._alias_index.get(alias_lower, [])
        if not entries:
            return None, 0.0, []

        # 先对该别名的所有条目执行时间衰减（轻量操作）
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

        # 多人冲突：按置信度排序
        sorted_entries = sorted(group_entries, key=lambda e: e.confidence, reverse=True)

        # 信号1: @ 锚定（强证据）
        if at_user_id:
            for e in group_entries:
                if e.user_id == at_user_id:
                    conf = min(0.98, e.confidence + 0.30)
                    return e.user_id, conf, [
                        x.user_id for x in group_entries if x.user_id != e.user_id
                    ]

        # 信号2: 最近活跃者（中等证据）
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

        # 信号3: 置信度显著领先（>1.5x）
        if len(sorted_entries) >= 2:
            if sorted_entries[0].confidence > sorted_entries[1].confidence * 1.5:
                conf = min(0.70, sorted_entries[0].confidence)
                return sorted_entries[0].user_id, conf, [
                    x.user_id for x in sorted_entries[1:]
                ]

        # L3: 无法确定
        return None, 0.0, [e.user_id for e in group_entries]

    def bump_alias_weight(self, alias: str, user_id: str, group_id: str) -> None:
        """有人用此别名称呼了此人：增量 mentioned_count，重算置信度。"""
        alias_lower = alias.strip().lower()
        if alias_lower not in self._alias_index:
            return

        for entry in self._alias_index[alias_lower]:
            if entry.user_id == user_id:
                entry.mentioned_count += 1
                entry.confidence = AliasEntry.compute_confidence(
                    entry.mentioned_count, entry.source,
                )
                entry.last_seen_at = _now_iso()
                if group_id not in entry.groups:
                    entry.groups.append(group_id)

        self._store.save_alias_index(self._alias_index)

    def get_aliases_for_group(self, group_id: str) -> dict[str, str]:
        """获取当前群相关的别名速查表 alias → user_name（仅当前群有记录的）。"""
        result: dict[str, str] = {}
        for alias, entries in self._alias_index.items():
            for e in entries:
                if group_id in e.groups:
                    result[alias] = e.user_name
                    break
        return result

    # ── 两层凝练：feed_messages → maybe_distill → maybe_update_biography ──

    def _ensure_card(self, user_id: str, name: str = "") -> UserPersonaCard:
        """获取或创建用户传记卡。"""
        if user_id not in self._cards:
            card = self._store.load_card(user_id)
            if card is not None:
                self._cards[user_id] = card
            else:
                self._cards[user_id] = UserPersonaCard(user_id=user_id, name=name or user_id)
        card = self._cards[user_id]
        if name and not card.name:
            card.name = name
        return card

    def feed_messages(
        self,
        user_id: str,
        name: str,
        group_id: str,
        messages: list[str],
        discovered_aliases: list[str] | None = None,
    ) -> None:
        """把一批原始群聊消息追加到 pending_messages 队列。零 LLM 零 embedding。"""
        card = self._ensure_card(user_id, name)
        if not card.name:
            card.name = name

        # 追加消息（截断到最近 ~2000 字）
        card.pending_messages.extend(messages)
        total_chars = sum(len(m) for m in card.pending_messages)
        while total_chars > 2000 and len(card.pending_messages) > 1:
            card.pending_messages.pop(0)
            total_chars = sum(len(m) for m in card.pending_messages)

        card.pending_message_count += len(messages)

        # 注册别名
        if discovered_aliases:
            for alias in discovered_aliases:
                self._register_alias(alias, user_id, name, group_id, source="llm_discovery")

        self._store.save_card(card)

    # ── 层1：蒸馏 ──

    async def maybe_distill(
        self,
        user_id: str,
        *,
        persona_name: str,
        brain: Any,
        model_name: str,
    ) -> bool:
        """如果攒的原始消息足够，调用 LLM 蒸馏为关于该用户的要点。

        触发条件：pending_messages >= 5 或距上次蒸馏 >= 8h 且有消息。

        Returns:
            True 表示蒸馏完成并产生了新要点。
        """
        card = self._ensure_card(user_id)
        if not card.pending_messages:
            return False

        # 触发条件
        should_distill = len(card.pending_messages) >= 5
        if not should_distill and card.last_distill_at:
            try:
                last_dt = datetime.fromisoformat(card.last_distill_at.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - last_dt >= timedelta(hours=8):
                    should_distill = True
            except (ValueError, TypeError):
                should_distill = True

        if not should_distill:
            return False

        # 构建蒸馏 prompt → LLM 调用
        from sirius_pulse.core.brain import RawRequest

        prompt = _build_distill_prompt(
            user_name=card.name,
            persona_name=persona_name,
            persona_aliases=self._persona_aliases,
            messages=card.pending_messages,
        )
        raw_request = RawRequest(
            model=model_name,
            system_prompt="你是信息提炼助手。严格输出 JSON。",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1024,
            purpose="biography_distill",
            response_format={"type": "json_object"},
        )

        try:
            raw = await brain.raw_call(raw_request)
        except Exception as exc:
            logger.warning("传记蒸馏 LLM 调用失败 user=%s: %s", user_id, exc)
            return False

        result = _parse_update_result(raw)
        if result is None:
            return False

        # 将蒸馏要点追加到 distilled_points
        new_points = [str(p).strip() for p in result.get("points", []) if p and str(p).strip()]
        if not new_points:
            # 蒸馏没有产出，但还是清空 pending 防止堆积
            card.pending_messages = []
            card.pending_message_count = 0
            card.last_distill_at = _now_iso()
            self._store.save_card(card)
            return False

        card.distilled_points.extend(new_points)
        card.pending_messages = []
        card.pending_message_count = 0
        card.last_distill_at = _now_iso()

        # 注册蒸馏发现的别名（人格自身名称 + 已知用户主名冲突过滤）
        for alias in result.get("discovered_aliases", []):
            if alias and str(alias).strip():
                alias_clean = str(alias).strip()
                if self._alias_is_persona_identity(alias_clean.lower()):
                    logger.debug(
                        "拒绝LLM别名: %s 是人格自身名称，不是 %s 的别名",
                        alias_clean, card.name,
                    )
                    continue
                self._register_alias(
                    alias_clean, user_id, card.name, "", source="llm_discovery"
                )

        self._store.save_card(card)
        logger.info(
            "传记蒸馏完成 user=%s name=%s new_points=%d total_points=%d",
            user_id, card.name, len(new_points), len(card.distilled_points),
        )
        return True

    # ── 层2：传记更新 ──

    async def maybe_update_biography(
        self,
        user_id: str,
        *,
        persona_name: str,
        brain: Any,
        model_name: str,
    ) -> bool:
        """如果蒸馏要点攒够了，调用 LLM 重写传记卡。

        触发条件：distilled_points >= 3 或距上次更新 >= 24h 且有新要点。

        Returns:
            True 表示传记被更新了。
        """
        card = self._ensure_card(user_id)
        if not card.distilled_points:
            return False

        # 触发条件
        should_update = len(card.distilled_points) >= 3
        if not should_update and card.last_updated_at:
            try:
                last_dt = datetime.fromisoformat(card.last_updated_at.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - last_dt >= timedelta(hours=24):
                    should_update = True
            except (ValueError, TypeError):
                should_update = True

        if not should_update:
            return False

        # 构建传记更新 prompt → LLM 调用
        from sirius_pulse.core.brain import RawRequest

        prompt = _build_update_prompt(
            user_name=card.name,
            persona_name=persona_name,
            persona_aliases=self._persona_aliases,
            old_bio=card.short_bio,
            old_anchors=card.identity_anchors,
            old_aliases=card.aliases,
            old_relationships=card.relationships,
            points=card.distilled_points,
        )
        raw_request = RawRequest(
            model=model_name,
            system_prompt="你是人物传记维护助手。严格输出 JSON。",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=1024,
            purpose="biography_update",
            response_format={"type": "json_object"},
        )

        try:
            raw = await brain.raw_call(raw_request)
        except Exception as exc:
            logger.warning("传记更新 LLM 调用失败 user=%s: %s", user_id, exc)
            return False

        result = _parse_update_result(raw)
        if result is None:
            return False

        # 更新传记卡
        card.short_bio = str(result.get("short_bio", card.short_bio))[
            : card.bio_token_budget * 4
        ]
        # 更新别名（保留旧别名 + LLM 输出中新发现的别名，去重）
        new_aliases = [str(a) for a in result.get("aliases", []) if a]
        existing_aliases = set(card.aliases)
        for alias in new_aliases:
            if alias not in existing_aliases:
                card.aliases.append(alias)
                existing_aliases.add(alias)

        # 读取 LLM 输出的亲和力分数，用 EMA 平滑（alpha=0.3，不完全信任 LLM）
        raw_affinity = result.get("affinity_score")
        if raw_affinity is not None:
            try:
                llm_val = max(-1.0, min(1.0, float(raw_affinity)))
                # EMA 混合：保留 70% 旧值 + 30% LLM 新值，避免单次糟糕输出污染
                card.affinity_score = round(card.affinity_score * 0.7 + llm_val * 0.3, 4)
            except (ValueError, TypeError):
                pass  # LLM 输出非法，保留旧值
        # LLM 未输出时，保留原有值不调整

        card.identity_anchors = [
            str(a) for a in result.get("identity_anchors", []) if a
        ][:5]
        card.relationships = [
            RelationshipAnchor(
                target_name=r.get("target", ""),
                target_user_id=r.get("target_user_id", ""),
                relation=r.get("relation", ""),
                fact_hint=r.get("fact_hint", ""),
                mentioned_count=int(r.get("mentioned_count", 1)),
                last_mentioned_at=r.get("last_mentioned_at", ""),
            )
            for r in result.get("relationships", [])[:5]
        ]
        card.distilled_points = []
        card.last_updated_at = _now_iso()

        self._store.save_card(card)
        logger.info(
            "传记已更新 user=%s name=%s anchors=%d rels=%d",
            user_id,
            card.name,
            len(card.identity_anchors),
            len(card.relationships),
        )
        return True

    # ── 别名注册（内部）────────────────────────────────────────

    def _alias_is_persona_identity(self, alias_lower: str) -> bool:
        """判断一个别名是否属于人格自身的身份名称（人格名 + 别名列表）。"""
        return bool(self._persona_name and alias_lower == self._persona_name) or (
            alias_lower in self._persona_aliases
        )

    def _register_alias(
        self,
        alias: str,
        user_id: str,
        user_name: str,
        group_id: str = "",
        source: str = "napcat",
    ) -> None:
        """注册或更新一个别名条目。

        拥有四层防御：
        1. 人格身份隔离：如果别名是 bot 人格自身名称，拒绝注册到任何其他用户
        2. LLM 来源冲突校验：拒绝注册为已知其他用户的主名
        3. 子串冲突校验：别名包含已知其他用户的有效名（去标点后>=2字）时跳过
        4. 标准别名注册/更新（新建时按来源设置初始置信度）
        """
        alias_lower = alias.strip().lower()
        if not alias_lower or len(alias_lower) < 2:
            return

        # 防御1：人格身份隔离——人格自身的名字和别名不能被注册到任何其他用户
        if self._alias_is_persona_identity(alias_lower):
            logger.debug(
                "拒绝别名注册: %s 是人格自身名称，不能注册为 %s(%s) 的别名",
                alias, user_name, user_id,
            )
            return

        # 防御2（LLM 来源）：校验是否与已知用户的主要名字冲突
        if source == "llm_discovery":
            for uid, card in self._cards.items():
                if uid == user_id:
                    continue
                if card.name and card.name.lower() == alias_lower:
                    logger.debug(
                        "拒绝LLM别名注册: %s 已是 %s 的主要名，不是 %s 的别名",
                        alias, card.name, user_id,
                    )
                    return

        # 防御3：别名包含已知人名作为子串时跳过（如"前前前世哥哥"包含"前前前世"）
        # 先去除非文字符号获取有效名，过短（<2字）的不参与检查
        for uid, card in self._cards.items():
            if uid == user_id:
                continue
            cleaned = re.sub(r'[^\w]', '', card.name or '', flags=re.UNICODE)
            if len(cleaned) < 2:
                continue
            if cleaned.lower() in alias_lower:
                logger.debug(
                    "拒绝别名注册: %s 包含已知用户 %s(%s) 的名 '%s'",
                    alias, card.name, uid, cleaned,
                )
                return

        if alias_lower not in self._alias_index:
            self._alias_index[alias_lower] = []

        # 查找是否已有此 user 的条目
        for entry in self._alias_index[alias_lower]:
            if entry.user_id == user_id:
                entry.last_seen_at = _now_iso()
                if group_id and group_id not in entry.groups:
                    entry.groups.append(group_id)
                if source != "napcat":
                    entry.source = source
                self._store.save_alias_index(self._alias_index)
                return

        # 新建条目：按来源设置初始置信度
        now = _now_iso()
        initial_confidence = AliasEntry.compute_confidence(1, source)
        self._alias_index[alias_lower].append(
            AliasEntry(
                user_id=user_id,
                user_name=user_name,
                groups=[group_id] if group_id else [],
                mentioned_count=1,
                confidence=initial_confidence,
                first_seen_at=now,
                last_seen_at=now,
                source=source,
            )
        )
        self._store.save_alias_index(self._alias_index)

    # ── 衰减与清理 ──

    def _decay_alias_key(self, alias_lower: str) -> None:
        """对单个别名的所有条目执行时间衰减。

        每过去一天，置信度衰减 5%。衰减后低于阈值的条目被移除。
        """
        entries = self._alias_index.get(alias_lower)
        if not entries:
            return

        survivor: list[AliasEntry] = []
        for entry in entries:
            days = _days_since(entry.last_seen_at)
            entry.confidence = AliasEntry.apply_time_decay(entry.confidence, days)
            if entry.confidence >= AliasEntry.DECAY_THRESHOLD:
                survivor.append(entry)
            else:
                logger.debug(
                    "别名衰减移除: %s → %s(%s) 置信度%.4f < 阈值%.2f",
                    alias_lower, entry.user_name, entry.user_id,
                    entry.confidence, AliasEntry.DECAY_THRESHOLD,
                )

        if len(survivor) != len(entries):
            if survivor:
                self._alias_index[alias_lower] = survivor
            else:
                del self._alias_index[alias_lower]
            self._store.save_alias_index(self._alias_index)

    def decay_all_aliases(self) -> int:
        """对所有别名执行时间衰减和低置信度清理。

        Returns:
            被移除的条目总数。
        """
        total_removed = 0
        for key in list(self._alias_index.keys()):
            entries = self._alias_index[key]
            survivor: list[AliasEntry] = []
            for entry in entries:
                days = _days_since(entry.last_seen_at)
                entry.confidence = AliasEntry.apply_time_decay(entry.confidence, days)
                if entry.confidence >= AliasEntry.DECAY_THRESHOLD:
                    survivor.append(entry)
                else:
                    total_removed += 1
                    logger.debug(
                        "批量衰减移除: %s → %s(%s) 置信度%.4f < 阈值%.2f",
                        key, entry.user_name, entry.user_id,
                        entry.confidence, AliasEntry.DECAY_THRESHOLD,
                    )

            if survivor:
                self._alias_index[key] = survivor
            else:
                del self._alias_index[key]

        if total_removed > 0:
            self._store.save_alias_index(self._alias_index)

        return total_removed

    # ── 查询 ──────────────────────────────────────────────────

    def get_card(self, user_id: str) -> UserPersonaCard | None:
        """获取用户传记卡（按需从磁盘加载）。"""
        if user_id not in self._cards:
            card = self._store.load_card(user_id)
            if card is not None:
                self._cards[user_id] = card
        return self._cards.get(user_id)

    def get_cards_for_users(self, user_ids: list[str]) -> list[UserPersonaCard]:
        """批量获取用户传记卡。"""
        return [c for uid in user_ids if (c := self.get_card(uid)) is not None]

    def cleanup_polluted_aliases(self) -> int:
        """清理别名索引中被人格身份名称污染的条目。

        移除所有 key 为人格自身名称（或别名）的别名条目，
        因为这些名称只属于观察者 bot，不应注册到任何其他用户。

        Returns:
            被清理的别名 key 数量。
        """
        if not self._persona_name:
            return 0

        persona_keys = {self._persona_name} | self._persona_aliases
        cleaned = 0
        for key in list(self._alias_index.keys()):
            if key in persona_keys:
                del self._alias_index[key]
                cleaned += 1

        if cleaned > 0:
            self._store.save_alias_index(self._alias_index)
            logger.info("已清理 %d 个人格身份污染别名条目", cleaned)

        return cleaned

    def register_alias_from_profile(
        self, user_id: str, name: str, aliases: list[str], group_id: str
    ) -> None:
        """从外部 UserProfile 注册别名（NapCatAdapter 调用）。"""
        if name:
            self._register_alias(name, user_id, name, group_id, source="napcat")
        for alias in aliases:
            if alias and alias.lower() != (name or "").lower():
                self._register_alias(alias, user_id, name, group_id, source="napcat")


__all__ = ["BiographyManager"]
