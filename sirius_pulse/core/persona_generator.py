"""Persona generator: creates rich character profiles for EmotionalGroupChatEngine.

Two creation paths:
  1. Template-based (zero cost) — built-in archetypes
  2. Interview-based (rich) — Q&A questionnaire → LLM generation

Also provides a bridge to convert legacy roleplay `AgentPreset` → `PersonaProfile`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sirius_pulse.models.persona import PersonaProfile

logger = logging.getLogger(__name__)


# ============================================================================
# Phase 1: Template archetypes (zero-cost)
# ============================================================================

_ARCHETYPES: dict[str, dict[str, Any]] = {}


# ============================================================================
# Interview questionnaire
# ============================================================================

_INTERVIEW_QUESTIONS: list[str] = [
    "如果用三个词形容自己，你会选哪三个？",
    "群里有人吵架时，你通常会怎么做？",
    "你最喜欢的聊天方式是什么？（幽默/严肃/随意/正式）",
    "有没有什么是你绝对不会在群里聊的？",
    "当朋友难过时，你会怎么安慰TA？",
    "你觉得自己在群里更像什么角色？（开心果/和事佬/旁观者/带头大哥/贴心小棉袄）",
    "你平时用表情包多吗？",
]


# ============================================================================
# Generator class
# ============================================================================


class PersonaGenerator:
    """Creates PersonaProfile via templates or interview."""

    @staticmethod
    def from_template(archetype_name: str) -> PersonaProfile:
        """Create persona from a built-in archetype (zero LLM cost)."""
        data = _ARCHETYPES.get(archetype_name)
        if data is None:
            raise ValueError(
                f"Unknown archetype: {archetype_name}. " f"Available: {list(_ARCHETYPES.keys())}"
            )

        profile = PersonaProfile(
            source="template",
            created_at=datetime.now(timezone.utc).isoformat(),
            **data,
        )
        return profile

    @staticmethod
    def from_interview(
        name: str,
        answers: dict[str, str],
        provider_async: Any,
        model: str = "gpt-4o-mini",
    ) -> PersonaProfile:
        """Create persona from Q&A answers via LLM generation."""
        if provider_async is None:
            raise ValueError("Interview-based persona generation requires a provider")

        prompt = PersonaGenerator._build_interview_prompt(name, answers)
        request = _build_llm_request(prompt, purpose="persona_generate", model=model)

        try:
            if hasattr(provider_async, "generate_async"):
                raw = _run_async(provider_async.generate_async, request)
            else:
                raw = _run_sync(provider_async.generate, request)
        except Exception as exc:
            raise RuntimeError(f"LLM persona generation failed: {exc}") from exc

        return PersonaGenerator._parse_llm_persona_output(name, raw)

    # ------------------------------------------------------------------
    # Interview prompt builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_interview_prompt(name: str, answers: dict[str, str]) -> str:
        qa_lines = []
        for i, q in enumerate(_INTERVIEW_QUESTIONS, 1):
            a = answers.get(str(i), answers.get(q, ""))
            if a:
                qa_lines.append(f"Q{i}: {q}\nA: {a}")

        qa_text = "\n\n".join(qa_lines)

        return (
            f"你是一位专业的角色设计师。请根据以下问卷回答，"
            f"为群聊角色「{name}」设计一个完整的角色设定。\n\n"
            f"{qa_text}\n\n"
            f"请输出严格JSON格式，包含以下字段：\n"
            f"{json.dumps(_PERSONA_JSON_SCHEMA, ensure_ascii=False, indent=2)}\n"
            f"只输出JSON，不要其他内容。"
        )

    @staticmethod
    def _parse_llm_persona_output(name: str, raw: str) -> PersonaProfile:
        data = json.loads(_extract_json(raw))
        return PersonaProfile(
            name=name,
            source="interview",
            created_at=datetime.now(timezone.utc).isoformat(),
            persona_summary=data.get("persona_summary", ""),
            personality_traits=data.get("personality_traits", []),
            backstory=data.get("backstory", ""),
            core_values=data.get("core_values", []),
            flaws=data.get("flaws", []),
            communication_style=data.get("communication_style", ""),
            emoji_preference=data.get("emoji_preference", ""),
            emotional_baseline=data.get("emotional_baseline", {"valence": 0.2, "arousal": 0.3}),
            boundaries=data.get("boundaries", []),
            social_role=data.get("social_role", ""),
        )

    # ------------------------------------------------------------------
    # Roleplay preset bridge
    # ------------------------------------------------------------------

    @staticmethod
    def from_roleplay_preset(agent_preset: Any) -> PersonaProfile:
        """Convert legacy AgentPreset → PersonaProfile.

        Best-effort parsing of agent persona keywords and global_system_prompt.
        """
        from sirius_pulse.config.models import AgentPreset

        if not isinstance(agent_preset, AgentPreset):
            raise TypeError(f"Expected AgentPreset, got {type(agent_preset)}")

        agent = agent_preset.agent
        prompt = agent_preset.global_system_prompt or ""

        profile = PersonaProfile(
            name=agent.name,
            source="roleplay_bridge",
            created_at=datetime.now(timezone.utc).isoformat(),
            temperature_preference=agent.temperature,
            max_tokens_preference=agent.max_tokens,
        )

        # Parse persona traits
        if agent.persona:
            traits = [t.strip() for t in agent.persona.split("/") if t.strip()]
            profile.personality_traits = traits

        # Parse global_system_prompt for structured sections
        profile.full_system_prompt = prompt

        # Best-effort extraction from common section headers
        _extract_section(prompt, "说话风格", profile, "communication_style")
        _extract_section(prompt, "边界", profile, "boundaries", list_mode=True)

        # If prompt is short enough, use it as backstory too
        if 50 < len(prompt) < 500:
            profile.backstory = prompt[:497] + "..." if len(prompt) > 500 else prompt

        return profile


# ============================================================================
# Helpers
# ============================================================================

_PERSONA_JSON_SCHEMA = {
    "persona_summary": "一句话描述",
    "personality_traits": ["特质1", "特质2"],
    "backstory": "背景故事（可选）",
    "communication_style": "说话风格描述",
    "emoji_preference": "heavy/moderate/light/none",
    "emotional_baseline": {"valence": 0.0, "arousal": 0.3},
    "boundaries": ["边界1"],
    "social_role": "observer/mediator/leader/jester/caregiver",
}


def _build_llm_request(
    prompt: str, *, purpose: str = "persona_generate", model: str = "gpt-4o-mini"
) -> Any:
    from sirius_pulse.providers.base import GenerationRequest

    return GenerationRequest(
        model=model,
        system_prompt="",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=2048,
        purpose=purpose,
    )


def _run_async(coro, request):
    import asyncio

    try:
        # 如果当前线程没有运行中的事件循环，直接使用
        loop = asyncio.get_event_loop()
        if not loop.is_running():
            return loop.run_until_complete(coro(request))
    except RuntimeError:
        pass
    # 已有事件循环在运行（如在 async 函数中被调用），创建新 loop 在新线程运行
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.new_event_loop().run_until_complete(coro(request)))
        return future.result()


def _run_sync(func, request):
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if not loop.is_running():
            return loop.run_until_complete(asyncio.to_thread(func, request))
    except RuntimeError:
        pass
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            lambda: asyncio.new_event_loop().run_until_complete(asyncio.to_thread(func, request))
        )
        return future.result()


def _extract_json(text: str) -> str:
    """Extract JSON object from text that may have markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first fence line
        if lines[0].startswith("```"):
            lines = lines[1:]
        # Remove last fence line
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _extract_section(
    prompt: str,
    keyword: str,
    profile: PersonaProfile,
    attr: str,
    *,
    list_mode: bool = False,
) -> None:
    """Best-effort extraction of a section from global_system_prompt."""
    import re

    # Look for keyword followed by content until next section or end
    pattern = re.compile(
        rf"{re.escape(keyword)}\s*[：:]\s*(.+?)(?=\n\s*(?:{ '|'.join(re.escape(h) for h in _SECTION_HEADERS )}|$)",
        re.DOTALL | re.IGNORECASE,
    )
    m = pattern.search(prompt)
    if m:
        content = m.group(1).strip()
        if list_mode:
            items = [s.strip("- *• ") for s in content.split("\n") if s.strip()]
            setattr(profile, attr, items[:5])
        else:
            setattr(profile, attr, content[:200])


_SECTION_HEADERS = "角色简介性格特质说话风格情感表达行为边界价值观"
