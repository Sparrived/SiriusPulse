"""Context assembler: builds LLM messages from basic memory + diary RAG.

历史消息以 assistant 消息切分，构造 user-assistant 消息链。
每个 assistant 回复前的 user/system 消息合并为一个 user 消息（XML 格式），
assistant 回复单独作为一条消息。

新架构增强：
- 注入 UserPersonaProfile 画像（模型工具维护）
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sirius_pulse.memory.basic.manager import BasicMemoryManager
from sirius_pulse.memory.diary.indexer import DiaryRetriever

logger = logging.getLogger(__name__)


class ContextAssembler:
    """Assembles conversation context for LLM generation.

    Combines:
    - Basic memory (immediate context, XML format)
    - Diary entries (historical RAG)
    - UserPersonaProfile cards (model-maintained people profiles)
    """

    def __init__(
        self,
        basic_mgr: BasicMemoryManager,
        diary_retriever: DiaryRetriever | None = None,
        profile_manager: Any | None = None,
        is_source_diarized: Callable[[str, str], bool] | None = None,
        memory_unit_retriever: Any | None = None,
        is_source_checkpointed: Callable[[str, str], bool] | None = None,
    ) -> None:
        self._basic = basic_mgr
        self._diary = diary_retriever
        self._profile_manager = profile_manager
        self._is_source_diarized = is_source_diarized
        self._memory_units = memory_unit_retriever
        self._is_source_checkpointed = is_source_checkpointed

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
        memory_unit_top_k: int | None = None,
        diary_token_budget: int = 800,
        cross_group_user_id: str = "",
        cross_group_enabled: bool = False,
        include_pending: bool = False,
        speaker_user_id: str = "",
        speaker_name: str = "",
        mentioned_user_ids: list[str] | None = None,
        content_is_tagged: bool = False,
        platform_message_id: str = "",
        dynamic_context: str = "",
    ) -> list[dict[str, Any]]:
        """构建消息链（user/assistant 交替）。

        返回消息结构：
        1. system   -- 稳定系统指令（已由 PromptFactory 组装完成）
        2. user/assistant 交替 -- 历史对话
        3. user     -- 当前用户消息（日记 + 动态上下文 + 消息内容）

        Args:
            content_is_tagged: 若 True 表示 current_query 已包含 <message> XML
                标签及前缀段落（来自延迟队列合并 + PromptFactory.assemble_chat），
                无需再用 html.escape 包装，直接作为 user 消息内容。
            dynamic_context: 每轮变化的上下文（传记、关系、记忆等），
                由 PromptFactory.assemble_chat 产出，注入到当前 user 消息中。
        """
        # 1. Retrieve relevant long-term memory.
        enriched_query = self._enrich_search_query(
            search_query or current_query, speaker_user_id, mentioned_user_ids
        )

        memory_context = ""
        memory_count = 0
        effective_memory_unit_top_k = diary_top_k if memory_unit_top_k is None else memory_unit_top_k
        if self._memory_units is not None:
            memory_units = self._memory_units.retrieve(
                query=enriched_query,
                group_id=group_id,
                top_k=effective_memory_unit_top_k,
                max_tokens_budget=diary_token_budget,
            )
            memory_count = len(memory_units)
            memory_context = self._build_memory_unit_context(memory_units)
        elif self._diary is not None:
            diary_entries = self._diary.retrieve(
                query=enriched_query,
                group_id=group_id,
                top_k=diary_top_k,
                max_tokens_budget=diary_token_budget,
            )
            memory_count = len(diary_entries)
            memory_context = self._build_diary_context(diary_entries)

        logger.info(
            "ContextAssembler: group=%s | %d memory items | query=%.30s...",
            group_id,
            memory_count,
            search_query or current_query,
        )

        # 2. 构建稳定的 system prompt（PromptFactory 已完成静态注入）
        enriched_system = self._build_stable_system(system_prompt)
        messages: list[dict[str, Any]] = [{"role": "system", "content": enriched_system}]

        # 3. 构建 user/assistant 交替的历史消息
        recent = self._cacheable_history_entries(group_id, recent_n=recent_n)
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

        # 4. 构建当前用户消息：日记 + 动态上下文 + 消息内容
        def _with_user_context(content: str) -> str:
            """Prefix long-term memory and dynamic context to the user message."""
            parts: list[str] = []
            if memory_context:
                parts.append(memory_context)
            if dynamic_context:
                parts.append(dynamic_context)
            if content:
                parts.append(content)
            return "\n\n".join(parts) if parts else ""

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
                from sirius_pulse.core.prompt_factory import PromptFactory

                current_xml = PromptFactory.tag_message(
                    current_query,
                    speaker=speaker_name or speaker_user_id,
                    user_id=speaker_user_id,
                    platform_message_id=platform_message_id,
                )
                if all_current:
                    pending_xml = self._entries_to_xml(all_current, tag="pending_messages")
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
                    messages.append({"role": "user", "content": _with_user_context(current_query)})

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
        memory_unit_top_k: int | None = None,
        diary_token_budget: int = 800,
        cross_group_user_id: str = "",
        cross_group_enabled: bool = False,
        include_pending: bool = False,
        speaker_user_id: str = "",
        speaker_name: str = "",
        mentioned_user_ids: list[str] | None = None,
        content_is_tagged: bool = False,
        platform_message_id: str = "",
        dynamic_context: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """构建消息链并返回 token 分布统计。"""
        messages = self.build_messages(
            group_id=group_id,
            current_query=current_query,
            system_prompt=system_prompt,
            search_query=search_query,
            recent_n=recent_n,
            diary_top_k=diary_top_k,
            memory_unit_top_k=memory_unit_top_k,
            diary_token_budget=diary_token_budget,
            cross_group_user_id=cross_group_user_id,
            cross_group_enabled=cross_group_enabled,
            include_pending=include_pending,
            speaker_user_id=speaker_user_id,
            speaker_name=speaker_name,
            mentioned_user_ids=mentioned_user_ids,
            content_is_tagged=content_is_tagged,
            platform_message_id=platform_message_id,
            dynamic_context=dynamic_context,
        )

        from sirius_pulse.token.utils import estimate_tokens

        breakdown: dict[str, int] = {}
        if messages:
            enriched_query = self._enrich_search_query(
                search_query or current_query, speaker_user_id, mentioned_user_ids
            )
            memory_text = ""
            effective_memory_unit_top_k = (
                diary_top_k if memory_unit_top_k is None else memory_unit_top_k
            )
            if self._memory_units is not None:
                memory_units = self._memory_units.retrieve(
                    query=enriched_query,
                    group_id=group_id,
                    top_k=effective_memory_unit_top_k,
                    max_tokens_budget=diary_token_budget,
                )
                memory_text = "\n".join(getattr(unit, "summary", "") for unit in memory_units[:12])
            elif self._diary is not None:
                diary_entries = self._diary.retrieve(
                    query=enriched_query,
                    group_id=group_id,
                    top_k=diary_top_k,
                    max_tokens_budget=diary_token_budget,
                )
                full_count = min(5, len(diary_entries))
                memory_text = "\n".join(
                    (
                        f"{i}. [{(e.created_at or '')[:16].replace('T', ' ')}] "
                        f"{e.content if (i <= full_count and e.content) else e.summary}"
                        if e.created_at
                        else f"{i}. {e.content if (i <= full_count and e.content) else e.summary}"
                    )
                    for i, e in enumerate(diary_entries[:12], 1)
                )
            if memory_text:
                breakdown["diary"] = estimate_tokens(memory_text)

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

    def _cacheable_history_entries(self, group_id: str, *, recent_n: int = 0) -> list[Any]:
        entries = (
            self._basic.get_context(group_id, n=recent_n)
            if recent_n and recent_n > 0
            else self._basic.get_all(group_id)
        )
        source_filter = self._is_source_checkpointed or self._is_source_diarized
        if not entries or source_filter is None:
            return list(entries)

        result: list[Any] = []
        for entry in entries:
            entry_id = getattr(entry, "entry_id", "")
            if not entry_id:
                result.append(entry)
                continue
            try:
                diarized = source_filter(group_id, entry_id)
            except Exception:
                diarized = False
            if not diarized:
                result.append(entry)
        return result

    @staticmethod
    def _build_memory_unit_context(memory_units: list[Any]) -> str:
        """Build compact memory-unit context for the current user message."""
        if not memory_units:
            return ""

        lines = [
            "<memory_units>",
            "The following are candidate background memory facts, not current chat messages. Use only directly relevant facts explicitly; indirect facts may only affect tone, and irrelevant facts must be ignored. Do not mention checking memory, reading logs, or remembering these facts. Do not repeat the same old event, preference, or time detail if it was already mentioned recently unless the user asks.",
        ]
        for unit in memory_units[:12]:
            ts = (getattr(unit, "created_at", "") or "")[:16].replace("T", " ")
            unit_type = getattr(unit, "unit_type", "") or "event"
            summary = getattr(unit, "summary", "") or ""
            prefix = f"[{ts}] ({unit_type})" if ts else f"({unit_type})"
            lines.append(f"{prefix} {summary}")
        lines.append("</memory_units>")
        return "\n".join(lines)

    @staticmethod
    def _build_diary_context(diary_entries: list[Any]) -> str:
        """构建日记上下文，作为 user 消息链的一部分注入。"""
        if not diary_entries:
            return ""

        from sirius_pulse.core.prompt_factory import TAG_HISTORY_DIARY, TAG_HISTORY_DIARY_END

        entries = diary_entries[:12]
        full_text_count = min(5, len(entries))
        lines = [
            TAG_HISTORY_DIARY,
            "以下是候选背景记忆，不是当前聊天消息。先判断相关性：直接相关才可显式使用，间接相关只影响语气，无关则忽略；不要主动说明你查看、翻阅或记得这些日记。不要复述与当前问题无关的旧事；同一事件、偏好或时间信息近期已经提过时，默认不要再次提及，除非用户主动问。",
        ]
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
        include_wrapper: bool = True,
    ) -> str:
        _tz_cn = timezone(timedelta(hours=8))
        lines: list[str] = [f"<{tag}>"] if include_wrapper else []
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
        if include_wrapper:
            lines.append(f"</{tag}>")
        return "\n".join(lines)

    @staticmethod
    def _build_stable_system(base_prompt: str) -> str:
        """返回已组装完成的稳定 system prompt。

        只包含不随消息变化的静态内容，利于 prompt caching。
        回复规范、人格和风格文本由 PromptFactory.assemble_chat 统一注入。
        动态内容（传记、关系、记忆等）由 PromptFactory.assemble_chat
        产出为 dynamic_context，注入到 user 消息中。
        """
        return base_prompt

    def _enrich_search_query(
        self,
        base_query: str,
        speaker_user_id: str = "",
        mentioned_user_ids: list[str] | None = None,
    ) -> str:
        """用人物画像信息丰富日记检索 query。"""
        if not self._profile_manager:
            return base_query

        bio_parts: list[str] = []

        if speaker_user_id:
            profile = self._profile_manager.get_profile("default", speaker_user_id, create=False)
            if profile:
                if profile.display_name:
                    bio_parts.append(profile.display_name)
                if profile.short_impression:
                    bio_parts.append(profile.short_impression[:100])

        for uid in mentioned_user_ids or []:
            if uid == speaker_user_id:
                continue
            profile = self._profile_manager.get_profile("default", uid, create=False)
            if profile and profile.display_name:
                bio_parts.append(profile.display_name)

        if not bio_parts:
            return base_query

        enriched = f"{base_query} {' '.join(bio_parts)}"
        return enriched[:500]
