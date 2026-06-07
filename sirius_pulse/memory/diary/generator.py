"""Diary generator: converts basic memory archive candidates into diary entries via LLM."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.memory.diary.models import DiaryEntry, DiaryGenerationResult
from sirius_pulse.memory.situation.models import Situation

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
        brain: Any,
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

        from sirius_pulse.core.brain import RawRequest

        system_prompt = _DIARY_SYSTEM_PROMPT
        user_prompt = _build_diary_user_prompt(persona_name, persona_description, candidates)

        parsed: dict[str, Any] | None = None
        for attempt in range(max_retries + 1):
            raw_request = RawRequest(
                model=model_name,
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                purpose="diary_generate",
                response_format={"type": "json_object"},
            )

            try:
                raw = await brain.raw_call(raw_request)
            except Exception as exc:
                logger.error(
                    "日记生成 LLM 调用已耗尽所有重试 (group=%s): %s",
                    group_id,
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
                    _DIARY_SYSTEM_PROMPT + "\n\n【重要提醒】上一次的输出不是合法 JSON，"
                    "请确保本次输出是严格合法的 JSON 对象，不要包含任何其他文字。"
                )
            else:
                logger.warning(
                    "日记生成响应 JSON 解析失败 (group=%s)，已耗尽 %d 次重试",
                    group_id,
                    max_retries + 1,
                )
                return None

        assert parsed is not None
        now_iso = datetime.now(timezone.utc).isoformat()
        entry = DiaryEntry(
            entry_id=f"dgy_{uuid.uuid4().hex[:12]}",
            group_id=group_id,
            created_at=now_iso,
            source_ids=[e.entry_id for e in candidates],
            content=(parsed.get("content") or "")[:300],
            keywords=[str(k).strip() for k in (parsed.get("keywords") or []) if str(k).strip()][
                :10
            ],
            summary=(parsed.get("summary") or "")[:50],
        )
        dominant_topic = str(parsed.get("dominant_topic") or "").strip()[:20]
        interest_topics = [
            str(t).strip() for t in (parsed.get("interest_topics") or []) if str(t).strip()
        ][:10]
        return DiaryGenerationResult(
            entry=entry,
            dominant_topic=dominant_topic,
            interest_topics=interest_topics,
        )

    # ── 从 Situation 生成日记（新架构）──

    _SITUATION_DIARY_PROMPT = """你是 {persona_name}，{persona_description}。
你正在回顾今天在群里的经历，写一篇日记。

今天的经历摘要（按时间顺序）：
{situations_text}

写作要求：
- 以"{persona_name}"的第一人称口吻书写，像你本人在回忆今天的经历
- 表达你自己的感受、看法、立场，不要旁观者视角
- 明确提到每个人做了什么、说了什么，用自然的口吻转述
- 保留重要信息、观点、约定、情绪变化
- 去除重复内容
- 如果今天有人提到了值得记住的事情（约定、计划、重要决定），重点记录
- 不限制长度，充分叙述

输出 JSON：
{{"content": "日记正文（以{persona_name}的口吻）", "keywords": ["关键词1", "关键词2"], "summary": "一句话摘要（不超过50字）"}}
"""

    async def generate_from_situations(
        self,
        *,
        group_id: str,
        situations: list[Situation],
        persona_name: str,
        persona_description: str,
        brain: Any,
        model_name: str,
        temperature: float = 0.5,
        max_tokens: int = 2048,
        max_retries: int = 2,
    ) -> dict[str, Any] | None:
        """从 Situation 列表生成日记。

        与 generate() 的区别：
        - 输入是 Situation（已验证的结构化压缩），而非原始消息
        - 不限制字数，让 LLM 充分叙述
        - 后续由 DiarySlicer 负责切片

        Returns:
            {"content": "...", "keywords": [...], "summary": "..."} 或 None
        """
        if not situations:
            return None

        from sirius_pulse.core.brain import RawRequest

        # 构建情景摘要文本
        situations_text = self._build_situations_text(situations)
        system_prompt = self._SITUATION_DIARY_PROMPT.format(
            persona_name=persona_name,
            persona_description=persona_description,
            situations_text=situations_text,
        )

        parsed: dict[str, Any] | None = None
        for attempt in range(max_retries + 1):
            raw_request = RawRequest(
                model=model_name,
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": "请根据以上经历写日记。"}],
                temperature=temperature,
                max_tokens=max_tokens,
                purpose="diary_from_situation",
                response_format={"type": "json_object"},
            )

            try:
                raw = await brain.raw_call(raw_request)
            except Exception as exc:
                logger.error(
                    "Situation 日记生成 LLM 调用失败 (group=%s): %s",
                    group_id,
                    exc,
                )
                return None

            parsed = self._parse_response(raw)
            if parsed and parsed.get("content"):
                break

            if attempt < max_retries:
                logger.warning(
                    "Situation 日记生成 JSON 解析失败 (group=%s, attempt=%d)",
                    group_id,
                    attempt + 1,
                )
                system_prompt += "\n\n【重要提醒】请确保输出是严格合法的 JSON 对象。"
            else:
                logger.warning(
                    "Situation 日记生成 JSON 解析失败 (group=%s)，已耗尽重试",
                    group_id,
                )
                return None

        return parsed

    @staticmethod
    def _build_situations_text(situations: list[Situation]) -> str:
        """将 Situation 列表转为 LLM 可读的摘要文本。"""
        lines = []
        for idx, sit in enumerate(situations, 1):
            time_str = ""
            if sit.time_range_start:
                try:
                    dt = datetime.fromisoformat(sit.time_range_start.replace("Z", "+00:00"))
                    time_str = f" ({dt.strftime('%H:%M')})"
                except (ValueError, TypeError):
                    pass

            participants_str = ""
            if sit.participants:
                participants_str = f" [参与者: {', '.join(sit.participants[:5])}]"

            lines.append(f"【片段{idx}{time_str}{participants_str}】{sit.summary}")

        return "\n".join(lines)

    # ── 内部工具 ──

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
