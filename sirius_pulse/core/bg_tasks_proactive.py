"""主动消息相关后台任务。

包含主动检查、开发者聊天、话题选择等功能。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sirius_pulse.core.delayed_response_queue import _parse_iso
from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.core.prompt_factory import TAG_GLOSSARY, PromptFactory

if TYPE_CHECKING:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

logger = logging.getLogger(__name__)


class ProactiveTasks:
    """主动消息相关任务组件。"""

    def __init__(self, engine: _EmotionalGroupChatEngineBase) -> None:
        self._engine = engine

    async def proactive_checker(self) -> None:
        """Periodically check proactive triggers for all active groups."""
        engine = self._engine
        import random

        interval = engine.config.get("proactive_check_interval_seconds", 60)
        while engine._bg_running:
            await asyncio.sleep(interval)
            # 动态重新读取体验配置，使WebUI的proactive_enabled立即生效
            if not self._load_proactive_global_enabled():
                continue
            group_ids = list(engine._group_last_message_at.keys())
            for i, group_id in enumerate(group_ids):
                # 群间添加随机抖动（0~15秒），避免多群同时触发
                if i > 0:
                    await asyncio.sleep(random.uniform(0, 15))
                try:
                    result = await self.proactive_check(group_id)
                    if result and result.get("reply"):
                        engine._log_inner_thought("群里安静了好一会儿，我主动打破沉默吧...")
                except Exception as exc:
                    logger.warning("Proactive check failed for %s: %s", group_id, exc)

    async def proactive_check(
        self,
        group_id: str,
        *,
        _now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Check if proactive trigger should fire for a group."""
        engine = self._engine
        if not engine.is_proactive_enabled(group_id):
            return None

        last_at = engine._group_last_message_at.get(group_id)
        group_profile = engine.semantic_memory.ensure_group_profile(group_id)

        trigger = engine.proactive_trigger.check(
            group_id,
            last_message_at=last_at,
            group_atmosphere={
                "valence": (
                    getattr(group_profile.atmosphere_history[-1], "group_valence", 0.0)
                    if group_profile.atmosphere_history
                    else 0.0
                ),
            },
            _now=_now,
        )
        if not trigger:
            return None

        # Guard: do not send another proactive message if nobody replied to the last one.
        last_proactive_iso = engine._last_proactive_at.get(group_id)
        if last_proactive_iso:
            last_proactive_dt = _parse_iso(last_proactive_iso)
            last_msg_dt = _parse_iso(last_at) if last_at else None
            if last_proactive_dt and (last_msg_dt is None or last_proactive_dt > last_msg_dt):
                return None

        # Check conversation gap readiness before proactive insertion
        recent = engine._helpers.get_recent_messages(group_id, n=6)
        rhythm = engine.rhythm_analyzer.analyze(group_id, recent)
        if rhythm.turn_gap_readiness < engine.expressiveness.proactive_gap_threshold:
            # Conversation is in full flow, don't interrupt with proactive
            return None

        # Record proactive trigger timestamp
        now_iso = (_now if _now is not None else datetime.now(timezone.utc)).isoformat()
        engine._last_proactive_at[group_id] = now_iso
        engine.proactive_trigger._last_proactive[group_id] = now_iso
        engine._save_proactive_state()

        # Generate proactive message
        bundle = self._build_proactive_prompt(trigger, group_id)
        style = engine.style_adapter.adapt(pace="steady")
        # Use ContextAssembler to build full messages with diary RAG + XML history
        diary_top_k = engine.config.get("diary_top_k", 5)
        diary_token_budget = engine.config.get("diary_token_budget", 800)
        msgs, ca_breakdown = engine.context_assembler.build_messages_with_breakdown(
            group_id=group_id,
            current_query=bundle.user_content or "...",
            system_prompt=bundle.system_prompt,
            search_query=bundle.user_content or "",
            diary_top_k=diary_top_k,
            diary_token_budget=diary_token_budget,
            include_pending=True,
        )
        system_prompt = msgs[0]["content"]
        messages = msgs[1:]

        # Merge assembler breakdown into response-assembler breakdown
        token_breakdown = bundle.token_breakdown.to_dict() if bundle.token_breakdown else {}
        for key, val in ca_breakdown.items():
            if key == "diary":
                token_breakdown["memory"] = token_breakdown.get("memory", 0) + val
            else:
                token_breakdown[key] = token_breakdown.get(key, 0) + val

        raw_reply = await engine.brain.generate_text(
            system_prompt,
            messages,
            group_id,
            style_params=style,
            post_process=True,
        )
        reply = raw_reply.strip()

        await engine.event_bus.emit(
            SessionEvent(
                type=SessionEventType.PROACTIVE_RESPONSE_TRIGGERED,
                data={
                    "group_id": group_id,
                    "trigger_type": trigger["trigger_type"],
                    "reply": reply,
                },
            )
        )

        clean_reply = reply.strip()

        return {
            "strategy": "proactive",
            "trigger_type": trigger["trigger_type"],
            "reply": clean_reply,
        }

    # ------------------------------------------------------------------
    # Developer proactive private-chat memory conversations
    # ------------------------------------------------------------------

    async def proactive_developer_chat_checker(self) -> None:
        """Periodically generate proactive memory-oriented chats for developers."""
        engine = self._engine
        interval = engine.config.get("proactive_developer_chat_interval_seconds", 1800)
        min_silence = engine.config.get("proactive_developer_min_silence_seconds", 120)
        while engine._bg_running:
            await asyncio.sleep(interval)
            now = datetime.now(timezone.utc).timestamp()
            for group_id in list(engine._developer_private_groups):
                try:
                    if not self._should_chat_with_developer(group_id, now, min_silence, interval):
                        continue
                    reply = await self._generate_developer_chat(group_id)
                    if reply:
                        clean_dev = reply.strip()
                        engine._pending_developer_chats.setdefault(group_id, []).append(clean_dev)
                        engine._last_developer_chat_at[group_id] = now
                        engine._log_inner_thought("突然想跟开发者聊聊，发了条消息过去～")
                        await engine.event_bus.emit(
                            SessionEvent(
                                type=SessionEventType.DEVELOPER_CHAT_TRIGGERED,
                                data={
                                    "group_id": group_id,
                                    "reply": clean_dev,
                                },
                            )
                        )
                except Exception as exc:
                    logger.warning("Developer chat check failed for %s: %s", group_id, exc)

    def _should_chat_with_developer(
        self,
        group_id: str,
        now: float,
        min_silence: float,
        interval: float,
    ) -> bool:
        """Check whether it's appropriate to proactively chat with a developer."""
        engine = self._engine
        # Active hours check
        start = engine.config.get("proactive_active_start_hour", 8)
        end = engine.config.get("proactive_active_end_hour", 23)
        local_hour = datetime.fromtimestamp(now).hour
        if not (start <= local_hour < end):
            return False

        # Respect silence since last message
        last_msg_iso = engine._group_last_message_at.get(group_id)
        if last_msg_iso:
            last_msg_dt = _parse_iso(last_msg_iso)
            if last_msg_dt and (now - last_msg_dt.timestamp()) < min_silence:
                return False

        # Respect interval since last proactive developer chat
        last_chat = engine._last_developer_chat_at.get(group_id, 0)
        if now - last_chat < interval:
            return False

        # CRITICAL: Do not send a new proactive message if the developer
        # has not replied to the last one.
        if last_chat > 0:
            if last_msg_iso:
                last_msg_dt = _parse_iso(last_msg_iso)
                if last_msg_dt and last_msg_dt.timestamp() <= last_chat:
                    return False
            else:
                return False

        return True

    async def _generate_developer_chat(self, group_id: str) -> str | None:
        """Generate a memory-oriented proactive message for a developer."""
        engine = self._engine
        user_id = group_id.replace("private_", "")

        topic = self._pick_developer_chat_topic(group_id, user_id, None)
        if not topic:
            return None

        from sirius_pulse.core.prompt_factory import PromptFactory

        sections = PromptFactory.build_developer_chat_sections("", topic, None)

        system_prompt = "\n\n".join(sections)
        messages = [{"role": "user", "content": "（你决定主动开口）"}]
        style = engine.style_adapter.adapt(pace="steady")

        raw_reply = await engine.brain.generate_text(
            system_prompt,
            messages,
            group_id,
            style_params=style,
            post_process=True,
        )
        reply = raw_reply.strip()

        clean_reply = reply.strip()

        return clean_reply or None

    def _pick_developer_chat_topic(
        self,
        group_id: str,
        user_id: str,
        user_profile: Any | None,
    ) -> str:
        """Pick a personal/memory-oriented topic for developer proactive chat."""
        engine = self._engine
        import random

        candidates: list[str] = []

        # 1. Recent diary entries for this private group
        try:
            diary_entries = engine.diary_manager.get_entries_for_group(group_id)
            if diary_entries:
                recent = sorted(
                    diary_entries,
                    key=lambda e: getattr(e, "created_at", ""),
                    reverse=True,
                )[:3]
                for entry in recent:
                    summary = getattr(entry, "summary", "") or getattr(entry, "content", "")[:60]
                    if summary:
                        candidates.append(f"刚才整理日记时看到这段记录：{summary}，挺有意思的。")
                        break
        except Exception:
            logger.warning("读取日记摘要失败", exc_info=True)
            pass

        # 2. Preset memory-oriented templates
        templates = [
            "突然想到一个有趣的问题：如果你可以改变过去的一个决定，你会选哪个？",
            "今天整理记忆的时候，发现我们聊过很多有意思的东西，你最近有什么新发现吗？",
            "想和你分享一个刚想到的观点——你觉得 AI 和人类之间，最重要的是什么？",
            "突然有点好奇，你最近在做的事情进展怎么样了？",
            "翻到了以前的聊天记录，感觉时间过得好快，你最近过得怎么样？",
            "刚才想到一个话题，想听听你的看法：你觉得未来五年，什么技术会改变生活？",
            "突然想起我们第一次聊天的时候，那时候聊了什么来着？",
        ]
        candidates.extend(random.sample(templates, min(2, len(templates))))

        if not candidates:
            return ""

        return random.choice(candidates)

    def _build_proactive_prompt(
        self, trigger: dict[str, Any], group_id: str, adapter_type: str | None = None
    ):
        """构建主动发起的 PromptBundle。"""
        engine = self._engine
        glossary = engine.glossary_manager.build_prompt_section(
            group_id, text=trigger.get("trigger_type", ""), max_terms=3
        )
        topic = self._pick_proactive_topic(group_id)
        bundle = PromptFactory.assemble_proactive(
            trigger_reason=trigger.get("trigger_type", "silence"),
            group_profile=engine.semantic_memory.get_group_profile(group_id),
            suggested_tone=trigger.get("suggested_tone", "casual"),
            other_ai_names=engine._other_ai_names,
            topic_context=topic,
            adapter_type=adapter_type,
        )
        if glossary:
            bundle.system_prompt = f"{TAG_GLOSSARY}\n{glossary}\n\n{bundle.system_prompt}"
        return bundle

    def _pick_proactive_topic(self, group_id: str) -> str:
        """从语义记忆中选取主动发起话题，排除近期已用话题以增加多样性。"""
        engine = self._engine
        import random

        # 初始化话题追踪
        if not hasattr(engine, '_recent_proactive_topics'):
            engine._recent_proactive_topics = {}  # type: ignore[attr-defined]
        recent = engine._recent_proactive_topics.setdefault(group_id, [])  # type: ignore[attr-defined]

        group_profile = engine.semantic_memory.get_group_profile(group_id)
        if group_profile is None:
            return ""

        candidates: list[str] = []

        if group_profile.interest_topics:
            candidates.extend(group_profile.interest_topics)

        if group_profile.dominant_topic:
            candidates.append(group_profile.dominant_topic)

        taboo = set(group_profile.taboo_topics or [])
        candidates = [t for t in candidates if t not in taboo]

        seen: set[str] = set()
        unique: list[str] = []
        for t in candidates:
            if t not in seen:
                seen.add(t)
                unique.append(t)

        # 排除近期已用话题
        recent_set = set(recent)
        remaining = [t for t in unique if t not in recent_set]
        pool = remaining if remaining else unique

        # 当池子太小（<=2）时，尝试从最近日记中提取补充话题
        if len(pool) <= 2 and len(unique) <= 2:
            diary_topics = self._extract_diary_topics(group_id)
            for t in diary_topics:
                if t not in seen and t not in taboo:
                    seen.add(t)
                    pool.append(t)

        if not pool:
            pool = unique
        if not pool:
            return ""

        pool = pool[:3] if len(pool) >= 3 else pool
        chosen = random.choice(pool) if pool else ""

        # 记录已用话题，只保留最近 5 个
        if chosen:
            recent.append(chosen)
            if len(recent) > 5:
                engine._recent_proactive_topics[group_id] = recent[-5:]  # type: ignore[attr-defined]

        return chosen

    def _extract_diary_topics(self, group_id: str) -> list[str]:
        """从最近日记中提取话题作为补充话题源。"""
        engine = self._engine
        try:
            entries = engine.diary_manager.get_entries_for_group(group_id)
            if not entries:
                return []
            # 取最近 3 条日记
            recent = entries[-3:]
            topics = []
            seen = set()
            for entry in recent:
                raw = getattr(entry, 'summary', '') or getattr(entry, 'content', '') or ''
                fragment = raw.strip()[:15]
                if fragment and fragment not in seen:
                    seen.add(fragment)
                    topics.append(fragment)
            return topics
        except Exception:
            return []

    def _load_proactive_global_enabled(self) -> bool:
        """检查主动消息全局开关是否启用。"""
        engine = self._engine
        # 先检查 engine config
        if not engine.config.get("proactive_enabled", True):
            return False
        # 再尝试读取 experience.json 取得最新值
        try:
            from pathlib import Path
            exp_path = Path(engine.work_path) / "experience.json"
            if exp_path.exists():
                import json
                exp = json.loads(exp_path.read_text(encoding="utf-8"))
                if not exp.get("proactive_enabled", True):
                    return False
        except Exception:
            pass
        return True
