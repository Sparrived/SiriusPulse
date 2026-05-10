"""Context assembler: builds LLM messages from basic memory + diary RAG.

Short-term memory (recent basic memory entries) is embedded into the system
prompt as an XML block rather than traditional OpenAI message history.
This avoids role-confusion in multi-human group chat scenarios.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from sirius_chat.memory.basic.manager import BasicMemoryManager
from sirius_chat.memory.diary.indexer import DiaryRetriever

logger = logging.getLogger(__name__)


class ContextAssembler:
    """Assembles conversation context for LLM generation.

    Combines recent basic memory (immediate context, embedded as XML in system
    prompt) with relevant diary entries (historical context) into standard
    OpenAI messages format.
    """

    def __init__(
        self,
        basic_mgr: BasicMemoryManager,
        diary_retriever: DiaryRetriever,
    ) -> None:
        self._basic = basic_mgr
        self._diary = diary_retriever

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
        recent_n: int = 5,
        diary_top_k: int = 12,
        diary_token_budget: int = 800,
        cross_group_user_id: str = "",
        cross_group_enabled: bool = False,
        include_pending: bool = False,
    ) -> list[dict[str, str]]:
        """Build OpenAI messages array with history embedded in system prompt.

        Returns exactly two messages:
        1. system  -- enriched with diary summaries + XML conversation history
        2. user    -- the current turn (current_query)

        When cross_group_enabled is True and cross_group_user_id is provided,
        recent messages from that user in other groups are also embedded
        (marked as cross-group to avoid confusion).
        """
        # 1. Retrieve relevant diary entries (group-isolated)
        diary_entries = self._diary.retrieve(
            query=search_query or current_query,
            group_id=group_id,
            top_k=diary_top_k,
            max_tokens_budget=diary_token_budget,
        )
        logger.info(
            "ContextAssembler: group=%s | 检索到 %d 条日记 | query=%.30s...",
            group_id,
            len(diary_entries),
            search_query or current_query,
        )
        if diary_entries:
            displayed = diary_entries[:12]
            for i, entry in enumerate(displayed, 1):
                label = entry.content if i <= 5 else entry.summary
                logger.info(
                    "  [日记嵌入 %d/%d] %s", i, len(displayed), label
                )

        # 2. Build XML conversation history from recent basic memory
        history_xml = self._build_history_xml(group_id, n=recent_n, include_pending=include_pending)

        # 2b. Cross-group history for the current user
        cross_group_xml = ""
        if cross_group_enabled and cross_group_user_id:
            cross_group_xml = self._build_cross_group_history_xml(
                cross_group_user_id, exclude_group_id=group_id, n=recent_n
            )

        # 3. Compose enriched system prompt
        enriched_system = self._enrich_system_prompt(
            system_prompt, diary_entries, history_xml, cross_group_xml
        )

        return [
            {"role": "system", "content": enriched_system},
            {"role": "user", "content": current_query},
        ]

    def build_messages_with_breakdown(
        self,
        group_id: str,
        current_query: str,
        system_prompt: str,
        *,
        search_query: str = "",
        recent_n: int = 5,
        diary_top_k: int = 12,
        diary_token_budget: int = 800,
        cross_group_user_id: str = "",
        cross_group_enabled: bool = False,
        include_pending: bool = False,
    ) -> tuple[list[dict[str, str]], dict[str, int]]:
        """Build OpenAI messages array and return per-module token breakdown.

        Returns a tuple of (messages, breakdown) where breakdown contains
        token counts for diary, history_xml, and cross_group_xml sections.
        """
        diary_entries = self._diary.retrieve(
            query=search_query or current_query,
            group_id=group_id,
            top_k=diary_top_k,
            max_tokens_budget=diary_token_budget,
        )

        history_xml = self._build_history_xml(group_id, n=recent_n, include_pending=include_pending)

        cross_group_xml = ""
        if cross_group_enabled and cross_group_user_id:
            cross_group_xml = self._build_cross_group_history_xml(
                cross_group_user_id, exclude_group_id=group_id, n=recent_n
            )

        enriched_system = self._enrich_system_prompt(
            system_prompt, diary_entries, history_xml, cross_group_xml
        )

        # Compute per-module token counts
        from sirius_chat.token.utils import estimate_tokens

        breakdown: dict[str, int] = {}
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
        if history_xml:
            breakdown["history_xml"] = estimate_tokens(history_xml)
        if cross_group_xml:
            breakdown["cross_group_xml"] = estimate_tokens(cross_group_xml)

        return [
            {"role": "system", "content": enriched_system},
            {"role": "user", "content": current_query},
        ], breakdown

    def build_history_xml(self, group_id: str, n: int = 10, *, include_pending: bool = False) -> str:
        """Build XML representation of recent conversation history.

        Exported for callers (e.g. proactive / delayed responses) that want
        to embed history into their own system prompts.
        """
        return self._build_history_xml(group_id, n=n, include_pending=include_pending)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_history_xml(
        self, group_id: str, n: int = 5, *, include_pending: bool = False,
    ) -> str:
        """Convert recent basic memory entries into an XML block.

        By default, trailing entries after the last assistant reply are excluded,
        because those are the pending (unanswered) user messages that will be
        passed separately as the ``user`` role content by the caller.

        When ``include_pending=True`` (used by delayed/proactive responses),
        all recent entries are included — the caller's user content does not
        contain the pending messages, so excluding them from history would
        lose critical conversational context.
        """
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
        """Build XML of recent entries for a user across other groups."""
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
    ) -> str:
        """Convert basic memory entries into an XML block."""
        lines: list[str] = [f'<{tag}>']
        for entry in entries:
            role = entry.role
            if role == "human":
                msg_role = "user"
            elif role == "assistant":
                msg_role = "assistant"
            else:
                msg_role = "system"

            speaker = entry.speaker_name or entry.user_id or "unknown"
            safe_content = html.escape(entry.content or "", quote=False)
            safe_speaker = html.escape(speaker, quote=True)
            safe_user_id = html.escape(entry.user_id or "", quote=True)
            safe_role = html.escape(msg_role, quote=True)

            attrs = (
                f' speaker="{safe_speaker}"'
                f' user_id="{safe_user_id}"'
                f' role="{safe_role}"'
            )
            if include_group and getattr(entry, "group_id", None):
                safe_group = html.escape(entry.group_id, quote=True)
                attrs += f' group="{safe_group}"'

            lines.append(f'  <message{attrs}>{safe_content}</message>')

            # 为带图消息输出带归属的 image 标签
            if getattr(entry, "multimodal_inputs", None):
                for m in entry.multimodal_inputs:
                    if m.get("type") != "image":
                        continue
                    # 动画表情 (sub_type=1) 简化输出，不暴露本地路径
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

    @staticmethod
    def _enrich_system_prompt(
        base_prompt: str,
        diary_entries: list[Any],
        history_xml: str = "",
        cross_group_xml: str = "",
    ) -> str:
        from sirius_chat.core.prompt_factory import PromptFactory

        return PromptFactory.enrich_system_prompt(
            base_prompt=base_prompt,
            diary_entries=diary_entries,
            history_xml=history_xml,
            cross_group_xml=cross_group_xml,
        )
