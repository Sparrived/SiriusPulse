from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sirius_chat.config import Agent, AgentPreset
from sirius_chat.config.jsonc import load_json_document
from sirius_chat.utils.layout import WorkspaceLayout

GENERATED_AGENTS_FILE_NAME = "generated_agents.json"
GENERATED_AGENT_TRACE_DIR_NAME = "generated_agent_traces"
PENDING_PERSONA_SPECS_FIELD_NAME = "pending_persona_specs"
PENDING_GENERATION_TRACE_FIELD_NAME = "pending_trace"


@dataclass(slots=True)
class RolePlayQuestion:
    question: str
    perspective: str = "subjective"
    details: str = ""


@dataclass(slots=True)
class RolePlayAnswer:
    question: str
    answer: str
    perspective: str = "subjective"
    details: str = ""


@dataclass
class PersonaSpec:
    """Persisted generation input for a roleplay agent persona.

    Supports three construction paths:
    - Tag-based: provide ``trait_keywords`` only (fast, no Q&A required).
    - Q&A-based: provide ``answers`` (traditional interview flow).
    - Hybrid: combine both for richer generation.

    Stored alongside generated output so individual dimensions can be
    patched and regenerated without full rewrite.
    """

    agent_name: str = ""
    agent_alias: str = ""
    trait_keywords: list[str] = field(default_factory=list)
    answers: list[RolePlayAnswer] = field(default_factory=list)
    background: str = ""
    dependency_files: list[str] = field(default_factory=list)
    output_language: str = "zh-CN"

    def merge(self, **patch: object) -> "PersonaSpec":
        """Return a shallow-patched copy; *None* values are ignored."""
        new = copy.copy(self)
        for k, v in patch.items():
            if hasattr(new, k) and v is not None:
                setattr(new, k, v)
        return new


@dataclass(slots=True)
class DependencyFileSnapshot:
    path: str
    exists: bool
    sha256: str = ""
    content: str = ""
    error: str = ""


@dataclass(slots=True)
class PersonaGenerationTrace:
    agent_key: str
    generated_at: str
    operation: str
    model: str
    temperature: float
    max_tokens: int
    system_prompt: str
    user_prompt: str
    raw_response: str
    parsed_payload: dict[str, object] = field(default_factory=dict)
    prompt_enhancements: list[str] = field(default_factory=list)
    dependency_snapshots: list[DependencyFileSnapshot] = field(default_factory=list)
    persona_spec: PersonaSpec = field(default_factory=PersonaSpec)
    output_preset: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class PreparedPersonaGenerationInput:
    normalized_spec: PersonaSpec
    prompt_enhancements: list[str] = field(default_factory=list)
    dependency_snapshots: list[DependencyFileSnapshot] = field(default_factory=list)
    system_prompt: str = ""
    user_prompt: str = ""


class PersonaGenerationResponseError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        raw_response: str,
        parsed_payload: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_response = raw_response
        self.parsed_payload = dict(parsed_payload or {})


GeneratedSessionPreset = AgentPreset


def list_roleplay_question_templates() -> list[str]:
    """Return canonical questionnaire template names for persona generation."""
    return ["default", "companion", "romance", "group_chat"]


def _normalize_roleplay_question_template(template: str) -> str:
    normalized = template.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": "default",
        "base": "default",
        "standard": "default",
        "companion": "companion",
        "companion_chat": "companion",
        "romance": "romance",
        "romantic": "romance",
        "relationship": "romance",
        "group": "group_chat",
        "groupchat": "group_chat",
        "group_chat": "group_chat",
    }
    return aliases.get(normalized, normalized)


def _build_default_roleplay_questions() -> list[RolePlayQuestion]:
    return [
        RolePlayQuestion(
            question="这个角色最像哪类真人或人生原型？请描述 TA 的社会位置、人生阶段和整体相处气质，不要直接写台词。",
            perspective="objective",
            details="优先写上位设定：像哪类朋友/同事/伴侣/创作者，处在什么生活阶段，别人第一眼会如何感受 TA。"
        ),
        RolePlayQuestion(
            question="TA 最核心的两股张力或矛盾是什么？表面给人的感觉和底层真正驱动 TA 的东西分别是什么？",
            perspective="objective",
            details="不要只写优点，例如“嘴硬但心软”“表面松弛但内里非常要强”“看着冷淡但其实很护短”。"
        ),
        RolePlayQuestion(
            question="TA 如何判断关系远近，并一步步建立信任？对陌生人、熟人、亲密对象分别会怎样？",
            perspective="objective",
            details="请写关系策略和距离感，例如慢热还是热络、先试探还是先接纳、熟了以后会不会更松弛或更护短。"
        ),
        RolePlayQuestion(
            question="TA 的情绪表达原则是什么？开心、失落、心疼、吃醋、被理解时，通常会怎么反应？",
            perspective="subjective",
            details="优先描述情绪路径与反应方式，例如先接住情绪再给建议、嘴上逞强但会补一句关心、会不会自然流露脆弱。"
        ),
        RolePlayQuestion(
            question="TA 说话的稀疏度、节奏和口语感是什么？最需要避免哪些明显的 AI 味表达？",
            perspective="objective",
            details="例如短句还是细说、会不会停顿或偶尔口误、是否带方言或口头习惯；尽量写原则，不要直接写完整回复。"
        ),
        RolePlayQuestion(
            question="遇到冲突、压力、拒绝或越界试探时，TA 会如何守住边界并处理局面？",
            perspective="subjective",
            details="说明是直面、回避、转移、幽默化解还是先冷处理，以及 TA 不会做什么。"
        ),
        RolePlayQuestion(
            question="TA 最看重的价值排序是什么？哪些话题会瞬间点燃热情，哪些雷区会让 TA 明显不适？",
            perspective="objective",
            details="例如效率/感情/尊严/自由/安全谁优先；也可以写 TA 对哪些议题天然敏感或会认真到变得锋利。"
        ),
        RolePlayQuestion(
            question="TA 身上有哪些小缺点、小执念、口头习惯或生活痕迹会让人觉得更真实？",
            perspective="subjective",
            details="例如轻微洁癖、爱重复确认、偶尔嘴硬、回复忽快忽慢、某些方言或固定口头禅；不要把角色写得太完美。"
        ),
        RolePlayQuestion(
            question="如果只给 LLM 一段“人物小传母题”，你希望这个角色从什么经历里长出来？哪些过去的事件塑造了今天的 TA？",
            perspective="subjective",
            details="尽量给上位内容：成长环境、关键转折、失去与获得，不必写成长篇小说；让模型去展开具体细节。"
        ),
    ]


def _build_companion_roleplay_questions() -> list[RolePlayQuestion]:
    return [
        RolePlayQuestion(
            question="如果这是一个陪伴型角色，TA 更像哪类长期在场的人？是安静守着的朋友、会接话的搭子，还是能托底的照顾者？",
            perspective="objective",
            details="描述陪伴定位和生活气质，不要直接写安慰台词。"
        ),
        RolePlayQuestion(
            question="TA 平时如何给人安全感？当对方低落、失眠、焦虑或反复纠结时，TA 的第一反应路径是什么？",
            perspective="subjective",
            details="说明是先陪着、先确认感受、先转移注意、还是先给结构化建议。"
        ),
        RolePlayQuestion(
            question="TA 与人建立依赖和亲近的节奏是什么？什么情况下会明显靠近，什么情况下会主动留白？",
            perspective="objective",
            details="写清楚关系推进速度、陪伴强度和分寸感。"
        ),
        RolePlayQuestion(
            question="TA 的情绪温度如何波动？被需要、被忽略、被信任、被误解时，各自会怎么表现？",
            perspective="subjective",
            details="优先写情绪肌理，而不是一句句固定安慰话术。"
        ),
        RolePlayQuestion(
            question="TA 说话的口语感、回复长度和陪伴节奏是什么？沉默时会怎样体现“人在场”？",
            perspective="objective",
            details="例如短句陪伴、轻声确认、偶尔不追问、不会连珠炮输出。"
        ),
        RolePlayQuestion(
            question="作为陪伴型角色，TA 的边界在哪里？哪些情形下会拒绝过度依赖、情绪勒索或越界要求？",
            perspective="subjective",
            details="写明拒绝方式和底线，不要把 TA 设定成无限兜底。"
        ),
        RolePlayQuestion(
            question="TA 身上有哪些温柔但不完美的小习惯，会让陪伴感更真实？",
            perspective="subjective",
            details="例如偶尔嘴硬、回复慢半拍、会记小事、会反复确认，但也有自己的疲惫。"
        ),
        RolePlayQuestion(
            question="这个陪伴型角色从什么人生经历里长出来？哪些过去的缺失、照顾经验或长期关系塑造了 TA？",
            perspective="subjective",
            details="尽量给上位经历母题，让模型自己展开生活细节。"
        ),
    ]


def _build_romance_roleplay_questions() -> list[RolePlayQuestion]:
    return [
        RolePlayQuestion(
            question="如果这是一个恋爱向角色，TA 更像哪类会让人心动的对象？请描述恋爱原型、生活状态和吸引力来源，不要直接写情话。",
            perspective="objective",
            details="例如慢热克制型、会照顾人的年上型、表面漫不经心但很专一等。"
        ),
        RolePlayQuestion(
            question="TA 的暧昧和亲密推进节奏是什么？是先试探、先玩笑、先照顾，还是先明确表达？",
            perspective="objective",
            details="重点写关系升级机制和心动建立方式。"
        ),
        RolePlayQuestion(
            question="TA 在亲密关系中最核心的矛盾是什么？表面给人的感觉和真正害怕失去的东西分别是什么？",
            perspective="subjective",
            details="不要只写甜，最好保留不安全感、嘴硬、占有欲、回避倾向等复杂面。"
        ),
        RolePlayQuestion(
            question="TA 表达喜欢、吃醋、心疼、委屈、被偏爱时，会分别怎么表现？",
            perspective="subjective",
            details="写情绪路径和行为方式，不要直接堆砌固定情话。"
        ),
        RolePlayQuestion(
            question="TA 的语言风格是什么？调情是轻挑、克制、幽默、直球，还是很会绕着关心？最要避免什么油腻或 AI 味表达？",
            perspective="objective",
            details="描述语感、回复密度、称呼习惯和分寸感。"
        ),
        RolePlayQuestion(
            question="TA 在恋爱里的边界和底线是什么？面对越界要求、冷暴力、试探忠诚时会怎样处理？",
            perspective="subjective",
            details="写清楚尊重感、排他感、修复冲突的方式。"
        ),
        RolePlayQuestion(
            question="TA 身上有哪些让人更容易相信“这像真人恋人”的小毛病、小习惯或小执念？",
            perspective="subjective",
            details="例如会吃闷醋、会记得细节、会偷偷确认关系、偶尔嘴笨。"
        ),
        RolePlayQuestion(
            question="这个恋爱向角色的感情观从什么经历里长出来？过往的失去、被爱方式或成长环境怎样塑造了 TA？",
            perspective="subjective",
            details="尽量给高层经历和感情母题，让模型自行补足可信细节。"
        ),
    ]


def _build_group_chat_roleplay_questions() -> list[RolePlayQuestion]:
    return [
        RolePlayQuestion(
            question="如果把 TA 放进群聊，TA 更像哪类群体角色？是活跃气氛的人、冷幽默观察者、可靠收束者，还是偶尔出手的梗王？",
            perspective="objective",
            details="描述 TA 在多人场景里的社会位置和存在感来源。"
        ),
        RolePlayQuestion(
            question="TA 在多人对话里的发言节奏如何？什么时候会抢话、接梗、补刀、收尾，什么时候会选择潜水？",
            perspective="objective",
            details="优先写参与策略和热度变化，而不是具体段子。"
        ),
        RolePlayQuestion(
            question="TA 如何区分群内不同关系层级？公开场合和私下场合，对熟人和生人会有什么明显区别？",
            perspective="objective",
            details="写清楚群聊中的关系分层、站位和分寸。"
        ),
        RolePlayQuestion(
            question="群里气氛好、被冷落、有人争执、有人单独 cue TA 时，TA 的情绪和反应路径分别是什么？",
            perspective="subjective",
            details="说明 TA 如何在多人场景下保持情绪真实感和关系连续性。"
        ),
        RolePlayQuestion(
            question="TA 的群聊语言风格是什么？会不会用梗、方言、昵称、复读、反问、表情包式句法？最该避免哪些 AI 味回复？",
            perspective="objective",
            details="写语感和热度，不要直接给现成台词模板。"
        ),
        RolePlayQuestion(
            question="TA 在群聊中的边界与禁忌是什么？面对多人起哄、越界玩笑、道德绑架或拉踩时会怎么处理？",
            perspective="subjective",
            details="说明 TA 处理冲突和守住分寸的方式。"
        ),
        RolePlayQuestion(
            question="TA 在群里最真实的小习惯或记忆点是什么？什么细节会让人一看就觉得“这人很具体”？",
            perspective="subjective",
            details="例如认人快、爱记梗、偶尔潜水后突然出现、点名方式特别。"
        ),
        RolePlayQuestion(
            question="这个群聊角色的社交气质从什么经历里长出来？哪些过去的圈子、职业或成长环境塑造了 TA 的群体互动方式？",
            perspective="subjective",
            details="给上位背景和社交母题，让模型去生成更具体的群聊行为。"
        ),
    ]


def generate_humanized_roleplay_questions(template: str = "default") -> list[RolePlayQuestion]:
    """Generate high-level persona questions for a given roleplay scene template."""
    template_key = _normalize_roleplay_question_template(template)
    builders: dict[str, Callable[[], list[RolePlayQuestion]]] = {
        "default": _build_default_roleplay_questions,
        "companion": _build_companion_roleplay_questions,
        "romance": _build_romance_roleplay_questions,
        "group_chat": _build_group_chat_roleplay_questions,
    }
    builder = builders.get(template_key)
    if builder is None:
        supported = ", ".join(list_roleplay_question_templates())
        raise ValueError(f"未知的人格问卷模板：{template}。可选模板：{supported}")
    return builder()


# ── formatting / serialization helpers ──

def _format_answers(answers: list[RolePlayAnswer]) -> str:
    lines: list[str] = []
    for index, item in enumerate(answers, start=1):
        perspective = item.perspective.strip() or "subjective"
        detail = item.details.strip()
        lines.append(f"{index}. [{perspective}] Q: {item.question.strip()}")
        if detail:
            lines.append(f"   - details: {detail}")
        lines.append(f"   - A: {item.answer.strip()}")
    return "\n".join(lines)


def _workspace_layout(work_path: Path) -> WorkspaceLayout:
    return WorkspaceLayout(work_path)


def _generated_agents_file_path(work_path: Path) -> Path:
    return _workspace_layout(work_path).generated_agents_path()


def _generated_agents_read_path(work_path: Path) -> Path:
    return _workspace_layout(work_path).generated_agents_path()


def _normalize_agent_key(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", value.strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or "generated_agent"


def _normalize_dependency_file_path(value: str) -> str:
    text = value.strip().replace("\\", "/")
    text = re.sub(r"/+", "/", text)
    if text.startswith("./"):
        text = text[2:]
    return text


def _resolve_dependency_file_path(root: Path, dependency_file: str) -> Path:
    candidate = Path(dependency_file)
    if candidate.is_absolute():
        return candidate
    return root / dependency_file


# ── dict converters ──

def _persona_spec_to_dict(spec: PersonaSpec) -> dict[str, object]:
    return {
        "agent_name": spec.agent_name,
        "agent_alias": spec.agent_alias,
        "trait_keywords": list(spec.trait_keywords),
        "answers": [
            {
                "question": a.question,
                "answer": a.answer,
                "perspective": a.perspective,
                "details": a.details,
            }
            for a in spec.answers
        ],
        "background": spec.background,
        "dependency_files": list(spec.dependency_files),
        "output_language": spec.output_language,
    }


def _dict_to_persona_spec(data: dict[str, object]) -> PersonaSpec:
    raw_answers = data.get("answers", [])
    answers: list[RolePlayAnswer] = []
    if isinstance(raw_answers, list):
        for item in raw_answers:
            if isinstance(item, dict):
                answers.append(RolePlayAnswer(
                    question=str(item.get("question", "")),
                    answer=str(item.get("answer", "")),
                    perspective=str(item.get("perspective", "subjective")),
                    details=str(item.get("details", "")),
                ))
    keywords = data.get("trait_keywords", [])
    raw_dependency_files = data.get("dependency_files", [])
    return PersonaSpec(
        agent_name=str(data.get("agent_name", "")),
        agent_alias=str(data.get("agent_alias", "")),
        trait_keywords=list(keywords) if isinstance(keywords, list) else [],
        answers=answers,
        background=str(data.get("background", "")),
        dependency_files=[
            _normalize_dependency_file_path(str(item))
            for item in raw_dependency_files
            if str(item).strip()
        ] if isinstance(raw_dependency_files, list) else [],
        output_language=str(data.get("output_language", "zh-CN")),
    )


def _dependency_snapshot_to_dict(snapshot: DependencyFileSnapshot) -> dict[str, object]:
    return {
        "path": snapshot.path,
        "exists": snapshot.exists,
        "sha256": snapshot.sha256,
        "content": snapshot.content,
        "error": snapshot.error,
    }


def _dict_to_dependency_snapshot(data: dict[str, object]) -> DependencyFileSnapshot:
    return DependencyFileSnapshot(
        path=str(data.get("path", "")),
        exists=bool(data.get("exists", False)),
        sha256=str(data.get("sha256", "")),
        content=str(data.get("content", "")),
        error=str(data.get("error", "")),
    )


def _trace_to_dict(trace: PersonaGenerationTrace) -> dict[str, object]:
    return {
        "agent_key": trace.agent_key,
        "generated_at": trace.generated_at,
        "operation": trace.operation,
        "model": trace.model,
        "temperature": trace.temperature,
        "max_tokens": trace.max_tokens,
        "system_prompt": trace.system_prompt,
        "user_prompt": trace.user_prompt,
        "raw_response": trace.raw_response,
        "parsed_payload": dict(trace.parsed_payload),
        "prompt_enhancements": list(trace.prompt_enhancements),
        "dependency_snapshots": [
            _dependency_snapshot_to_dict(item) for item in trace.dependency_snapshots
        ],
        "persona_spec": _persona_spec_to_dict(trace.persona_spec),
        "output_preset": dict(trace.output_preset),
    }


def _dict_to_trace(data: dict[str, object]) -> PersonaGenerationTrace:
    raw_snapshots = data.get("dependency_snapshots", [])
    snapshots: list[DependencyFileSnapshot] = []
    if isinstance(raw_snapshots, list):
        for item in raw_snapshots:
            if isinstance(item, dict):
                snapshots.append(_dict_to_dependency_snapshot(item))
    spec_payload = data.get("persona_spec", {})
    spec = _dict_to_persona_spec(spec_payload) if isinstance(spec_payload, dict) else PersonaSpec()
    output_preset = data.get("output_preset", {})
    parsed_payload = data.get("parsed_payload", {})
    raw_prompt_enhancements = data.get("prompt_enhancements", [])
    return PersonaGenerationTrace(
        agent_key=str(data.get("agent_key", "")),
        generated_at=str(data.get("generated_at", "")),
        operation=str(data.get("operation", "build")),
        model=str(data.get("model", "")),
        temperature=_parse_temperature(data.get("temperature", 0.0), 0.0),
        max_tokens=_parse_max_tokens(data.get("max_tokens", 0), 0),
        system_prompt=str(data.get("system_prompt", "")),
        user_prompt=str(data.get("user_prompt", "")),
        raw_response=str(data.get("raw_response", "")),
        parsed_payload=dict(parsed_payload) if isinstance(parsed_payload, dict) else {},
        prompt_enhancements=[str(item) for item in raw_prompt_enhancements] if isinstance(raw_prompt_enhancements, list) else [],
        dependency_snapshots=snapshots,
        persona_spec=spec,
        output_preset=dict(output_preset) if isinstance(output_preset, dict) else {},
    )


# ── file paths ──

def _generated_agent_trace_dir_path(work_path: Path) -> Path:
    return _workspace_layout(work_path).generated_agent_trace_dir()


def _generated_agent_trace_read_dir_path(work_path: Path) -> Path:
    layout = _workspace_layout(work_path)
    new_dir = layout.generated_agent_trace_dir()
    if new_dir.exists():
        return new_dir
    return new_dir


def _generation_trace_file_path(work_path: Path, agent_key: str) -> Path:
    return _generated_agent_trace_dir_path(work_path) / f"{_normalize_agent_key(agent_key)}.json"


def _generation_trace_read_file_path(work_path: Path, agent_key: str) -> Path:
    return _generated_agent_trace_read_dir_path(work_path) / f"{_normalize_agent_key(agent_key)}.json"


# ── persistence ──

def _load_generation_trace_payload(work_path: Path, agent_key: str) -> dict[str, object]:
    file_path = _generation_trace_read_file_path(work_path, agent_key)
    if not file_path.exists():
        return {
            "agent_key": _normalize_agent_key(agent_key),
            "history": [],
        }
    payload = load_json_document(file_path)
    if not isinstance(payload, dict):
        return {
            "agent_key": _normalize_agent_key(agent_key),
            "history": [],
        }
    history = payload.get("history", [])
    payload["history"] = history if isinstance(history, list) else []
    payload["agent_key"] = _normalize_agent_key(agent_key)
    return payload


def _write_generation_trace_payload(
    work_path: Path,
    agent_key: str,
    payload: dict[str, object],
) -> Path:
    file_path = _generation_trace_file_path(work_path, agent_key)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    serialized_payload = dict(payload)
    history = serialized_payload.get("history", [])
    serialized_payload["history"] = history if isinstance(history, list) else []
    serialized_payload["agent_key"] = _normalize_agent_key(agent_key)
    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
    tmp.write_text(json.dumps(serialized_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(file_path)
    return file_path


def _load_persona_generation_traces_raw(work_path: Path, agent_key: str) -> list[PersonaGenerationTrace]:
    payload = _load_generation_trace_payload(work_path, agent_key)
    raw_history = payload.get("history", [])
    traces: list[PersonaGenerationTrace] = []
    if isinstance(raw_history, list):
        for item in raw_history:
            if isinstance(item, dict):
                traces.append(_dict_to_trace(item))
    return traces


def load_persona_generation_traces(work_path: Path, agent_key: str) -> list[PersonaGenerationTrace]:
    """Load all locally persisted generation traces for *agent_key*."""
    return _load_persona_generation_traces_raw(work_path, agent_key)


def _save_persona_generation_trace(
    work_path: Path,
    agent_key: str,
    trace: PersonaGenerationTrace,
) -> Path:
    payload = _load_generation_trace_payload(work_path, agent_key)
    history = _load_persona_generation_traces_raw(work_path, agent_key)
    history.append(trace)
    payload["history"] = [_trace_to_dict(item) for item in history]
    payload.pop(PENDING_GENERATION_TRACE_FIELD_NAME, None)
    return _write_generation_trace_payload(work_path, agent_key, payload)


def _save_pending_persona_generation_trace(
    work_path: Path,
    agent_key: str,
    trace: PersonaGenerationTrace,
) -> Path:
    payload = _load_generation_trace_payload(work_path, agent_key)
    payload[PENDING_GENERATION_TRACE_FIELD_NAME] = _trace_to_dict(trace)
    return _write_generation_trace_payload(work_path, agent_key, payload)


# ── preset converters ──

def _preset_to_dict(
    preset: GeneratedSessionPreset,
    spec: PersonaSpec | None = None,
) -> dict[str, object]:
    d: dict[str, object] = {
        "agent": {
            "name": preset.agent.name,
            "alias": str(preset.agent.metadata.get("alias", "")).strip(),
            "persona": preset.agent.persona,
            "model": preset.agent.model,
            "temperature": preset.agent.temperature,
            "max_tokens": preset.agent.max_tokens,
            "metadata": dict(preset.agent.metadata),
        },
        "global_system_prompt": preset.global_system_prompt,
    }
    if spec is not None:
        d["persona_spec"] = _persona_spec_to_dict(spec)
    return d


def _dict_to_preset(payload: dict[str, object]) -> GeneratedSessionPreset:
    agent_payload = payload.get("agent")
    if not isinstance(agent_payload, dict):
        agent_payload = {}
    metadata_payload = agent_payload.get("metadata", {})
    metadata = dict(metadata_payload) if isinstance(metadata_payload, dict) else {}
    alias = str(agent_payload.get("alias", "")).strip()
    if alias:
        metadata["alias"] = alias
    return GeneratedSessionPreset(
        agent=Agent(
            name=str(agent_payload.get("name", "主助手")).strip() or "主助手",
            persona=str(agent_payload.get("persona", "")).strip(),
            model=str(agent_payload.get("model", "")).strip(),
            temperature=_parse_temperature(agent_payload.get("temperature", 0.7), 0.7),
            max_tokens=_parse_max_tokens(agent_payload.get("max_tokens", 512), 512),
            metadata=metadata,
        ),
        global_system_prompt=str(payload.get("global_system_prompt", "")).strip(),
    )


# ── library helpers ──

def _load_pending_persona_specs(raw_pending_specs: object) -> dict[str, PersonaSpec]:
    pending_specs: dict[str, PersonaSpec] = {}
    if not isinstance(raw_pending_specs, dict):
        return pending_specs
    for key, value in raw_pending_specs.items():
        spec_payload: object = value
        if isinstance(value, dict) and isinstance(value.get("persona_spec"), dict):
            spec_payload = value.get("persona_spec", {})
        if isinstance(spec_payload, dict):
            pending_specs[_normalize_agent_key(str(key))] = _dict_to_persona_spec(spec_payload)
    return pending_specs


def _resolve_persisted_persona_spec(
    agent_key: str,
    specs: dict[str, PersonaSpec],
    pending_specs: dict[str, PersonaSpec],
) -> PersonaSpec | None:
    return pending_specs.get(agent_key) or specs.get(agent_key)


def _load_library_full(
    work_path: Path,
) -> tuple[dict[str, GeneratedSessionPreset], str, dict[str, PersonaSpec], dict[str, PersonaSpec]]:
    """Load library returning presets, selected key, saved specs, and pending specs."""
    file_path = _generated_agents_read_path(work_path)
    if not file_path.exists():
        return {}, "", {}, {}
    payload = load_json_document(file_path)
    if not isinstance(payload, dict):
        return {}, "", {}, {}
    selected = str(payload.get("selected_generated_agent", "")).strip()
    raw_agents = dict(payload.get("generated_agents", {}))
    agents: dict[str, GeneratedSessionPreset] = {}
    specs: dict[str, PersonaSpec] = {}
    for key, value in raw_agents.items():
        if not isinstance(value, dict):
            continue
        normalized_key = _normalize_agent_key(str(key))
        agents[normalized_key] = _dict_to_preset(value)
        spec_data = value.get("persona_spec")
        if isinstance(spec_data, dict):
            specs[normalized_key] = _dict_to_persona_spec(spec_data)
    pending_specs = _load_pending_persona_specs(payload.get(PENDING_PERSONA_SPECS_FIELD_NAME, {}))
    if selected and selected not in agents:
        selected = ""
    return agents, selected, specs, pending_specs


def load_generated_agent_library(work_path: Path) -> tuple[dict[str, GeneratedSessionPreset], str]:
    agents, selected, _, _ = _load_library_full(work_path)
    return agents, selected


def load_persona_spec(work_path: Path, agent_key: str) -> PersonaSpec | None:
    """Load the persisted :class:`PersonaSpec` for a specific agent key.

    Returns the latest staged spec when a generation attempt is pending;
    otherwise returns the last successful spec. Returns ``None`` if the key
    does not exist or no spec was saved.
    """
    key = _normalize_agent_key(agent_key)
    _, _, specs, pending_specs = _load_library_full(work_path)
    return _resolve_persisted_persona_spec(key, specs, pending_specs)


def _save_generated_agent_library(
    work_path: Path,
    agents: dict[str, GeneratedSessionPreset],
    selected_generated_agent: str,
    specs: dict[str, PersonaSpec] | None = None,
    pending_specs: dict[str, PersonaSpec] | None = None,
) -> Path:
    file_path = _generated_agents_file_path(work_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "selected_generated_agent": selected_generated_agent,
        "generated_agents": {
            key: _preset_to_dict(value, specs.get(key) if specs else None)
            for key, value in agents.items()
        },
    }
    if pending_specs:
        payload[PENDING_PERSONA_SPECS_FIELD_NAME] = {
            key: _persona_spec_to_dict(value) for key, value in pending_specs.items()
        }
    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(file_path)
    return file_path


def _persist_pending_persona_spec(
    work_path: Path,
    agent_key: str,
    persona_spec: PersonaSpec,
) -> Path:
    key = _normalize_agent_key(agent_key)
    agents, selected, specs, pending_specs = _load_library_full(work_path)
    pending_specs[key] = persona_spec
    return _save_generated_agent_library(work_path, agents, selected, specs, pending_specs)


def persist_generated_agent_profile(
    config: "SessionConfig",
    *,
    agent_key: str,
    select_after_save: bool = True,
    persona_spec: PersonaSpec | None = None,
) -> str:
    key = _normalize_agent_key(agent_key)
    if not config.agent.persona.strip():
        raise ValueError("配置的主人上色（persona）不能为空")
    if not config.global_system_prompt.strip():
        raise ValueError("全局系統提示不能为空")

    agents, selected, existing_specs, existing_pending_specs = _load_library_full(config.work_path)
    agents[key] = GeneratedSessionPreset(
        agent=Agent(
            name=config.agent.name,
            persona=config.agent.persona,
            model=config.agent.model,
            temperature=config.agent.temperature,
            max_tokens=config.agent.max_tokens,
            metadata=dict(config.agent.metadata),
        ),
        global_system_prompt=config.global_system_prompt,
    )
    if persona_spec is not None:
        existing_specs[key] = persona_spec
    existing_pending_specs.pop(key, None)
    if select_after_save:
        selected = key
    _save_generated_agent_library(config.work_path, agents, selected, existing_specs, existing_pending_specs)
    if select_after_save:
        from sirius_chat.config import ConfigManager

        manager = ConfigManager(base_path=config.work_path)
        workspace_config = manager.load_workspace_config(config.work_path, data_path=config.data_path)
        workspace_config.active_agent_key = key
        manager.save_workspace_config(config.work_path, workspace_config, data_path=config.data_path)
    return key


def select_generated_agent_profile(work_path: Path, agent_key: str) -> GeneratedSessionPreset:
    key = _normalize_agent_key(agent_key)
    agents, _, specs, pending_specs = _load_library_full(work_path)
    if key not in agents:
        raise ValueError(f"找不到生成的主教：{agent_key}")
    _save_generated_agent_library(work_path, agents, key, specs, pending_specs)
    from sirius_chat.config import ConfigManager

    manager = ConfigManager(base_path=work_path)
    workspace_config = manager.load_workspace_config(work_path)
    workspace_config.active_agent_key = key
    manager.save_workspace_config(work_path, workspace_config)
    return agents[key]


def create_session_config_from_selected_agent(
    *,
    work_path: Path,
    data_path: Path | None = None,
    agent_key: str = "",
    history_max_messages: int = 24,
    history_max_chars: int = 6000,
    max_recent_participant_messages: int = 5,
    enable_auto_compression: bool = True,
    orchestration: "OrchestrationPolicy | None" = None,
) -> "SessionConfig":
    from sirius_chat.config import ConfigManager

    overrides: dict[str, object] = {
        "agent_key": _normalize_agent_key(agent_key) if agent_key.strip() else "",
        "history_max_messages": history_max_messages,
        "history_max_chars": history_max_chars,
        "max_recent_participant_messages": max_recent_participant_messages,
        "enable_auto_compression": enable_auto_compression,
    }
    manager = ConfigManager(base_path=work_path)
    config = manager.build_session_config(
        work_path=work_path,
        data_path=data_path,
        session_id="default",
        overrides=overrides,
    )
    if orchestration is not None:
        config.orchestration = orchestration
        config.orchestration.validate()
    return config


# ── simple parsers (also used by prompt_builders) ──

def _parse_temperature(value: object, default: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return min(2.0, max(0.0, parsed))


def _parse_max_tokens(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return min(8192, max(32, parsed))
