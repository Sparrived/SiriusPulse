"""Diary generator: converts basic memory archive candidates into diary entries via LLM."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.memory.diary.models import DiaryEntry, DiaryGenerationResult

logger = logging.getLogger(__name__)

_DIARY_SYSTEM_PROMPT = (
    "你是日记整理助手。请根据提供的对话记录，以第一人称口吻整理成一段日记，\n"
    "并分析群聊中大家最感兴趣的话题。\n"
    "\n"
    "【对话记录格式说明】\n"
    "每条记录格式为：[稳定ID (显示名称)] 内容\n"
    "- 稳定ID：用户的唯一身份标识，不会因改昵称而变化，是识别人的主要依据\n"
    "- 显示名称：括号内的昵称/名字，可能变化，仅作辅助识别\n"
    "- assistant 的显示名称为 AI 当前人格名称\n"
    "\n"
    "【日记要求】\n"
    "- 明确提到谁（用显示名称）说了什么、做了什么、表达了什么观点\n"
    "- 通过稳定ID识别同一个人，即使显示名称不同也要视为同一人\n"
    "- 保留关键信息、用户观点、重要约定、情绪变化\n"
    "- 去除日常寒暄和重复内容\n"
    "- 口吻自然，像AI本人在回顾群聊经历\n"
    "- 正文不超过300字\n"
    "\n"
    "【话题分析要求】\n"
    "- dominant_topic: 本次对话最核心的一个话题（不超过10字）\n"
    "- interest_topics: 本次对话涉及的2-5个兴趣话题，按重要性排序\n"
    "\n"
    "严格输出 JSON，包含以下字段：\n"
    '{"content": "日记正文", "keywords": ["关键词1", "关键词2"], '
    '"summary": "一句话摘要（不超过50字）", '
    '"dominant_topic": "主导话题", '
    '"interest_topics": ["话题1", "话题2"]}'
)


def _build_diary_user_prompt(
    persona_name: str,
    persona_description: str,
    candidates: list[BasicMemoryEntry],
) -> str:
    lines: list[str] = []
    for e in candidates:
        # user_id is the stable identity key; speaker_name is the display nickname
        name = e.speaker_name if e.speaker_name else e.user_id
        lines.append(f"【{e.user_id} ({name})】{e.content}")
    conversation = "\n".join(lines)
    return (
        f"人格设定：{persona_name}，{persona_description}\n\n"
        f"以下是对话记录（格式：【稳定ID (显示名称)】内容）：\n"
        f"{conversation}\n\n"
        "请整理成日记并分析话题。记住：稳定ID是识别人的主要依据，显示名称只是辅助。"
    )


class DiaryGenerator:
    """Generates diary entries from archive candidate messages."""

    def __init__(self) -> None:
        self._last_request: Any | None = None

    async def generate(
        self,
        *,
        group_id: str,
        candidates: list[BasicMemoryEntry],
        persona_name: str,
        persona_description: str,
        provider_async: Any,
        model_name: str,
        temperature: float = 0.5,
        max_tokens: int = 512,
        max_retries: int = 2,
    ) -> DiaryGenerationResult | None:
        """Generate a diary entry from candidate messages.

        Returns None if generation fails or candidates are empty.
        On JSON parse failure, retries up to *max_retries* times with a
        stronger system prompt reminder.
        """
        if not candidates:
            return None

        from sirius_pulse.providers.base import GenerationRequest

        system_prompt = _DIARY_SYSTEM_PROMPT
        user_prompt = _build_diary_user_prompt(
            persona_name, persona_description, candidates
        )

        for attempt in range(max_retries + 1):
            request = GenerationRequest(
                model=model_name,
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                purpose="diary_generate",
            )
            self._last_request = request

            try:
                raw = await provider_async.generate_async(request)
            except Exception as exc:
                logger.warning(
                    "日记生成 LLM 调用失败 (group=%s, attempt=%d/%d): %s",
                    group_id,
                    attempt + 1,
                    max_retries + 1,
                    exc,
                )
                return None

            parsed = self._parse_response(raw)
            if parsed:
                break

            if attempt < max_retries:
                logger.warning(
                    "日记生成响应 JSON 解析失败 (group=%s, attempt=%d/%d)，准备重试",
                    group_id,
                    attempt + 1,
                    max_retries + 1,
                )
                system_prompt = (
                    _DIARY_SYSTEM_PROMPT
                    + "\n\n【重要提醒】上一次的输出不是合法 JSON，"
                    "请确保本次输出是严格合法的 JSON 对象，不要包含任何其他文字。"
                )
            else:
                logger.warning(
                    "日记生成响应 JSON 解析失败 (group=%s)，已耗尽 %d 次重试",
                    group_id,
                    max_retries + 1,
                )
                return None

        now_iso = datetime.now(timezone.utc).isoformat()
        entry = DiaryEntry(
            entry_id=f"dgy_{uuid.uuid4().hex[:12]}",
            group_id=group_id,
            created_at=now_iso,
            source_ids=[e.entry_id for e in candidates],
            content=parsed.get("content", "")[:300],
            keywords=[str(k).strip() for k in parsed.get("keywords", []) if str(k).strip()][:10],
            summary=parsed.get("summary", "")[:50],
        )
        dominant_topic = str(parsed.get("dominant_topic", "")).strip()[:20]
        interest_topics = [
            str(t).strip() for t in parsed.get("interest_topics", [])
            if str(t).strip()
        ][:10]
        return DiaryGenerationResult(
            entry=entry,
            dominant_topic=dominant_topic,
            interest_topics=interest_topics,
        )

    @staticmethod
    def _parse_response(raw: str) -> dict[str, Any] | None:
        text = raw.strip()
        if "```" in text:
            parts = text.split("```")
            if len(parts) >= 3:
                text = parts[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
        try:
            result = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("日记生成响应 JSON 解析失败")
            return None
        if isinstance(result, dict):
            return result
        return None
