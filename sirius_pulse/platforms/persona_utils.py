"""人格生成公共工具。

供 WebUI 复用，包含问卷问题、JSON Schema、以及基于 LLM 的生成函数。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.core.orchestration_store import OrchestrationStore
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.providers.base import GenerationRequest

LOG = logging.getLogger("sirius.platforms.persona_utils")

# 问卷问题（深度定制用）
INTERVIEW_QUESTIONS = [
    "如果把 TA 放进群聊，TA 更像哪类群体角色？是活跃气氛的人、冷幽默观察者、可靠收束者，还是偶尔出手的梗王？",
    "TA 在多人对话里的发言节奏如何？什么时候会抢话、接梗、补刀、收尾，什么时候会选择潜水？",
    "TA 如何区分群内不同关系层级？公开场合和私下场合，对熟人和生人会有什么明显区别？",
    "群里气氛好、被冷落、有人争执、有人单独 cue TA 时，TA 的情绪和反应路径分别是什么？",
    "TA 的群聊语言风格是什么？会不会用梗、方言、昵称、复读、反问、表情包式句法？最该避免哪些 AI 味回复？",
    "TA 在群聊中的边界与禁忌是什么？面对多人起哄、越界玩笑、道德绑架或拉踩时会怎么处理？",
    "TA 在群里最真实的小习惯或记忆点是什么？什么细节会让人一看就觉得「这人很具体」？",
    "这个群聊角色的社交气质从什么经历里长出来？哪些过去的圈子、职业或成长环境塑造了 TA 的群体互动方式？",
]

# 问卷生成期望的 JSON Schema（与 v1.0 PersonaProfile 对应）
PERSONA_JSON_SCHEMA = {
    "persona_summary": "一句话描述",
    "personality_traits": ["特质1", "特质2"],
    "backstory": "背景故事（可选）",
    "communication_style": "说话风格描述",
    "emoji_preference": "heavy/moderate/light/none",
    "emotional_baseline": {"valence": 0.0, "arousal": 0.3},
    "boundaries": ["边界1"],
    "social_role": "observer/mediator/leader/jester/caregiver",
}


def extract_json(text: str) -> str:
    """从 markdown 代码块中提取 JSON。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


async def generate_persona_from_interview(
    work_path: Path,
    provider: Any,
    name: str,
    answers: dict[str, str],
    aliases: list[str] | None = None,
    model: str | None = None,
) -> PersonaProfile:
    """基于问卷回答通过 LLM 生成人格设定。"""
    if model is None:
        orch = OrchestrationStore.load(work_path)
        model = orch.get("analysis_model", "gpt-4o-mini")

    qa_lines = []
    for i, q in enumerate(INTERVIEW_QUESTIONS, 1):
        a = answers.get(str(i), "")
        if a:
            qa_lines.append(f"Q{i}: {q}\nA: {a}")
    qa_text = "\n\n".join(qa_lines)

    prompt = (
        f"你是一位专业的角色设计师。请根据以下问卷回答，"
        f"为群聊角色「{name}」设计一个完整的角色设定。\n\n"
        f"{qa_text}\n\n"
        f"请输出严格JSON格式，包含以下字段：\n"
        f"{json.dumps(PERSONA_JSON_SCHEMA, ensure_ascii=False, indent=2)}\n"
        f"只输出JSON，不要其他内容。"
    )

    # 过程持久化
    pending_path = work_path / "engine_state" / "pending_persona_interview.json"
    pending_state = {
        "name": name,
        "aliases": aliases or [],
        "answers": answers,
        "prompt": prompt,
        "model": model,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = pending_path.with_suffix(pending_path.suffix + ".tmp")
    tmp.write_text(json.dumps(pending_state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(pending_path)
    LOG.debug("Pending persona interview state saved to %s", pending_path)

    request = GenerationRequest(
        model=model,
        system_prompt="",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=2048,
        purpose="persona_generate",
    )

    if provider is None:
        raise RuntimeError("未配置 Provider，无法生成人格")

    try:
        if hasattr(provider, "generate_async"):
            raw = await provider.generate_async(request)
        else:
            import asyncio

            raw = await asyncio.to_thread(provider.generate, request)
    except Exception as exc:
        LOG.error("LLM 人格生成失败，pending state 保留在 %s: %s", pending_path, exc)
        raise

    # 保存 completed record
    record_path = work_path / "engine_state" / "persona_interview_record.json"
    pending_state["status"] = "completed"
    pending_state["completed_at"] = datetime.now(timezone.utc).isoformat()
    tmp = record_path.with_suffix(record_path.suffix + ".tmp")
    tmp.write_text(json.dumps(pending_state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(record_path)
    LOG.info("Persona interview record saved to %s", record_path)

    # 解析 JSON
    data = extract_json(raw)
    parsed = json.loads(data)

    return PersonaProfile(
        name=name,
        aliases=aliases or [],
        source="interview",
        created_at=datetime.now(timezone.utc).isoformat(),
        persona_summary=parsed.get("persona_summary", ""),
        personality_traits=parsed.get("personality_traits", []),
        backstory=parsed.get("backstory", ""),
        communication_style=parsed.get("communication_style", ""),
        emoji_preference=parsed.get("emoji_preference", ""),
        emotional_baseline=parsed.get("emotional_baseline", {"valence": 0.2, "arousal": 0.3}),
        boundaries=parsed.get("boundaries", []),
        social_role=parsed.get("social_role", ""),
    )
