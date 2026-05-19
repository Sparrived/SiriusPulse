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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sirius_chat.memory.biography.models import AliasEntry, RelationshipAnchor, UserPersonaCard
from sirius_chat.memory.biography.store import BiographyStore

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════
# 层1 Prompt：从原始群聊消息蒸馏关于目标用户的要点
# ══════════════════════════════════════════════════════════════════

def _build_distill_prompt(
    *,
    user_name: str,
    persona_name: str,
    messages: list[str],
) -> str:
    msgs_text = "\n".join(f"【{i+1}】{m}" for i, m in enumerate(messages))

    return (
        f"你是一个信息提炼助手。以下是一段群聊对话记录，请从中提取关于 {user_name} 的关键信息。\n\n"
        f"人格名称：{persona_name}\n\n"
        f"=== 群聊对话记录 ===\n"
        f"{msgs_text}\n\n"
        f"对话中每条消息都标注了说话人（\"说话人: 内容\"格式）。请提炼 {user_name} 相关的信息，"
        f"每条要点简洁（不超过 40 字），按重要性排列，最多 5 条。\n\n"
        f"提取角度：\n"
        f"1. {user_name} 自己说的话中透露的自身信息\n"
        f"2. 其他人谈论 {user_name} 时透露的信息（含代称/外号指代）\n"
        f"3. {user_name} 与他人的互动中体现的关系信息\n\n"
        f"注意：只提取与 {user_name} 相关的内容，忽略不相关的闲聊。\n\n"
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
    old_bio: str,
    old_anchors: list[str],
    old_relationships: list[RelationshipAnchor],
    points: list[str],
) -> str:
    old_rels_lines: list[str] = []
    for r in old_relationships:
        old_rels_lines.append(f"  - {r.target_name}：{r.fact_hint}")
    old_rels_text = "\n".join(old_rels_lines) if old_rels_lines else "（无）"

    points_text = "\n".join(f"【{i+1}】{p}" for i, p in enumerate(points))

    return (
        f"你是人物传记维护助手。以下是从多段群聊中浓缩的关于 {user_name} 的要点，"
        f"请据此更新你对该用户的认知档案。\n\n"
        f"人格名称：{persona_name}\n\n"
        f"=== 现有的《{user_name}》档案 ===\n"
        f"短期传记：\n{old_bio or '（尚无传记）'}\n\n"
        f"已知锚点：\n{chr(10).join(f'- {a}' for a in old_anchors) if old_anchors else '（无）'}\n\n"
        f"已知关系：\n{old_rels_text}\n\n"
        f"=== 近期的认知要点（从群聊蒸馏而来） ===\n"
        f"{points_text}\n\n"
        f"请综合旧档案和新要点，输出 {user_name} 的更新后完整档案。注意：\n"
        f"- 如果旧信息与新要点冲突，以新要点为准\n"
        f"- 如果新要点没有涉及旧档案中的某条信息，保留旧信息（除非明显过时）\n"
        f"- 传记不超过 500 字\n"
        f"- 锚点每条不超过 20 字，最多 5 条\n\n"
        f"严格输出 JSON：\n"
        f'{{"short_bio": "浓缩传记全文（不超过500字）", '
        f'"identity_anchors": ["锚点1", ...], '
        f'"relationships": [{{"target": "对方名", "fact_hint": "事实描述"}}, ...]}}'
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

    def __init__(self, work_path: Path | str) -> None:
        self._store = BiographyStore(work_path)
        self._cards: dict[str, UserPersonaCard] = {}
        self._alias_index: dict[str, list[AliasEntry]] = {}

        # 启动时加载别名索引
        self._alias_index = self._store.load_alias_index()
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
        """别名消歧解析——三层：群过滤 → 上下文 → 兜底。

        Returns:
            (resolved_user_id | None, confidence 0~1, [alternative_user_ids])
            confidence=0 表示无法确定，alternatives 为所有候选。
        """
        alias_lower = alias.strip().lower()
        entries = self._alias_index.get(alias_lower, [])
        if not entries:
            return None, 0.0, []

        # L1: 按群过滤
        if group_id:
            group_entries = [e for e in entries if group_id in e.groups]
            if not group_entries:
                group_entries = entries
        else:
            group_entries = entries

        if len(group_entries) == 1:
            return group_entries[0].user_id, 0.95, []

        # 多人冲突

        # 信号1: @ 锚定（最强）
        if at_user_id:
            for e in group_entries:
                if e.user_id == at_user_id:
                    return e.user_id, 0.98, [
                        x.user_id for x in group_entries if x.user_id != e.user_id
                    ]

        # 信号2: 最近活跃者
        if recent_speakers:
            for speaker in recent_speakers:
                for e in group_entries:
                    if e.user_id == speaker:
                        return e.user_id, 0.75, [
                            x.user_id for x in group_entries if x.user_id != e.user_id
                        ]

        # 信号3: 权重差距
        sorted_entries = sorted(group_entries, key=lambda e: e.weight, reverse=True)
        if len(sorted_entries) >= 2 and sorted_entries[0].weight > sorted_entries[1].weight * 1.5:
            return sorted_entries[0].user_id, 0.60, [
                x.user_id for x in sorted_entries[1:]
            ]

        # L3: 无法确定
        return None, 0.0, [e.user_id for e in group_entries]

    def bump_alias_weight(self, alias: str, user_id: str, group_id: str) -> None:
        """有人用此别名称呼了此人，提升权重，同别名其他候选衰减。"""
        alias_lower = alias.strip().lower()
        if alias_lower not in self._alias_index:
            return

        for entry in self._alias_index[alias_lower]:
            if entry.user_id == user_id:
                entry.mentioned_count += 1
                entry.weight = min(10.0, entry.weight + 0.3)
                entry.last_seen_at = _now_iso()
                if group_id not in entry.groups:
                    entry.groups.append(group_id)

        for entry in self._alias_index[alias_lower]:
            if entry.user_id != user_id:
                entry.weight = max(0.5, entry.weight * 0.98)

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
        provider_async: Any,
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
        from sirius_chat.providers.base import GenerationRequest

        prompt = _build_distill_prompt(
            user_name=card.name,
            persona_name=persona_name,
            messages=card.pending_messages,
        )
        request = GenerationRequest(
            model=model_name,
            system_prompt="你是信息提炼助手。严格输出 JSON。",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1024,
            purpose="biography_distill",
        )

        try:
            raw = await provider_async.generate_async(request)
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

        # 注册蒸馏发现的别名
        for alias in result.get("discovered_aliases", []):
            if alias and str(alias).strip():
                self._register_alias(
                    str(alias).strip(), user_id, card.name, "", source="llm_discovery"
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
        provider_async: Any,
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
        from sirius_chat.providers.base import GenerationRequest

        prompt = _build_update_prompt(
            user_name=card.name,
            persona_name=persona_name,
            old_bio=card.short_bio,
            old_anchors=card.identity_anchors,
            old_relationships=card.relationships,
            points=card.distilled_points,
        )
        request = GenerationRequest(
            model=model_name,
            system_prompt="你是人物传记维护助手。严格输出 JSON。",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=1024,
            purpose="biography_update",
        )

        try:
            raw = await provider_async.generate_async(request)
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
        card.identity_anchors = [
            str(a) for a in result.get("identity_anchors", []) if a
        ][:5]
        card.relationships = [
            RelationshipAnchor(
                target_name=r.get("target", ""),
                fact_hint=r.get("fact_hint", ""),
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

    def _register_alias(
        self,
        alias: str,
        user_id: str,
        user_name: str,
        group_id: str = "",
        source: str = "napcat",
    ) -> None:
        """注册或更新一个别名条目。"""
        alias_lower = alias.strip().lower()
        if not alias_lower or len(alias_lower) < 2:
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

        # 新建条目
        now = _now_iso()
        self._alias_index[alias_lower].append(
            AliasEntry(
                user_id=user_id,
                user_name=user_name,
                groups=[group_id] if group_id else [],
                first_seen_at=now,
                last_seen_at=now,
                source=source,
            )
        )
        self._store.save_alias_index(self._alias_index)

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
