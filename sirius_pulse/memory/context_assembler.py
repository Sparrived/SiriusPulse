"""Context assembler: builds LLM messages from basic memory + diary RAG.

方案 C：历史消息以 assistant 消息切分，构造 user-assistant 消息链。
每个 assistant 回复前的 user/system 消息合并为一个 user 消息（XML 格式），
assistant 回复单独作为一条消息。这样既保留了 XML 的结构化语义（speaker、user_id），
又让 LLM 能更好地理解对话流。
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sirius_pulse.memory.basic.manager import BasicMemoryManager
from sirius_pulse.memory.diary.indexer import DiaryRetriever

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
        biography_card: Any = None,
    ) -> list[dict[str, Any]]:
        """构建消息链（方案 C：以 assistant 消息切分）。

        返回多条消息：
        1. system  -- 富化后的系统提示词（含日记）
        2. user/assistant 交替 -- 历史对话（按 assistant 切分）
        3. user   -- 当前用户消息

        每个 assistant 回复前的 user/system 消息合并为一个 user 消息（XML 格式），
        assistant 回复单独作为一条消息。
        """
        # 1. Retrieve relevant diary entries (group-isolated)
        enriched_query = self._enrich_search_query(
            search_query or current_query, biography_card
        )
        diary_entries = self._diary.retrieve(
            query=enriched_query,
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

        # 2. 构建富化后的系统提示词（只含日记，不含历史）
        enriched_system = self._enrich_system_prompt(
            system_prompt, diary_entries, history_xml="", cross_group_xml=""
        )

        # 3. 构建消息链（方案 C）
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": enriched_system}
        ]

        # 获取历史条目并按 assistant 切分
        recent = self._basic.get_context(group_id, n=recent_n)
        if recent and not include_pending:
            # 排除 assistant 最后回复之后的未回复用户消息
            last_assistant_idx = -1
            for i in range(len(recent) - 1, -1, -1):
                if recent[i].role == "assistant":
                    last_assistant_idx = i
                    break
            if last_assistant_idx >= 0:
                recent = recent[: last_assistant_idx + 1]

        if recent:
            # 按 assistant 消息切分，构造 user-assistant 消息链
            current_user_entries: list[Any] = []
            for entry in recent:  # type: ignore[assignment]
                if entry.role == "assistant":  # type: ignore[attr-defined]
                    # 将之前累积的 user/system 消息打包为一个 user 消息
                    if current_user_entries:
                        xml_content = self._entries_to_xml(current_user_entries)
                        messages.append({"role": "user", "content": xml_content})
                        current_user_entries = []
                    # 添加 assistant 消息
                    messages.append({"role": "assistant", "content": entry.content or ""})
                else:
                    # user/system 消息累积到当前批次
                    current_user_entries.append(entry)

            # 处理剩余的 user/system 消息（未被 assistant 回复的）
            if current_user_entries:
                xml_content = self._entries_to_xml(current_user_entries)
                messages.append({"role": "user", "content": xml_content})

        # 4. 添加当前用户消息
        messages.append({"role": "user", "content": current_query})

        return messages

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
        biography_card: Any = None,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """构建消息链并返回 token 分布统计。

        Returns a tuple of (messages, breakdown) where breakdown contains
        token counts for diary and history sections.
        """
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
            biography_card=biography_card,
        )

        # Compute per-module token counts
        from sirius_pulse.token.utils import estimate_tokens

        breakdown: dict[str, int] = {}
        if messages:
            # 计算 diary 部分的 token
            enriched_query = self._enrich_search_query(
                search_query or current_query, biography_card
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

            # 计算历史消息部分的 token
            history_tokens = 0
            for msg in messages:
                if msg.get("role") in ("user", "assistant") and msg.get("content"):
                    history_tokens += estimate_tokens(str(msg["content"]))
            breakdown["history"] = history_tokens

        return messages, breakdown

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

        When ``include_pending=True`` (used by proactive responses where
        caller's user content is just topic context or "..."), all recent
        entries are included — the caller's user content does not contain
        the pending messages, so excluding them from history would lose
        critical conversational context.
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
        _tz_cn = timezone(timedelta(hours=8))
        lines: list[str] = [f'<{tag}>']
        for entry in entries:
            speaker = entry.speaker_name or entry.user_id or "unknown"
            safe_content = html.escape(entry.content or "", quote=False)
            safe_speaker = html.escape(speaker, quote=True)
            safe_user_id = html.escape(entry.user_id or "", quote=True)

            # 从 ISO 时间戳提取时分秒
            ts_str = ""
            raw_ts = getattr(entry, "timestamp", "")
            if raw_ts:
                try:
                    ts_str = datetime.fromisoformat(raw_ts).astimezone(_tz_cn).strftime("%H:%M:%S")
                except (ValueError, TypeError):
                    ts_str = ""

            attrs = f' speaker="{safe_speaker}" user_id="{safe_user_id}"'
            if ts_str:
                attrs += f' time="{ts_str}"'
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
        from sirius_pulse.core.prompt_factory import PromptFactory

        return PromptFactory.enrich_system_prompt(
            base_prompt=base_prompt,
            diary_entries=diary_entries,
            history_xml=history_xml,
            cross_group_xml=cross_group_xml,
        )

    @staticmethod
    def _enrich_search_query(base_query: str, biography_card: Any) -> str:
        """用传记卡信息丰富日记检索 query，提高对"此人相关"日记的命中率。

        将用户姓名、身份锚点和传记摘要追加到原始 query 后，
        使语义检索和关键词检索都能兼顾"内容相关"和"人物相关"。
        """
        if biography_card is None:
            return base_query

        bio_parts = []
        if biography_card.name:
            bio_parts.append(biography_card.name)
        if biography_card.identity_anchors:
            bio_parts.extend(biography_card.identity_anchors[:3])
        if biography_card.short_bio:
            bio_parts.append(biography_card.short_bio[:100])

        if not bio_parts:
            return base_query

        enriched = f"{base_query} {' '.join(bio_parts)}"
        return enriched[:500]
