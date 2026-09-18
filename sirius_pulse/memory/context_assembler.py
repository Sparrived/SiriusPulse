"""Context assembler: builds LLM messages from basic memory + diary RAG.

历史消息以 assistant 消息切分，构造 user-assistant 消息链。
每个 assistant 回复前的 user/system 消息合并为一个 user 消息（XML 格式），
assistant 回复单独作为一条消息。

"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sirius_pulse.core.constants import DEFAULT_BASIC_MEMORY_HISTORY_TOKEN_BUDGET
from sirius_pulse.memory.basic.manager import BasicMemoryManager
from sirius_pulse.memory.diary.indexer import DiaryRetriever

logger = logging.getLogger(__name__)


class ContextAssembler:
    """Assembles conversation context for LLM generation.

    Combines:
    - Basic memory (immediate context, XML format)
    - Diary entries (historical RAG)
    """

    def __init__(
        self,
        basic_mgr: BasicMemoryManager,
        diary_retriever: DiaryRetriever | None = None,
        is_source_diarized: Callable[[str, str], bool] | None = None,
        memory_unit_retriever: Any | None = None,
        is_source_checkpointed: Callable[[str, str], bool] | None = None,
    ) -> None:
        self._basic = basic_mgr
        self._diary = diary_retriever
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
        identity_aliases: list[str] | None = None,
        mentioned_user_ids: list[str] | None = None,
        content_is_tagged: bool = False,
        platform_message_id: str = "",
        dynamic_context: str = "",
        history_token_budget: int = DEFAULT_BASIC_MEMORY_HISTORY_TOKEN_BUDGET,
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
        enriched_query = search_query or current_query

        memory_context = ""
        memory_count = 0
        effective_memory_unit_top_k = (
            diary_top_k if memory_unit_top_k is None else memory_unit_top_k
        )
        if self._memory_units is not None:
            memory_units = self._memory_units.retrieve(
                query=enriched_query,
                group_id=group_id,
                top_k=effective_memory_unit_top_k,
                max_tokens_budget=diary_token_budget,
                user_id=speaker_user_id,
                identity_aliases=identity_aliases,
                mentioned_user_ids=mentioned_user_ids,
                cross_group_enabled=cross_group_enabled,
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
        # 活跃窗口会保留远多于提示词所需的原始消息（直到被 checkpoint 覆盖），
        # 因此这里必须按 token 预算裁剪，只有最近预算内的消息进入提示词。
        recent = self._cacheable_history_entries(group_id, recent_n=recent_n)
        recent = self._trim_history_to_token_budget(recent, history_token_budget)
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

        message_timing = self._build_message_timing_context(recent, pending_entries)

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

        def _with_message_timing(content: str) -> str:
            if not message_timing:
                return content
            return f"{message_timing}\n\n{content}" if content else message_timing

        # 排除与当前发言者匹配的最后一条 pending 条目，避免 current_query 重复注入。
        filtered_pending = pending_entries
        if pending_entries and speaker_user_id:
            for i in range(len(pending_entries) - 1, -1, -1):
                if pending_entries[i].user_id == speaker_user_id:
                    filtered_pending = pending_entries[:i] + pending_entries[i + 1 :]
                    break
        all_current = filtered_pending

        if content_is_tagged:
            tagged_content = current_query
            if all_current:
                pending_xml = self._entries_to_xml(all_current, tag="pending_messages")
                pending_lines = [
                    line
                    for line in pending_xml.split("\n")
                    if line.strip()
                    and not line.startswith("<pending_messages>")
                    and not line.startswith("</pending_messages>")
                ]
                tagged_content = "\n".join(pending_lines) + "\n" + current_query
            messages.append(
                {
                    "role": "user",
                    "content": _with_user_context(_with_message_timing(tagged_content)),
                }
            )
        else:
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
                    messages.append(
                        {
                            "role": "user",
                            "content": _with_user_context(_with_message_timing(combined)),
                        }
                    )
                else:
                    messages.append(
                        {
                            "role": "user",
                            "content": _with_user_context(_with_message_timing(current_xml)),
                        }
                    )
            else:
                if all_current:
                    pending_xml = self._entries_to_xml(all_current, tag="pending_messages")
                    messages.append(
                        {
                            "role": "user",
                            "content": _with_user_context(
                                _with_message_timing(pending_xml + "\n" + current_query)
                            ),
                        }
                    )
                else:
                    messages.append(
                        {
                            "role": "user",
                            "content": _with_user_context(_with_message_timing(current_query)),
                        }
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
        memory_unit_top_k: int | None = None,
        diary_token_budget: int = 800,
        cross_group_user_id: str = "",
        cross_group_enabled: bool = False,
        include_pending: bool = False,
        speaker_user_id: str = "",
        speaker_name: str = "",
        identity_aliases: list[str] | None = None,
        mentioned_user_ids: list[str] | None = None,
        content_is_tagged: bool = False,
        platform_message_id: str = "",
        dynamic_context: str = "",
        history_token_budget: int = DEFAULT_BASIC_MEMORY_HISTORY_TOKEN_BUDGET,
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
            identity_aliases=identity_aliases,
            mentioned_user_ids=mentioned_user_ids,
            content_is_tagged=content_is_tagged,
            platform_message_id=platform_message_id,
            dynamic_context=dynamic_context,
            history_token_budget=history_token_budget,
        )

        from sirius_pulse.token.utils import estimate_tokens

        breakdown: dict[str, int] = {}
        if messages:
            enriched_query = search_query or current_query
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
                    user_id=speaker_user_id,
                    identity_aliases=identity_aliases,
                    mentioned_user_ids=mentioned_user_ids,
                    cross_group_enabled=cross_group_enabled,
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
    def _trim_history_to_token_budget(entries: list[Any], budget: int) -> list[Any]:
        """保留最近 N token 内的历史消息，从最旧一端裁剪。

        活跃窗口保留的是"尚未被 checkpoint 覆盖"的原始消息，数量可能远超提示词所能
        承载；只有本预算内的最近消息才注入模型。budget <= 0 表示不限制。
        """
        if budget <= 0 or not entries:
            return entries
        from sirius_pulse.token.utils import estimate_tokens

        kept: list[Any] = []
        used = 0
        for entry in reversed(entries):
            cost = estimate_tokens(str(getattr(entry, "content", "") or ""))
            if kept and used + cost > budget:
                break
            used += cost
            kept.append(entry)
        kept.reverse()
        return kept

    @staticmethod
    def _build_memory_unit_context(memory_units: list[Any]) -> str:
        """Build compact memory-unit context for the current user message."""
        if not memory_units:
            return ""

        lines = [
            "<memory_units>",
            "The following are candidate background memory facts, not current chat messages. Use only directly relevant facts explicitly; indirect facts may only affect tone, and irrelevant facts must be ignored. Do not mention checking memory, reading logs, or remembering these facts. Do not repeat the same old event, preference, or time detail if it was already mentioned recently unless the user asks. Facts marked as your own first-person experience are things you yourself did or noticed, so you may bring them up in your own voice when they are relevant to what is being discussed.",
        ]
        for unit in memory_units[:12]:
            ts = (getattr(unit, "event_time", "") or getattr(unit, "created_at", "") or "")[
                :16
            ].replace("T", " ")
            unit_type = getattr(unit, "unit_type", "") or "event"
            summary = getattr(unit, "summary", "") or ""
            status = getattr(unit, "status", "") or ""
            valid_until = getattr(unit, "valid_until", "") or ""
            prefix = f"[{ts}] ({unit_type})" if ts else f"({unit_type})"
            suffix = ""
            if status and status != "unknown":
                suffix += f" [{status}]"
            if valid_until:
                suffix += f" [valid_until={valid_until[:10]}]"
            lines.append(f"{prefix} {summary}{suffix}")
        lines.append("</memory_units>")
        return "\n".join(lines)

    @staticmethod
    def _format_message_gap(seconds: float) -> str:
        seconds = max(0, round(seconds))
        if seconds < 60:
            return f"约 {seconds} 秒"
        minutes = seconds // 60
        if minutes < 60:
            return f"约 {minutes} 分钟"
        hours = minutes // 60
        if hours < 24:
            return f"约 {hours} 小时"
        return f"约 {hours // 24} 天"

    @staticmethod
    def _parse_entry_timestamp(entry: Any) -> datetime | None:
        raw_timestamp = str(getattr(entry, "timestamp", "") or "")
        if not raw_timestamp:
            return None
        try:
            parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @classmethod
    def _build_message_timing_context(
        cls,
        recent: list[Any],
        pending_entries: list[Any],
    ) -> str:
        visible = [*recent, *pending_entries]
        if len(visible) < 2 or visible[-1].role == "assistant":
            return ""
        latest = cls._parse_entry_timestamp(visible[-1])
        previous = cls._parse_entry_timestamp(visible[-2])
        if latest is None or previous is None:
            return ""
        gap = max(0.0, (latest - previous).total_seconds())
        return f"【消息间隔】当前消息与上一条消息相隔{cls._format_message_gap(gap)}。"

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
        lines: list[str] = [f"<{tag}>"] if include_wrapper else []
        for entry in entries:
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
