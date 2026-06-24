"""Context assembler: builds LLM messages from basic memory + diary RAG.

历史消息以 assistant 消息切分，构造 user-assistant 消息链。
每个 assistant 回复前的 user/system 消息合并为一个 user 消息（XML 格式），
assistant 回复单独作为一条消息。

新架构增强：
- 注入 BiographyView 传记（演化链派生）
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sirius_pulse.memory.basic.manager import BasicMemoryManager
from sirius_pulse.memory.biography.view import BiographyView
from sirius_pulse.memory.diary.indexer import DiaryRetriever

logger = logging.getLogger(__name__)


class ContextAssembler:
    """Assembles conversation context for LLM generation.

    Combines:
    - Basic memory (immediate context, XML format)
    - Diary entries (historical RAG)
    - BiographyView (user profiles from evolution chain)
    """

    def __init__(
        self,
        basic_mgr: BasicMemoryManager,
        diary_retriever: DiaryRetriever,
        biography_view: BiographyView | None = None,
        is_source_diarized: Callable[[str, str], bool] | None = None,
    ) -> None:
        self._basic = basic_mgr
        self._diary = diary_retriever
        self._bio_view = biography_view
        self._is_source_diarized = is_source_diarized

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
        platform_message_id: str = "",
    ) -> list[dict[str, Any]]:
        """构建消息链（方案 C：以 assistant 消息切分）。

        返回多条消息：
        1. system  -- 富化后的系统提示词（含传记）
        2. user/assistant 交替 -- 历史对话（按 assistant 切分）
        3. user   -- 当前用户消息（含日记上下文）

        Args:
            content_is_tagged: 若 True 表示 current_query 已包含 <message> XML
                标签及前缀段落（来自延迟队列合并 + PromptFactory.assemble_chat），
                无需再用 html.escape 包装，直接作为 user 消息内容。
        """
        # 1. 检索相关日记
        enriched_query = self._enrich_search_query(
            search_query or current_query, speaker_user_id, mentioned_user_ids
        )

        diary_entries = self._diary.retrieve(
            query=enriched_query,
            group_id=group_id,
            top_k=diary_top_k,
            max_tokens_budget=diary_token_budget,
        )

        logger.info(
            "ContextAssembler: group=%s | %d 条日记 | query=%.30s...",
            group_id,
            len(diary_entries),
            search_query or current_query,
        )

        # 2. 获取传记信息（从演化链派生）
        bio_sections = self._build_biography_sections(speaker_user_id, mentioned_user_ids or [])

        # 3. 构建富化后的系统提示词
        enriched_system = self._enrich_system_prompt(
            system_prompt,
            biography_sections=bio_sections,
        )

        # 4. 构建消息链（方案 C）
        messages: list[dict[str, Any]] = [{"role": "system", "content": enriched_system}]

        diary_context = self._build_diary_context(diary_entries)

        def _with_user_context(content: str) -> str:
            if not diary_context:
                return content
            return f"{diary_context}\n\n{content}" if content else diary_context

        # 获取历史条目并按 assistant 切分（recent_n<=0 时取全部未压缩消息，上限 50 条）
        recent = self._cacheable_history_entries(group_id)
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
                pending_entries = recent[last_assistant_idx + 1 :]
                recent = recent[: last_assistant_idx + 1]

        history_xml = self._entries_to_xml(recent) if recent else ""
        enriched_system = self._enrich_system_prompt(
            system_prompt,
            biography_sections=bio_sections,
            history_xml=history_xml,
        )
        messages = [{"role": "system", "content": enriched_system}]
        recent = []

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
            messages.append({"role": "user", "content": _with_user_context(current_query)})
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
                # 使用统一的 tag_message 生成 <message> 标签
                from sirius_pulse.core.prompt_factory import PromptFactory

                current_xml = PromptFactory.tag_message(
                    current_query,
                    speaker=speaker_name or speaker_user_id,
                    user_id=speaker_user_id,
                    platform_message_id=platform_message_id,
                )
                # 把 pending 消息和当前消息合并
                if all_current:
                    pending_xml = self._entries_to_xml(all_current, tag="pending_messages")
                    # 去掉外层标签，只保留 message 标签
                    pending_lines = [
                        line
                        for line in pending_xml.split("\n")
                        if line.strip()
                        and not line.startswith("<pending_messages>")
                        and not line.startswith("</pending_messages>")
                    ]
                    combined = "\n".join(pending_lines) + "\n" + current_xml
                    messages.append({"role": "user", "content": _with_user_context(combined)})
                else:
                    messages.append({"role": "user", "content": _with_user_context(current_xml)})
            else:
                if all_current:
                    pending_xml = self._entries_to_xml(all_current, tag="pending_messages")
                    messages.append(
                        {
                            "role": "user",
                            "content": _with_user_context(pending_xml + "\n" + current_query),
                        }
                    )
                else:
                    messages.append(
                        {"role": "user", "content": _with_user_context(current_query)}
                    )

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
        platform_message_id: str = "",
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
            platform_message_id=platform_message_id,
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

    def build_history_xml(
        self, group_id: str, n: int = 10, *, include_pending: bool = False
    ) -> str:
        """Build XML representation of recent conversation history."""
        return self._build_history_xml(group_id, n=n, include_pending=include_pending)

    def _cacheable_history_entries(self, group_id: str) -> list[Any]:
        entries = self._basic.get_all(group_id)
        if not entries or self._is_source_diarized is None:
            return list(entries)

        result: list[Any] = []
        for entry in entries:
            entry_id = getattr(entry, "entry_id", "")
            if not entry_id:
                result.append(entry)
                continue
            try:
                diarized = self._is_source_diarized(group_id, entry_id)
            except Exception:
                diarized = False
            if not diarized:
                result.append(entry)
        return result

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

        # 被提及者传记
        for uid in mentioned_user_ids:
            if uid == speaker_user_id:
                continue
            bio = self._bio_view.get_biography(uid)
            if bio and bio.short_bio:
                parts.append(f"[被提及] {bio.name}: {bio.short_bio}")

        return "\n".join(parts)

    @staticmethod
    def _build_diary_context(diary_entries: list[Any]) -> str:
        """构建日记上下文，作为 user 消息链的一部分注入。"""
        if not diary_entries:
            return ""

        from sirius_pulse.core.prompt_factory import TAG_HISTORY_DIARY, TAG_HISTORY_DIARY_END

        entries = diary_entries[:12]
        full_text_count = min(5, len(entries))
        lines = [TAG_HISTORY_DIARY]
        for i, entry in enumerate(entries, 1):
            ts = (getattr(entry, "created_at", "") or "")[:16].replace("T", " ")
            content = getattr(entry, "content", "")
            summary = getattr(entry, "summary", "")
            text = content if (i <= full_text_count and content) else summary
            lines.append(f"{i}. [{ts}] {text}" if ts else f"{i}. {text}")
        lines.append(TAG_HISTORY_DIARY_END)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_history_xml(
        self,
        group_id: str,
        n: int = 5,
        *,
        include_pending: bool = False,
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
        entries = self._basic.get_entries_by_user(user_id, exclude_group_id=exclude_group_id, n=n)
        if not entries:
            return ""
        return self._entries_to_xml(entries, tag="cross_group_history", include_group=True)

    @staticmethod
    def _entries_to_xml(
        entries: list[Any],
        *,
        tag: str = "conversation_history",
        include_group: bool = False,
    ) -> str:
        _tz_cn = timezone(timedelta(hours=8))
        lines: list[str] = [f"<{tag}>"]
        for entry in entries:
            # 解析时间
            ts_str = ""
            raw_ts = getattr(entry, "timestamp", "")
            if raw_ts:
                try:
                    ts_str = datetime.fromisoformat(raw_ts).astimezone(_tz_cn).strftime("%H:%M:%S")
                except (ValueError, TypeError):
                    ts_str = ""

            # 使用统一的 tag_message 生成 <message> 标签
            from sirius_pulse.core.prompt_factory import PromptFactory

            msg_id = getattr(entry, "platform_message_id", "")
            group = getattr(entry, "group_id", "") if include_group else ""
            safe_speaker = html.escape(entry.speaker_name or entry.user_id or "unknown", quote=True)
            safe_user_id = html.escape(entry.user_id or "", quote=True)
            tagged = PromptFactory.tag_message(
                entry.content or "",
                speaker=entry.speaker_name or entry.user_id or "unknown",
                user_id=entry.user_id or "",
                platform_message_id=msg_id,
                time_str=ts_str,
                group_id=group,
            )
            lines.append(f"  {tagged}")

            if getattr(entry, "multimodal_inputs", None):
                for m in entry.multimodal_inputs:
                    if m.get("type") != "image":
                        continue
                    if m.get("sub_type") == "1":
                        # 优先使用缓存的caption，否则使用默认值
                        sticker_caption = html.escape(str(m.get("caption", "动画表情")), quote=True)
                        lines.append(
                            f'  <image type="sticker" caption="{sticker_caption}" '
                            f'speaker="{safe_speaker}" user_id="{safe_user_id}"/>'
                        )
                        continue
                    url = html.escape(str(m.get("value", "")), quote=True)
                    caption = html.escape(str(m.get("caption", "")), quote=True)
                    lines.append(
                        f'  <image src="{url}" caption="{caption}" '
                        f'speaker="{safe_speaker}" user_id="{safe_user_id}"/>'
                    )
        lines.append(f"</{tag}>")
        return "\n".join(lines)

    def _enrich_system_prompt(
        self,
        base_prompt: str,
        biography_sections: str = "",
        history_xml: str = "",
    ) -> str:
        """富化系统提示词：注入传记和可缓存历史。"""
        # 注入传记信息（较稳定，放最前面以最大化缓存前缀匹配）
        enriched = base_prompt
        if biography_sections:
            enriched += f"\n\n<biography>\n{biography_sections}\n</biography>"

        if history_xml:
            history_prefix = "\n".join(
                [
                    "【历史聊天信息】",
                    "尚未被日记记忆系统收录的近期原始消息。",
                    history_xml,
                    "【历史聊天信息结束】",
                ]
            )
            return f"{history_prefix}\n\n{enriched}"
        return enriched

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
        for uid in mentioned_user_ids or []:
            if uid == speaker_user_id:
                continue
            bio = self._bio_view.get_biography(uid)
            if bio and bio.name:
                bio_parts.append(bio.name)

        if not bio_parts:
            return base_query

        enriched = f"{base_query} {' '.join(bio_parts)}"
        return enriched[:500]
