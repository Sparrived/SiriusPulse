"""Context assembler: builds LLM messages from basic memory + diary RAG + situation summaries.

历史消息以 assistant 消息切分，构造 user-assistant 消息链。
每个 assistant 回复前的 user/system 消息合并为一个 user 消息（XML 格式），
assistant 回复单独作为一条消息。

新架构增强：
- 注入当日 Situation 摘要（暂冷压缩产物）
- 注入 BiographyView 传记（演化链派生）
- 支持 DiarySlice 三路召回（语义 + 三元组 + 关键词）
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sirius_pulse.memory.basic.manager import BasicMemoryManager
from sirius_pulse.memory.biography.view import BiographyView
from sirius_pulse.memory.diary.indexer import DiaryRetriever
from sirius_pulse.memory.situation.store import SituationStore

logger = logging.getLogger(__name__)


class ContextAssembler:
    """Assembles conversation context for LLM generation.

    Combines:
    - Basic memory (immediate context, XML format)
    - Situation summaries (today's validated facts)
    - Diary entries (historical RAG)
    - BiographyView (user profiles from evolution chain)
    """

    def __init__(
        self,
        basic_mgr: BasicMemoryManager,
        diary_retriever: DiaryRetriever,
        situation_store: SituationStore | None = None,
        biography_view: BiographyView | None = None,
        slice_retriever: Any | None = None,
    ) -> None:
        self._basic = basic_mgr
        self._diary = diary_retriever
        self._situations = situation_store
        self._bio_view = biography_view
        self._slice_retriever = slice_retriever

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_messages(
        self,
        group_id: str,
        current_query: str,
        system_prompt: str,
        *,
        search_query: str = "",
        recent_n: int = 0,
        diary_top_k: int = 12,
        diary_token_budget: int = 800,
        cross_group_user_id: str = "",
        cross_group_enabled: bool = False,
        include_pending: bool = False,
        speaker_user_id: str = "",
        speaker_name: str = "",
        mentioned_user_ids: list[str] | None = None,
        content_is_tagged: bool = False,
    ) -> list[dict[str, Any]]:
        """构建消息链（方案 C：以 assistant 消息切分）。

        返回多条消息：
        1. system  -- 富化后的系统提示词（含 Situation 摘要 + 日记 + 传记）
        2. user/assistant 交替 -- 历史对话（按 assistant 切分）
        3. user   -- 当前用户消息

        Args:
            content_is_tagged: 若 True 表示 current_query 已包含 <message> XML
                标签及前缀段落（来自延迟队列合并 + PromptFactory.assemble_chat），
                无需再用 html.escape 包装，直接作为 user 消息内容。
        """
        # 1. 获取当日 Situation 摘要（已通过演化链验证）
        today_summaries = self._get_recent_summaries(group_id)

        # 1.5 获取最新 Situation 涉及的原始消息的一半
        latest_source_entries = self._get_latest_situation_source_half(group_id)

        # 2. 检索相关日记（优先使用 DiarySliceRetriever 三路召回）
        enriched_query = self._enrich_search_query(
            search_query or current_query, speaker_user_id, mentioned_user_ids
        )

        # 提取查询中的实体（用于三元组精确匹配）
        query_entities = self._extract_entities(enriched_query)

        # 优先使用 DiarySliceRetriever 三路召回
        diary_slices = []
        if self._slice_retriever:
            diary_slices = self._slice_retriever.retrieve(
                query=enriched_query,
                query_entities=query_entities,
                group_id=group_id,
                token_budget=diary_token_budget,
                top_k=diary_top_k,
            )

        # fallback: 使用旧的 DiaryRetriever
        diary_entries = []
        if not diary_slices:
            diary_entries = self._diary.retrieve(
                query=enriched_query,
                group_id=group_id,
                top_k=diary_top_k,
                max_tokens_budget=diary_token_budget,
            )

        logger.info(
            "ContextAssembler: group=%s | %d 条当日摘要 | %d 条日记切片 | %d 条旧日记 | query=%.30s...",
            group_id, len(today_summaries), len(diary_slices), len(diary_entries),
            search_query or current_query,
        )

        # 3. 获取传记信息（从演化链派生）
        bio_sections = self._build_biography_sections(
            speaker_user_id, mentioned_user_ids or []
        )

        # 4. 构建富化后的系统提示词
        enriched_system = self._enrich_system_prompt(
            system_prompt, diary_entries,
            today_summaries=today_summaries,
            biography_sections=bio_sections,
            diary_slices=diary_slices,
            latest_source_entries=latest_source_entries,
        )

        # 5. 构建消息链（方案 C）
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": enriched_system}
        ]

        # 获取历史条目并按 assistant 切分（recent_n<=0 时取全部未压缩消息，上限 50 条）
        if recent_n > 0:
            recent = self._basic.get_context(group_id, n=recent_n)
        else:
            recent = self._basic.get_all(group_id)[-50:]
        pending_entries: list[Any] = []

        if recent and not include_pending:
            # 找到最后一条 assistant 消息的位置
            last_assistant_idx = -1
            for i in range(len(recent) - 1, -1, -1):
                if recent[i].role == "assistant":
                    last_assistant_idx = i
                    break

            if last_assistant_idx >= 0:
                # last_assistant 之后的消息是 pending（未回复的）
                pending_entries = recent[last_assistant_idx + 1:]
                recent = recent[: last_assistant_idx + 1]

        if recent:
            current_user_entries: list[Any] = []
            for entry in recent:
                if entry.role == "assistant":
                    if current_user_entries:
                        xml_content = self._entries_to_xml(current_user_entries)
                        messages.append({"role": "user", "content": xml_content})
                        current_user_entries = []
                    messages.append({"role": "assistant", "content": entry.content or ""})
                else:
                    current_user_entries.append(entry)

            if current_user_entries:
                xml_content = self._entries_to_xml(current_user_entries)
                messages.append({"role": "user", "content": xml_content})

        # 6. 添加当前用户消息（带身份标识）
        # 当 content_is_tagged=True 时，current_query 已由 PromptFactory.assemble_chat()
        # 包含完整的 XML 标签和前缀段落（传记、情绪、关系、技能等），直接使用
        if content_is_tagged:
            messages.append({"role": "user", "content": current_query})
        else:
            # 如果有 pending 消息，把它们和当前消息一起打包
            # 排除与当前发言者匹配的最后一条 pending 条目，避免 current_query 重复注入
            filtered_pending = pending_entries
            if pending_entries and speaker_user_id:
                for i in range(len(pending_entries) - 1, -1, -1):
                    if pending_entries[i].user_id == speaker_user_id:
                        filtered_pending = pending_entries[:i] + pending_entries[i + 1 :]
                        break
            all_current = filtered_pending
            if speaker_name or speaker_user_id:
                # 用 XML 格式包装，让模型知道是谁说的
                safe_content = html.escape(current_query, quote=False)
                safe_speaker = html.escape(speaker_name or speaker_user_id, quote=True)
                safe_uid = html.escape(speaker_user_id, quote=True)
                current_xml = (
                    f'<message index="1" speaker="{safe_speaker}" user_id="{safe_uid}">'
                    f'{safe_content}</message>'
                )
                # 把 pending 消息和当前消息合并
                if all_current:
                    pending_xml = self._entries_to_xml(all_current, tag="pending_messages")
                    # 去掉外层标签，只保留 message 标签
                    pending_lines = [
                        line for line in pending_xml.split("\n")
                        if line.strip() and not line.startswith("<pending_messages>")
                        and not line.startswith("</pending_messages>")
                    ]
                    combined = "\n".join(pending_lines) + "\n" + current_xml
                    messages.append({"role": "user", "content": combined})
                else:
                    messages.append({"role": "user", "content": current_xml})
            else:
                if all_current:
                    pending_xml = self._entries_to_xml(all_current, tag="pending_messages")
                    messages.append({"role": "user", "content": pending_xml + "\n" + current_query})
                else:
                    messages.append({"role": "user", "content": current_query})

        return messages

    def build_messages_with_breakdown(
        self,
        group_id: str,
        current_query: str,
        system_prompt: str,
        *,
        search_query: str = "",
        recent_n: int = 0,
        diary_top_k: int = 12,
        diary_token_budget: int = 800,
        cross_group_user_id: str = "",
        cross_group_enabled: bool = False,
        include_pending: bool = False,
        speaker_user_id: str = "",
        speaker_name: str = "",
        mentioned_user_ids: list[str] | None = None,
        content_is_tagged: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """构建消息链并返回 token 分布统计。"""
        messages = self.build_messages(
            group_id=group_id,
            current_query=current_query,
            system_prompt=system_prompt,
            search_query=search_query,
            recent_n=recent_n,
            diary_top_k=diary_top_k,
            diary_token_budget=diary_token_budget,
            cross_group_user_id=cross_group_user_id,
            cross_group_enabled=cross_group_enabled,
            include_pending=include_pending,
            speaker_user_id=speaker_user_id,
            speaker_name=speaker_name,
            mentioned_user_ids=mentioned_user_ids,
            content_is_tagged=content_is_tagged,
        )

        from sirius_pulse.token.utils import estimate_tokens

        breakdown: dict[str, int] = {}
        if messages:
            enriched_query = self._enrich_search_query(
                search_query or current_query, speaker_user_id, mentioned_user_ids
            )
            diary_entries = self._diary.retrieve(
                query=enriched_query,
                group_id=group_id,
                top_k=diary_top_k,
                max_tokens_budget=diary_token_budget,
            )
            if diary_entries:
                full_count = min(5, len(diary_entries))
                diary_text = "\n".join(
                    f"{i}. [{(e.created_at or '')[:16].replace('T', ' ')}] "
                    f"{e.content if (i <= full_count and e.content) else e.summary}"
                    if e.created_at
                    else f"{i}. {e.content if (i <= full_count and e.content) else e.summary}"
                    for i, e in enumerate(diary_entries[:12], 1)
                )
                breakdown["diary"] = estimate_tokens(diary_text)

            history_tokens = 0
            for msg in messages:
                if msg.get("role") in ("user", "assistant") and msg.get("content"):
                    history_tokens += estimate_tokens(str(msg["content"]))
            breakdown["history"] = history_tokens

        return messages, breakdown

    def build_history_xml(self, group_id: str, n: int = 10, *, include_pending: bool = False) -> str:
        """Build XML representation of recent conversation history."""
        return self._build_history_xml(group_id, n=n, include_pending=include_pending)

    # ------------------------------------------------------------------
    # Situation 摘要
    # ------------------------------------------------------------------

    def _get_recent_summaries(self, group_id: str) -> list[str]:
        """获取未处理的 Situation 摘要列表（只要没被转译为日记就一直携带）。"""
        if not self._situations:
            return []
        # 只注入未处理的 situations，已处理的不再携带
        situations = self._situations.get_recent(group_id, unprocessed_only=True)
        return [s.summary for s in situations if s.summary]

    def _get_latest_situation_source_half(self, group_id: str) -> list[Any]:
        """获取最新 Situation 涉及的原始消息的一半。

        从 BasicMemory 窗口中查找 source_entry_ids 对应的条目，
        取后半部分（较新的一半）返回。
        """
        if not self._situations:
            return []
        # 获取未处理的 situations（只要没被转译为日记就一直携带）
        situations = self._situations.get_recent(group_id, unprocessed_only=True)
        if not situations:
            return []
        latest = situations[-1]
        source_ids = latest.source_entry_ids
        if not source_ids:
            return []
        # 从 BasicMemory 窗口中查找匹配的条目
        all_entries = self._basic.get_all(group_id)
        id_set = set(source_ids)
        matched = [e for e in all_entries if e.entry_id in id_set]
        if not matched:
            return []
        # 取后半部分（较新的一半）
        half = len(matched) // 2
        return matched[half:]

    # ------------------------------------------------------------------
    # Biography 传记
    # ------------------------------------------------------------------

    def _build_biography_sections(
        self,
        speaker_user_id: str,
        mentioned_user_ids: list[str],
    ) -> str:
        """构建传记信息段落（从演化链派生）。"""
        if not self._bio_view:
            return ""

        parts: list[str] = []

        # 发言者传记
        if speaker_user_id:
            bio = self._bio_view.get_biography(speaker_user_id)
            if bio and bio.short_bio:
                parts.append(f"【发言者】{bio.name}: {bio.short_bio}")

        # 被提及者传记
        for uid in mentioned_user_ids:
            if uid == speaker_user_id:
                continue
            bio = self._bio_view.get_biography(uid)
            if bio and bio.short_bio:
                parts.append(f"【被提及】{bio.name}: {bio.short_bio}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_history_xml(
        self, group_id: str, n: int = 5, *, include_pending: bool = False,
    ) -> str:
        recent = self._basic.get_context(group_id, n=n)
        if not recent:
            return ""
        if not include_pending:
            last_assistant_idx = -1
            for i in range(len(recent) - 1, -1, -1):
                if recent[i].role == "assistant":
                    last_assistant_idx = i
                    break
            if last_assistant_idx >= 0:
                recent = recent[: last_assistant_idx + 1]
        return self._entries_to_xml(recent, tag="conversation_history")

    def _build_cross_group_history_xml(
        self, user_id: str, *, exclude_group_id: str, n: int = 5
    ) -> str:
        entries = self._basic.get_entries_by_user(
            user_id, exclude_group_id=exclude_group_id, n=n
        )
        if not entries:
            return ""
        return self._entries_to_xml(entries, tag="cross_group_history", include_group=True)

    @staticmethod
    def _entries_to_xml(
        entries: list[Any],
        *,
        tag: str = "conversation_history",
        include_group: bool = False,
        start_index: int = 1,
        reverse_index: bool = True,
    ) -> str:
        _tz_cn = timezone(timedelta(hours=8))
        lines: list[str] = [f'<{tag}>']
        total = len(entries)
        for i, entry in enumerate(entries):
            # reverse_index=True 时：最新消息 index=1，最旧消息 index=total
            idx = (total - i) if reverse_index else (i + start_index)
            speaker = entry.speaker_name or entry.user_id or "unknown"
            safe_content = html.escape(entry.content or "", quote=False)
            safe_speaker = html.escape(speaker, quote=True)
            safe_user_id = html.escape(entry.user_id or "", quote=True)

            ts_str = ""
            raw_ts = getattr(entry, "timestamp", "")
            if raw_ts:
                try:
                    ts_str = datetime.fromisoformat(raw_ts).astimezone(_tz_cn).strftime("%H:%M:%S")
                except (ValueError, TypeError):
                    ts_str = ""

            attrs = f' index="{idx}" speaker="{safe_speaker}" user_id="{safe_user_id}"'
            if ts_str:
                attrs += f' time="{ts_str}"'
            if include_group and getattr(entry, "group_id", None):
                safe_group = html.escape(entry.group_id, quote=True)
                attrs += f' group="{safe_group}"'
            # 添加平台消息 ID（用于引用回复）
            msg_id = getattr(entry, "platform_message_id", "")
            if msg_id:
                safe_msg_id = html.escape(str(msg_id), quote=True)
                attrs += f' msg_id="{safe_msg_id}"'

            lines.append(f'  <message{attrs}>{safe_content}</message>')

            if getattr(entry, "multimodal_inputs", None):
                for m in entry.multimodal_inputs:
                    if m.get("type") != "image":
                        continue
                    if m.get("sub_type") == "1":
                        lines.append(
                            f'  <image type="sticker" caption="动画表情" '
                            f'speaker="{safe_speaker}" user_id="{safe_user_id}"/>'
                        )
                        continue
                    url = html.escape(str(m.get("value", "")), quote=True)
                    caption = html.escape(str(m.get("caption", "")), quote=True)
                    lines.append(
                        f'  <image src="{url}" caption="{caption}" '
                        f'speaker="{safe_speaker}" user_id="{safe_user_id}"/>'
                    )
        lines.append(f'</{tag}>')
        return "\n".join(lines)

    def _enrich_system_prompt(
        self,
        base_prompt: str,
        diary_entries: list[Any],
        today_summaries: list[str] | None = None,
        biography_sections: str = "",
        diary_slices: list[Any] | None = None,
        latest_source_entries: list[Any] | None = None,
    ) -> str:
        """富化系统提示词：注入 Situation 摘要 + 日记 + 传记。"""
        from sirius_pulse.core.prompt_factory import PromptFactory

        # 先用 PromptFactory 注入日记（旧格式）
        enriched = PromptFactory.enrich_system_prompt(
            base_prompt=base_prompt,
            diary_entries=diary_entries,
            history_xml="",
            cross_group_xml="",
        )

        # 注入日记切片（新格式，优先级更高）
        if diary_slices:
            slices_text = self._format_diary_slices(diary_slices)
            if slices_text:
                enriched += f"\n\n<diary_slices>\n{slices_text}\n</diary_slices>"

        # 注入当日 Situation 摘要 + 最新 Situation 涉及的原始消息
        if today_summaries:
            summaries_text = "\n".join(f"- {s}" for s in today_summaries)
            enriched += f"\n\n<today_context>\n今天的经历摘要：\n{summaries_text}"
            if latest_source_entries:
                source_xml = self._entries_to_xml(
                    latest_source_entries, tag="compressed_source_messages"
                )
                enriched += f"\n\n最新压缩涉及的原始消息（部分内容）：\n{source_xml}"
            enriched += "\n</today_context>"

        # 注入传记信息
        if biography_sections:
            enriched += f"\n\n<biography>\n{biography_sections}\n</biography>"

        return enriched

    @staticmethod
    def _format_diary_slices(slices: list[Any]) -> str:
        """格式化日记切片为文本。"""
        lines = []
        for i, s in enumerate(slices, 1):
            content = getattr(s, "content", "") or ""
            summary = getattr(s, "summary", "") or ""
            topics = getattr(s, "topics", []) or []
            time_start = getattr(s, "time_range_start", "") or ""

            time_str = ""
            if time_start:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(time_start.replace("Z", "+00:00"))
                    time_str = f" ({dt.strftime('%m-%d %H:%M')})"
                except (ValueError, TypeError):
                    pass

            topic_str = f" [{', '.join(topics)}]" if topics else ""
            lines.append(f"{i}. [{time_str}{topic_str}] {summary}")
            if content and content != summary:
                lines.append(f"   {content[:200]}")

        return "\n".join(lines)

    @staticmethod
    def _extract_entities(text: str) -> list[str]:
        """从文本中提取实体名（简单实现）。"""
        import re
        # 提取中文名字（2-4个字）
        entities = re.findall(r'[\u4e00-\u9fff]{2,4}', text)
        # 去重
        return list(set(entities))[:5]

    def _enrich_search_query(
        self,
        base_query: str,
        speaker_user_id: str = "",
        mentioned_user_ids: list[str] | None = None,
    ) -> str:
        """用传记信息丰富日记检索 query。"""
        if not self._bio_view:
            return base_query

        bio_parts: list[str] = []

        # 发言者传记
        if speaker_user_id:
            bio = self._bio_view.get_biography(speaker_user_id)
            if bio:
                if bio.name:
                    bio_parts.append(bio.name)
                if bio.identity_anchors:
                    bio_parts.extend(bio.identity_anchors[:3])
                if bio.short_bio:
                    bio_parts.append(bio.short_bio[:100])

        # 被提及者传记
        for uid in (mentioned_user_ids or []):
            if uid == speaker_user_id:
                continue
            bio = self._bio_view.get_biography(uid)
            if bio and bio.name:
                bio_parts.append(bio.name)

        if not bio_parts:
            return base_query

        enriched = f"{base_query} {' '.join(bio_parts)}"
        return enriched[:500]
