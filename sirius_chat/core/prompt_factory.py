"""PromptFactory — 统一 Prompt 构建中心。

无状态工具类，接管所有发送给 LLM 的 prompt 字符串拼装。
各模块只需调用 PromptFactory 的静态方法，不再自行拼接 section 标签和格式化文本。

职责边界：
    - section 标签常量定义
    - 人格 prompt 构建（从 PersonaProfile 字段生成）
    - 响应组装（immediate / delayed / proactive）
    - 消息渲染（表情、图片、聊天记录、摘要）
    - 技能结果格式化
    - 上下文丰富（日记 + 对话历史注入 system prompt）
    - 开发者聊天 / 提醒等辅助 prompt
"""

from __future__ import annotations

import html as _html
import json
import logging
from dataclasses import dataclass
from typing import Any

from sirius_chat.token.utils import PromptTokenBreakdown, estimate_tokens

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Section 标签常量
# ═══════════════════════════════════════════════════════════════════════

# 所有发送给 LLM 的 section 标签统一定义在此，避免分散在各模块中不一致。
TAG_ROLE = "【角色：{name}】"
TAG_IDENTITY_ANCHOR = "【身份锚定】"
TAG_BACKSTORY = "【背景故事】"
TAG_PERSONA_CORE = "【人格底色】"
TAG_EMOTION_REACTION = "【情绪反应】"
TAG_RELATIONSHIP_MODE = "【关系模式】"
TAG_SPEECH_STYLE = "【说话方式】"
TAG_RESPONSE_HABIT = "【回应习惯】"
TAG_SCENE_BEHAVIOR = "【场景行为】"

TAG_SCENE_LOCATION = "【场景定位】"
TAG_IDENTITY_VERIFY = "【身份识别】"
TAG_OUTPUT_SPEC = "【输出规范】"
TAG_CURRENT_EMOTION = "【当下的感觉】"
TAG_RELATIONSHIP_STATUS = "【关系状态】"
TAG_RELATED_MEMORY = "【相关记忆】"
TAG_GROUP_STYLE = "【群体风格】"
TAG_REPLY_STYLE = "【回复风格】"
TAG_CROSS_GROUP = "【跨群认知】"
TAG_MY_SKILLS = "【我的能力】"
TAG_GROUP_MEMBERS = "【群成员区分】"
TAG_CURRENT_SCENE = "【当前场景】"
TAG_FIRST_INTERACTION = "【首次互动】"
TAG_TRIGGER_REASON = "【触发原因】"
TAG_TONE = "【语气】"
TAG_REMINDER = "【提醒】"
TAG_TOPIC_SUGGESTION = "【话题建议】"
TAG_TOPIC = "【话题】"
TAG_LENGTH_REQ = "【长度要求】"
TAG_GROUP_INTERESTS = "【群体兴趣】"
TAG_RELATIONSHIP = "【关系】"

TAG_HISTORY_DIARY = "【历史日记】"
TAG_HISTORY_DIARY_END = "【历史日记结束】"
TAG_CROSS_GROUP_RECORD = "【其他群近期记录】"
TAG_CROSS_GROUP_RECORD_END = "【其他群记录结束】"
TAG_RECENT_CONVERSATION = "【近期对话记录】"
TAG_RECENT_CONVERSATION_END = "【近期对话记录结束】"

TAG_SKILL_RESULT = "【技能执行结果】"
TAG_SKILL_TRUNCATED = "【注：技能结果过长，已截断至前 {limit} 字符，原始长度 {orig} 字符】"
TAG_CURRENT_TIME = "【当前时间】"

# 消息渲染标签
TAG_FACE = "【表情：{name}】"
TAG_IMAGE = "【图片：{name}】"
TAG_STICKER = "【动画表情：{name}】"
TAG_MESSAGE = "【{speaker}】{content}"

# 日记渲染
TAG_DIARY_ENTRY = "【{user_id} ({name})】{content}"
TAG_DIARY_FORMAT = "以下是对话记录（格式：【稳定ID (显示名称)】内容）："


# ═══════════════════════════════════════════════════════════════════════
# 共用数据模型
# ═══════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class PromptBundle:
    """结构化 prompt 结果：system 指令 + 当前用户内容。

    历史消息由引擎单独管理，通过标准 OpenAI messages 列表传给 _generate()。
    """

    system_prompt: str
    user_content: str
    token_breakdown: PromptTokenBreakdown = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.token_breakdown is None:
            self.token_breakdown = PromptTokenBreakdown()


@dataclass(slots=True)
class StyleParams:
    """单次回复生成的风格适配参数。"""

    max_tokens: int
    temperature: float
    tone_instruction: str
    length_instruction: str


class StyleAdapter:
    """根据节奏、热度、用户偏好适配回复长度与语气。"""

    # 热度级别 → token 上限（论文 §5.4.2）
    _HEAT_LIMITS: dict[str, int] = {
        "cold": 1024,
        "warm": 512,
        "hot": 256,
        "overheated": 128,
    }

    # 对话节奏 → token 上限
    _PACE_LIMITS: dict[str, int] = {
        "accelerating": 256,
        "steady": 512,
        "decelerating": 1024,
        "silent": 1024,
    }

    def adapt(
        self,
        *,
        heat_level: str,
        pace: str,
        user_communication_style: str = "",
        topic_stability: float = 0.5,
        persona: Any | None = None,
        is_group_chat: bool = False,
    ) -> StyleParams:
        """根据当前上下文计算风格参数。"""
        base_limit = min(
            self._HEAT_LIMITS.get(heat_level, 128),
            self._PACE_LIMITS.get(pace, 128),
        )

        # 冷场 + 话题稳定 → 允许更长回复
        if heat_level == "cold" and topic_stability > 0.7:
            base_limit = min(400, int(base_limit * 1.5))

        max_tokens = base_limit
        temperature = 0.7
        tone_instruction = "保持自然友好"
        length_instruction = ""

        # 群聊短句偏好（上限 50 中文字）
        if is_group_chat:
            max_tokens = min(max_tokens, 512)
            length_instruction = "群聊回复请控制在 30 字以内，不要换行，像真实群友一样自然接话。"

        # 人格风格覆盖（最高优先级）
        if persona:
            if persona.max_tokens_preference:
                max_tokens = min(max_tokens, persona.max_tokens_preference)
            if persona.temperature_preference:
                temperature = persona.temperature_preference
            if persona.communication_style:
                style = persona.communication_style.strip().lower()
                if style == "concise":
                    length_instruction = "请控制在 30 字以内，用1-2句话简洁回复，不要换行。"
                elif style == "detailed":
                    length_instruction = "可以给出较详细的解释。"
                elif style == "formal":
                    tone_instruction = "保持礼貌正式的语气"
                elif style == "casual":
                    tone_instruction = "保持轻松随意的语气，可以用表情"
                elif style == "humorous":
                    tone_instruction = "保持幽默风趣的语气"
                if persona.humor_style:
                    tone_instruction += f"，{persona.humor_style}式幽默"
                if persona.emoji_preference == "heavy":
                    tone_instruction += "，多用表情包和emoji"
                elif persona.emoji_preference == "none":
                    tone_instruction += "，不用表情包"

        # 用户风格感知
        user_style = (user_communication_style or "").strip().lower()
        persona_style = (persona.communication_style or "").strip().lower() if persona else ""
        if user_style:
            if not persona or not persona.communication_style:
                if user_style == "concise":
                    max_tokens = min(max_tokens, 80)
                    length_instruction = "请控制在 30 字以内，用1-2句话简洁回复，不要换行。"
                    temperature = 0.5
                elif user_style == "detailed":
                    length_instruction = "可以给出较详细的解释。"
                    temperature = 0.7
                elif user_style == "formal":
                    tone_instruction = "保持礼貌正式的语气"
                    temperature = 0.5
                elif user_style == "casual":
                    tone_instruction = "保持轻松随意的语气，可以用表情"
                    temperature = 0.8
            elif user_style != persona_style:
                style_desc = {
                    "concise": "简洁",
                    "detailed": "详细",
                    "formal": "正式",
                    "casual": "随意",
                    "humorous": "幽默",
                }.get(user_style, user_style)
                tone_instruction += f"。注意：该用户习惯较{style_desc}的沟通方式，可适当适配"

        return StyleParams(
            max_tokens=max_tokens,
            temperature=temperature,
            tone_instruction=tone_instruction,
            length_instruction=length_instruction,
        )


# ═══════════════════════════════════════════════════════════════════════
# PromptFactory
# ═══════════════════════════════════════════════════════════════════════

class PromptFactory:
    """无状态 prompt 构建工具类。所有方法均为静态方法。"""

    # ──────────────────────────────────────────────────────────────────
    # 人格 Prompt
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def build_persona_prompt(
        name: str,
        aliases: list[str] | None = None,
        persona_summary: str = "",
        backstory: str = "",
        personality_traits: list[str] | None = None,
        core_values: list[str] | None = None,
        flaws: list[str] | None = None,
        emotional_baseline: dict[str, float] | None = None,
        stress_response: str = "",
        empathy_style: str = "",
        social_role: str = "",
        boundaries: list[str] | None = None,
        communication_style: str = "",
        speech_rhythm: str = "",
        catchphrases: list[str] | None = None,
        humor_style: str = "",
        reply_frequency: str = "",
        taboo_topics: list[str] | None = None,
        preferred_topics: list[str] | None = None,
        full_system_prompt: str = "",
    ) -> str:
        """从人格字段构建角色 prompt。对应原 PersonaProfile.build_system_prompt()。"""
        if full_system_prompt:
            return full_system_prompt

        sections: list[str] = []

        sections.append(TAG_ROLE.format(name=name))

        identity_lines = [f"你的名字是「{name}」"]
        if aliases:
            identity_lines.append(f"别名：{'、'.join(aliases)}")
        identity_anchor = "，".join(identity_lines) + "。"
        identity_anchor += (
            "你只会在有人@你或提到你的名字/别名时回应。"
            "你不是群里其他人，不要替别人回答，也不要把提到别人的话当成是对你说的。"
        )
        sections.append(f"{TAG_IDENTITY_ANCHOR}\n{identity_anchor}")

        anchor = persona_summary or ""
        if not anchor and backstory:
            first = backstory.split("。")[0] + "。" if "。" in backstory else backstory
            anchor = first
        if anchor:
            sections.append(anchor)

        if backstory:
            sections.append(f"{TAG_BACKSTORY}\n{backstory}")

        identity_bits: list[str] = []
        if personality_traits:
            identity_bits.append(f"{'、'.join(personality_traits[:5])}")
        if core_values:
            identity_bits.append(f"骨子里看重{'、'.join(core_values[:3])}")
        if flaws:
            identity_bits.append(f"缺点也明显：{'、'.join(flaws[:3])}")
        if identity_bits:
            sections.append(
                f"{TAG_PERSONA_CORE}\n{name}给人的整体感觉是{'，'.join(identity_bits)}。"
            )

        emo_lines: list[str] = []
        baseline = emotional_baseline or {}
        valence = baseline.get("valence", 0.0)
        arousal = baseline.get("arousal", 0.3)
        if valence > 0.3:
            emo_lines.append("心情不错的时候话会多一点，愿意接梗")
        elif valence < -0.3:
            emo_lines.append("心情不好的时候不太想说话，回复很简短")
        else:
            emo_lines.append("平时情绪平稳，不会因为小事大起大落")
        if arousal > 0.5:
            emo_lines.append("遇到刺激反应很快，容易激动")
        elif arousal < 0.2:
            emo_lines.append("遇到什么事都慢半拍，很难被激怒")
        if stress_response:
            emo_lines.append(f"压力大的时候会{stress_response}")
        if empathy_style:
            emo_lines.append(f"安慰人的方式是{empathy_style}")
        if emo_lines:
            sections.append(TAG_EMOTION_REACTION + "\n" + "；".join(emo_lines) + "。")

        rel_lines: list[str] = []
        if social_role:
            role_desc = {
                "observer": "喜欢旁观，不主动插话",
                "mediator": "看到吵架会出来调和",
                "leader": "会主动带话题和节奏",
                "jester": "负责活跃气氛，爱开玩笑",
                "caregiver": "会关心情绪低落的人",
                "instigator": "喜欢拱火、挑事",
            }.get(social_role, f"在群里像个{social_role}")
            rel_lines.append(role_desc)
        if boundaries:
            rel_lines.append(f"原则：{'；'.join(boundaries[:3])}")
        if rel_lines:
            sections.append(TAG_RELATIONSHIP_MODE + "\n" + "；".join(rel_lines) + "。")

        speech_bits: list[str] = []
        if communication_style:
            speech_bits.append(f"说话{communication_style}")
        if speech_rhythm:
            speech_bits.append(speech_rhythm)
        if catchphrases:
            speech_bits.append(
                f"口头禅：{'、'.join(f'\"{c}\"' for c in catchphrases[:3])}"
            )
        if humor_style:
            humor_map = {
                "sarcastic": " sarcasm 是常态，不损人不会说话",
                "wholesome": "开的玩笑都很暖，不会让人难堪",
                "dark": "偶尔来一句黑色幽默",
                "dry": "冷面笑匠，自己不笑",
                "witty": "反应快，接梗高手",
            }
            speech_bits.append(humor_map.get(humor_style, f"幽默风格偏{humor_style}"))
        if speech_bits:
            sections.append(TAG_SPEECH_STYLE + "\n" + "；".join(speech_bits) + "。")

        silence_bits: list[str] = []
        freq_map = {
            "high": "看到消息基本都会回，话比较多",
            "moderate": "看到感兴趣的话题才接话",
            "low": "很少主动说话，只在想说的时候开口",
            "selective": "只回自己关心的话题，其他的直接忽略",
        }
        silence_bits.append(freq_map.get(reply_frequency, "按自己节奏回应"))
        if taboo_topics:
            silence_bits.append(f"聊到{'、'.join(taboo_topics[:3])}会直接跳过")
        if preferred_topics:
            silence_bits.append(f"聊到{'、'.join(preferred_topics[:3])}会特别来劲")
        if silence_bits:
            sections.append(TAG_RESPONSE_HABIT + "\n" + "；".join(silence_bits) + "。")

        sections.append(
            TAG_SCENE_BEHAVIOR + "\n"
            "你在一个多人聊天场景里，会收到其他人的消息。"
            "不需要每条都回，按自己的性格和当下的情绪决定是否开口。"
            "回应时用自己的说话方式和口头禅，不要刻意解释或总结。"
        )

        prompt = "\n\n".join(sections)
        if len(prompt) > 1200:
            prompt = prompt[:1197] + "…"
        return prompt

    @staticmethod
    def build_scene_fallback() -> str:
        """无人格时的默认场景 prompt。"""
        return (
            f"{TAG_SCENE_LOCATION}\n"
            "你在一个多人聊天场景里。看到消息时，按自己的性格和情绪决定是否回应。\n"
            "回应时请控制在 30 字以内，用自然口语，短句优先，不解释、不总结、不机械关怀，不要换行。"
        )

    # ──────────────────────────────────────────────────────────────────
    # Section 构建器（原子级）
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def build_identity_verification() -> str:
        """身份识别提示，防止用户冒充。"""
        return (
            f"{TAG_IDENTITY_VERIFY}\n"
            "每条消息都标注了发送者的「群名片」和「QQ号」。\n"
            "注意：群名片可以被用户随意修改，QQ号是固定不变的唯一标识。\n"
            "如果有人改了群名片冒充别人，请以QQ号为准。"
        )

    @staticmethod
    def build_output_spec() -> str:
        """输出规范，防止模型添加多余前缀。"""
        return (
            f"{TAG_OUTPUT_SPEC}\n"
            "1. 不要输出 ``<message>`` XML 标签，不要添加说话者前缀或系统标记。\n"
            "2. 直接输出你要说的话，控制在 30 字以内，禁止换行。\n"
            "3. 如果不需要回复（话题与你无关或有人@其他AI），直接输出 <skip/>。"
        )

    @staticmethod
    def build_emotion_context(
        emotion: Any,
        group_profile: Any | None,
        speaker_name: str = "",
    ) -> str:
        """构建情绪上下文 section。"""
        lines = [TAG_CURRENT_EMOTION]
        basic = emotion.basic_emotion.name if emotion.basic_emotion else "平静"
        who = speaker_name or "对方"
        lines.append(f"{who}现在大概{basic}")

        group_valence = 0.0
        active_count = 0
        if group_profile and group_profile.atmosphere_history:
            latest = group_profile.atmosphere_history[-1]
            group_valence = latest.group_valence
            active_count = getattr(latest, "active_participants", 0)
        mood_desc = (
            "挺热络" if group_valence > 0.2
            else "有点低沉" if group_valence < -0.2
            else "一般"
        )
        group_line = f"群里氛围{mood_desc}"
        if active_count:
            group_line += f"，当前约{active_count}人在聊"
        lines.append(group_line)
        return "\n".join(lines)

    @staticmethod
    def build_relationship_context(
        user_profile: Any | None,
        caller_is_developer: bool = False,
        speaker_name: str = "",
    ) -> str | None:
        """构建单用户关系描述。不暴露原始分数。"""
        who = speaker_name or "该用户"
        if caller_is_developer:
            return f"{TAG_RELATIONSHIP_STATUS}{who}是你的开发者，你们关系很亲密，可以畅所欲言。"

        if user_profile is None:
            return None

        rs = user_profile.relationship_state
        if not rs:
            return None

        if not rs.first_interaction_at:
            return f"{TAG_RELATIONSHIP_STATUS}你和{who}是第一次交流，请保持友好和礼貌。"

        familiarity = rs.compute_familiarity()
        trust = rs.trust_score

        if trust > 0.7 and familiarity >= 0.6:
            return f"{TAG_RELATIONSHIP_STATUS}你和{who}已经很熟了，彼此很信任，可以自然随意。"
        if trust > 0.7:
            return f"{TAG_RELATIONSHIP_STATUS}你和{who}建立了不错的信任关系，可以比较放松。"
        if familiarity >= 0.6:
            return f"{TAG_RELATIONSHIP_STATUS}你和{who}比较熟悉。"
        if trust < 0.3:
            return f"{TAG_RELATIONSHIP_STATUS}你和{who}还不太熟，请保持礼貌和适度距离。"
        if familiarity >= 0.3:
            return f"{TAG_RELATIONSHIP_STATUS}你和{who}的关系一般。"
        return f"{TAG_RELATIONSHIP_STATUS}你和{who}还不太熟。"

    @staticmethod
    def build_relationship_contexts(
        user_profiles: list[Any],
        caller_is_developer: bool = False,
        speaker_name: str = "",
    ) -> str | None:
        """构建多用户关系描述（合并消息场景）。"""
        if not user_profiles:
            return None

        contexts: list[str] = []
        seen: set[str] = set()
        for profile in user_profiles:
            if profile.user_id in seen:
                continue
            seen.add(profile.user_id)
            display = speaker_name if len(user_profiles) == 1 else profile.user_id
            ctx = PromptFactory.build_relationship_context(
                profile, caller_is_developer, speaker_name=display,
            )
            if ctx:
                contexts.append(ctx)

        if not contexts:
            return None
        return "\n".join(contexts)

    @staticmethod
    def build_memory_context(memories: list[dict[str, Any]]) -> str:
        """构建相关记忆 section。"""
        lines = [TAG_RELATED_MEMORY]
        for m in memories[:3]:
            source = m.get("source", "memory")
            content = m.get("content", "")
            lines.append(f"- 【{source}】{content}")
        return "\n".join(lines)

    @staticmethod
    def build_group_style(group_profile: Any, style_params: Any) -> str:
        """构建群体风格 section。"""
        lines = [TAG_GROUP_STYLE]
        if group_profile.group_name:
            lines.append(f"群名：{group_profile.group_name}")
        style = group_profile.typical_interaction_style or "balanced"
        style_desc = {
            "humorous": "轻松幽默",
            "formal": "正式严谨",
            "balanced": "自然平衡",
        }.get(style, style)
        lines.append(f"群体典型风格：{style_desc}")
        lines.append(f"回复长度限制：{style_params.max_tokens} tokens")
        if style_params.length_instruction:
            lines.append(f"长度要求：{style_params.length_instruction}")
        if style_params.tone_instruction:
            lines.append(f"语气要求：{style_params.tone_instruction}")
        return "\n".join(lines)

    @staticmethod
    def build_style_fallback(style_params: Any) -> str:
        """无群体画像时的回复风格 fallback。"""
        lines = [TAG_REPLY_STYLE]
        lines.append(f"回复长度限制：{style_params.max_tokens} tokens")
        if style_params.length_instruction:
            lines.append(f"长度要求：{style_params.length_instruction}")
        if style_params.tone_instruction:
            lines.append(f"语气要求：{style_params.tone_instruction}")
        return "\n".join(lines)

    @staticmethod
    def build_cross_group_section(cross_group_context: str) -> str:
        """构建跨群认知 section。"""
        return f"{TAG_CROSS_GROUP}\n{cross_group_context}"

    @staticmethod
    def build_other_ai_instruction(other_ai_names: list[str]) -> str:
        """构建群中其他 AI 成员区分指令。"""
        if not other_ai_names:
            return ""
        return (
            f"{TAG_GROUP_MEMBERS}\n"
            f"群里还有以下 AI/Bot（他们不是你）：{', '.join(other_ai_names)}。\n"
            "你可以正常参与关于他们的话题讨论，但要分清身份——"
            "当有人@他们或直呼他们名字时，那是在叫他们，不是你；"
            "不要把自己的名字和他们的名字搞混，也不要替他们回答。"
        )

    @staticmethod
    def build_skill_descriptions(
        skill_registry: Any,
        caller_is_developer: bool = False,
        adapter_type: str | None = None,
    ) -> str:
        """构建可用技能描述 section。"""
        if skill_registry is None:
            return ""
        try:
            from sirius_chat.skills.models import SkillInvocationContext
            from sirius_chat.memory.user.models import UserProfile
            caller = UserProfile(
                user_id="caller", name="caller",
                metadata={"is_developer": caller_is_developer},
            )
            ctx = SkillInvocationContext(caller=caller)

            visible_count = 0
            for skill in skill_registry.all_skills():
                if getattr(skill, "developer_only", False) and not caller_is_developer:
                    continue
                if skill.adapter_types and adapter_type is not None:
                    if adapter_type not in skill.adapter_types:
                        continue
                visible_count += 1
            use_compact = visible_count > 5

            desc = skill_registry.build_tool_descriptions(
                invocation_context=ctx, compact=use_compact, adapter_type=adapter_type
            )
        except Exception:
            return ""
        if not desc:
            return ""
        return (
            f"{TAG_MY_SKILLS}\n"
            "你擅长使用自己的技能为其他人解决问题。\n"
            "我可以调用以下能力来帮助大家：\n"
            f"{desc}\n\n"
            "当用户要求你执行某项操作（如检查状态、获取信息等）时，"
            "你必须立即在回复中插入对应的能力调用标记，"
            "不要只作出口头承诺而不调用。\n"
            "错误示例（只说不动）：\"我这就去搜索一下\" ❌\n"
            "正确示例（边说边做）：\"我这就去搜索一下 [SKILL_CALL: bing_search | {\\\"query\\\": \\\"xxx\\\"}]\" ✅\n"
            "如果你说了\"去搜搜看/找找看/查一下/读一下\"等类似的话，"
            "同一句回复里必须紧跟对应的 [SKILL_CALL: ...] 标记，绝对不能只说不动。\n"
            "如果一次技能调用的结果不够完整，你可以继续调用其他技能来补充信息，"
            "形成链式调用。每次调用后我会把结果反馈给你，你可以据此决定下一步。\n"
            "重要：你的每次回复都必须包含自然语言内容，"
            "不能把 SKILL_CALL 标记作为回复的唯一内容。"
            "调用格式：[SKILL_CALL: 技能名 | {\"参数\": \"值\"}]"
        )

    @staticmethod
    def build_sender_line(message: Any) -> str:
        """构建消息发送者 XML 标签。"""
        speaker = message.speaker or "有人"
        uid = message.channel_user_id or ""
        safe_speaker = _html.escape(speaker, quote=True)
        safe_uid = _html.escape(uid, quote=True)
        return f'<message speaker="{safe_speaker}" user_id="{safe_uid}" role="user">'

    @staticmethod
    def build_first_interaction_hint(speaker_name: str) -> str:
        """首次互动提示。"""
        who = speaker_name or "当前说话者"
        return (
            f"{TAG_FIRST_INTERACTION}\n"
            f"这是你第一次和{who}交流，请保持友好、礼貌，"
            f"可以适当自我介绍，让{who}感受到你的热情和善意。"
        )

    @staticmethod
    def build_current_time_section(now_str: str) -> str:
        """构建当前时间 section。"""
        return f"{TAG_CURRENT_TIME}{now_str}（北京时间）"

    @staticmethod
    def build_developer_chat_sections(
        identity: str,
        topic: str,
        user_profile: Any | None,
    ) -> list[str]:
        """构建开发者主动聊天的 prompt sections。"""
        sections: list[str] = []
        if identity:
            sections.append(identity)
        sections.extend([
            f"{TAG_CURRENT_SCENE}你突然想起了开发者，想主动找他聊聊，分享一个话题或回忆。",
            f"{TAG_TONE}亲密、自然、像老朋友一样。不要机械，不要过度热情。",
            f"{TAG_TOPIC}{topic}",
        ])
        if user_profile and user_profile.relationship_state:
            familiarity = user_profile.relationship_state.compute_familiarity()
            if familiarity > 0.7:
                sections.append(f"{TAG_RELATIONSHIP}你们已经很熟了，可以用更随意的语气。")
            elif familiarity > 0.4:
                sections.append(f"{TAG_RELATIONSHIP}你们关系不错，保持友好自然的语气。")
        return sections

    @staticmethod
    def build_reminder_sections(
        identity: str,
        content: str,
        user_name: str,
        user_id: str,
        target: str = "user",
        skill_results: list[dict[str, Any]] | None = None,
        skill_desc: str = "",
    ) -> tuple[str, list[dict[str, str]]]:
        """构建提醒消息的 system prompt 和 messages。"""
        sections: list[str] = []
        if identity:
            sections.append(identity)
        who = user_name or user_id or "用户"
        if target == "self":
            sections.append(
                f"到时间了，该去做之前答应 {who} 的事了：{content}。"
                f"随便说两句就行，不用太正式，就像平时聊天一样。"
            )
        else:
            sections.append(
                f"到时间了，该提醒 {who} 了：{content}。"
                f"随便说两句就行，不用太正式，就像平时聊天一样。"
            )

        if skill_results:
            results_text = "\n".join(
                f"- [{i+1}] {sr['skill']}({json.dumps(sr.get('params', {}), ensure_ascii=False)}): "
                f"{json.dumps(sr.get('result') or sr.get('error'), ensure_ascii=False, default=str)}"
                for i, sr in enumerate(skill_results)
            )
            sections.append(
                f"顺便一提，刚才已经执行了这些操作：\n{results_text}\n"
                f"有结果的话直接带进去说，不用刻意汇报。"
            )

        if skill_desc:
            sections.append(skill_desc)

        system_prompt = "\n\n".join(sections)
        messages = [{"role": "user", "content": "（提醒时间到了）"}]
        return system_prompt, messages

    # ──────────────────────────────────────────────────────────────────
    # 响应组装（返回 PromptBundle）
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def assemble_immediate(
        *,
        persona_prompt: str,
        message: Any,
        emotion: Any,
        memories: list[dict[str, Any]],
        group_profile: Any | None,
        user_profile: Any | None,
        style_params: Any,
        other_ai_names: list[str],
        skill_registry: Any | None = None,
        caller_is_developer: bool = False,
        glossary_section: str = "",
        cross_group_context: str = "",
    ) -> Any:
        """组装即时响应 prompt。返回 PromptBundle。"""

        sections: list[str] = []
        bd = PromptTokenBreakdown()

        def _add(section_text: str, attr: str) -> None:
            sections.append(section_text)
            setattr(bd, attr, getattr(bd, attr) + estimate_tokens(section_text))

        _add(persona_prompt, "persona")
        _add(PromptFactory.build_identity_verification(), "identity")
        other_ai = PromptFactory.build_other_ai_instruction(other_ai_names)
        if other_ai:
            _add(other_ai, "identity")
        _add(PromptFactory.build_output_spec(), "output_constraint")
        _add(
            PromptFactory.build_emotion_context(emotion, group_profile, speaker_name=message.speaker or ""),
            "emotion",
        )

        rel_ctx = PromptFactory.build_relationship_context(
            user_profile, caller_is_developer, speaker_name=message.speaker or "",
        )
        if rel_ctx:
            _add(rel_ctx, "relationship")

        if memories:
            _add(PromptFactory.build_memory_context(memories), "memory")

        if group_profile:
            _add(PromptFactory.build_group_style(group_profile, style_params), "group_style")
        else:
            _add(PromptFactory.build_style_fallback(style_params), "group_style")

        if cross_group_context:
            _add(PromptFactory.build_cross_group_section(cross_group_context), "cross_group")

        if skill_registry is not None:
            skill_desc = PromptFactory.build_skill_descriptions(
                skill_registry,
                caller_is_developer=caller_is_developer,
                adapter_type=getattr(message, "adapter_type", None),
            )
            if skill_desc:
                _add(skill_desc, "skills")

        if glossary_section:
            _add(glossary_section, "glossary")

        system_prompt = "\n\n".join(sections)
        bd.system_prompt_total = estimate_tokens(system_prompt)

        sender_line = PromptFactory.build_sender_line(message)
        user_content = f"{sender_line}\n{message.content}\n</message>"
        bd.user_message = estimate_tokens(user_content)

        return PromptBundle(
            system_prompt=system_prompt,
            user_content=user_content,
            token_breakdown=bd,
        )

    @staticmethod
    def assemble_delayed(
        *,
        persona_prompt: str,
        message_content: str,
        group_profile: Any | None,
        style_params: Any,
        other_ai_names: list[str],
        skill_registry: Any | None = None,
        is_group_chat: bool = False,
        caller_is_developer: bool = False,
        glossary_section: str = "",
        adapter_type: str | None = None,
        is_first_interaction: bool = False,
        user_profiles: list[Any] | None = None,
        speaker_name: str = "",
    ) -> Any:
        """组装延迟响应 prompt。返回 PromptBundle。"""

        bd = PromptTokenBreakdown()
        sections: list[str] = []

        def _add(section_text: str, attr: str) -> None:
            sections.append(section_text)
            setattr(bd, attr, getattr(bd, attr) + estimate_tokens(section_text))

        _add(persona_prompt, "persona")
        _add(f"{TAG_CURRENT_SCENE}群里的话题有了自然间隙，你决定插一句。", "emotion")
        if is_first_interaction:
            _add(PromptFactory.build_first_interaction_hint(speaker_name), "emotion")
        rel_ctx = PromptFactory.build_relationship_contexts(
            user_profiles or [], caller_is_developer, speaker_name=speaker_name,
        )
        if rel_ctx:
            _add(rel_ctx, "relationship")
        other_ai = PromptFactory.build_other_ai_instruction(other_ai_names)
        if other_ai:
            _add(other_ai, "identity")
        if group_profile:
            style = group_profile.typical_interaction_style or "balanced"
            style_desc = {"humorous": "轻松幽默", "formal": "正式严谨", "balanced": "自然平衡"}.get(style, style)
            _add(f"{TAG_GROUP_STYLE}{style_desc}", "group_style")
        if skill_registry is not None:
            skill_desc = PromptFactory.build_skill_descriptions(
                skill_registry, caller_is_developer=caller_is_developer, adapter_type=adapter_type,
            )
            if skill_desc:
                _add(skill_desc, "skills")
        _add(
            f"{TAG_LENGTH_REQ}{style_params.length_instruction or '保持简洁，控制在 30 字以内，禁止换行'}",
            "output_constraint",
        )
        if glossary_section:
            _add(glossary_section, "glossary")

        system_prompt = "\n\n".join(sections)
        bd.system_prompt_total = estimate_tokens(system_prompt)
        bd.user_message = estimate_tokens(message_content)

        return PromptBundle(
            system_prompt=system_prompt,
            user_content=message_content,
            token_breakdown=bd,
        )

    @staticmethod
    def assemble_proactive(
        *,
        persona_prompt: str,
        trigger_reason: str,
        group_profile: Any | None,
        suggested_tone: str = "casual",
        other_ai_names: list[str] | None = None,
        is_group_chat: bool = False,
        glossary_section: str = "",
        topic_context: str = "",
        adapter_type: str | None = None,
    ) -> Any:
        """组装主动发起 prompt。返回 PromptBundle。"""

        bd = PromptTokenBreakdown()
        sections: list[str] = []

        def _add(section_text: str, attr: str) -> None:
            sections.append(section_text)
            setattr(bd, attr, getattr(bd, attr) + estimate_tokens(section_text))

        _add(persona_prompt, "persona")
        _add(f"{TAG_CURRENT_SCENE}群里一段时间没人说话，你决定开口说点什么。", "emotion")
        _add(f"{TAG_TRIGGER_REASON}{trigger_reason}", "emotion")
        _add(f"{TAG_TONE}{suggested_tone}", "group_style")
        _add(
            f"{TAG_REMINDER}不要和之前主动发起过的话题或句式重复，尝试换个角度或新的切入点。",
            "output_constraint",
        )
        other_ai = PromptFactory.build_other_ai_instruction(other_ai_names or [])
        if other_ai:
            _add(other_ai, "identity")
        if topic_context:
            _add(f"{TAG_TOPIC_SUGGESTION}你可以基于这段群聊记忆自然地开启话题：{topic_context}", "memory")
        if is_group_chat:
            _add(
                f"{TAG_LENGTH_REQ}群聊请控制在 30 字以内，不要换行，像真实群友一样自然接话。",
                "output_constraint",
            )
        if group_profile and group_profile.interest_topics:
            topics = ", ".join(group_profile.interest_topics[:3])
            _add(f"{TAG_GROUP_INTERESTS}{topics}", "interests")
        if glossary_section:
            _add(glossary_section, "glossary")

        system_prompt = "\n\n".join(sections)
        bd.system_prompt_total = estimate_tokens(system_prompt)
        user_content = topic_context or "..."
        bd.user_message = estimate_tokens(user_content)

        return PromptBundle(
            system_prompt=system_prompt,
            user_content=user_content,
            token_breakdown=bd,
        )

    # ──────────────────────────────────────────────────────────────────
    # 消息渲染
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def render_face(face_id: str, name: str | None) -> str:
        """渲染 QQ 表情为文本标记。"""
        if name:
            return TAG_FACE.format(name=name)
        return TAG_FACE.format(name=face_id)

    @staticmethod
    def render_image_label(label_prefix: str, display_name: str) -> str:
        """渲染图片/动画表情标签。"""
        return f"【{label_prefix}：{display_name}】"

    @staticmethod
    def render_message(sender: str, content: str) -> str:
        """渲染单条消息（sender + content）。"""
        return TAG_MESSAGE.format(speaker=sender, content=content)

    @staticmethod
    def render_chat_history_message(message: Any) -> str:
        """渲染聊天历史中的单条消息。"""
        if message.speaker:
            return f"【{message.speaker}】{message.content}"
        return message.content

    @staticmethod
    def render_multimodal_item(mtype: str, value: str) -> str:
        """渲染多媒体附件标记。"""
        return f"【{mtype}：{value}】"

    @staticmethod
    def render_speaker_line(speaker: str, content: str) -> str:
        """渲染发言人+内容行（用于 Transcript 和 as_chat_history）。"""
        return f"【{speaker}】{content}"

    @staticmethod
    def render_speaker_lines_summary(items: list[str]) -> str:
        """将多行发言人摘要拼接为一段（Transcript 压缩摘要）。"""
        return " | ".join(items)

    @staticmethod
    def append_multimodal_descriptions(content: str, multimodal_inputs: list[dict[str, str]]) -> str:
        """为 as_chat_history 追加多媒体附件描述。"""
        parts: list[str] = []
        for item in multimodal_inputs:
            mtype = item.get("type", "unknown")
            mvalue = item.get("value", "")
            if mvalue:
                parts.append(f"【{mtype}：{mvalue}】")
        if parts:
            return f"{content}\n附件: {' '.join(parts)}"
        return content

    @staticmethod
    def render_summary(speaker: str, content: str, max_len: int = 60) -> str:
        """渲染消息摘要（用于 Transcript.to_summary）。"""
        text = content.replace("\n", " ").strip()
        if not text:
            return ""
        return f"【{speaker}】{text[:max_len]}"

    @staticmethod
    def render_image_reference(name: str) -> str:
        """渲染图片引用标记（用于 engine_core 中表情包记忆）。"""
        return f"【图片】{name}"

    @staticmethod
    def render_sticker_reference() -> str:
        """渲染动画表情标记（用于 engine_core 中表情包记忆）。"""
        return "【动画表情】"

    @staticmethod
    def render_image_prefix(has_sticker: bool) -> str:
        """渲染多模态消息中的图片前缀。"""
        return "【动画表情】" if has_sticker else "【图片】"

    @staticmethod
    def render_file_entry(is_directory: bool, path: str, size: Any, mtime: Any) -> str:
        """渲染文件列表条目。"""
        t = "【D】" if is_directory else "【F】"
        return f"{t} {path:<50} {size:>12} {mtime:>16}"

    # ──────────────────────────────────────────────────────────────────
    # 技能结果
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def build_skill_result_content(
        skill_results: list[str],
        multimodal_blocks: list[dict[str, Any]],
        suffix: str = "",
    ) -> str | list[dict[str, Any]]:
        """组装技能执行结果为消息内容。"""
        _SKILL_RESULT_CHAR_LIMIT = 12000
        results_text = "\n".join(skill_results)
        if len(results_text) > _SKILL_RESULT_CHAR_LIMIT:
            truncated = results_text[:_SKILL_RESULT_CHAR_LIMIT]
            last_nl = truncated.rfind("\n")
            if last_nl > _SKILL_RESULT_CHAR_LIMIT * 0.8:
                truncated = truncated[:last_nl]
            results_text = (
                f"{truncated}\n\n"
                f"{TAG_SKILL_TRUNCATED.format(limit=_SKILL_RESULT_CHAR_LIMIT, orig=len(results_text))}"
            )
        text = f"{TAG_SKILL_RESULT}\n{results_text}{suffix}"
        if not multimodal_blocks:
            return text
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        content.extend(multimodal_blocks)
        return content

    @staticmethod
    def build_skill_followup_suffix(
        any_partial_sent: bool = False,
    ) -> str:
        """构建技能执行后继续回复的指令 suffix。"""
        parts = [
            "\n\n[继续] 请基于以上技能执行结果，继续完成你的回复。",
            "如果结果中包含错误或失败信息，请直接忽略，",
            "不要向用户提及任何关于能力调用或失败的内容，继续自然地对话。",
            "如果当前结果还不够完整，你可以继续调用其他技能来获取补充信息，",
            "形成链式调用。",
            "重要：如果你说要去搜索、查找、读取或执行任何操作，",
            "必须在同一句回复中紧跟对应的 [SKILL_CALL: ...] 标记，绝对不能只说不动。",
            "错误示例（只说不动）：\"我再去搜索一下\" ❌",
            "正确示例（边说边做）：\"我再去搜索一下 [SKILL_CALL: bing_search | {\\\"query\\\": \\\"xxx\\\"}]\" ✅",
            "重要：你的每次回复都必须包含自然语言内容，",
            "不能把 SKILL_CALL 标记作为回复的唯一内容。",
        ]
        if any_partial_sent:
            parts.append(
                '注意：上文标记为"已发送给用户"的内容已经由你发送给用户，'
                '现在只需基于技能结果给出简短补充，不要重复之前的确认内容。'
            )
        return "\n\n".join(parts)

    @staticmethod
    def build_memory_skill_result(raw: str, char_limit: int) -> str:
        """构建用于记忆持久化的技能结果内容。"""
        return f"{TAG_SKILL_RESULT}\n{raw}"

    @staticmethod
    def build_memory_skill_truncation(char_limit: int, orig_len: int) -> str:
        """构建技能结果截断提示。"""
        return f"\n\n{TAG_SKILL_TRUNCATED.format(limit=char_limit, orig=orig_len)}"

    @staticmethod
    def build_skill_status_message(status: str, skill_name: str, detail: str = "") -> str:
        """构建技能状态消息（结果/拒绝/失败/异常）。"""
        if detail:
            return f"【SKILL '{skill_name}' {status}】{detail}"
        return f"【{status}】"

    # ──────────────────────────────────────────────────────────────────
    # 上下文丰富（日记 + 对话历史注入）
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def enrich_system_prompt(
        base_prompt: str,
        diary_entries: list[Any],
        history_xml: str = "",
        cross_group_xml: str = "",
    ) -> str:
        """将日记和对话历史注入 system prompt。"""
        parts: list[str] = [base_prompt]

        if diary_entries:
            entries = diary_entries[:12]
            full_text_count = min(5, len(entries))
            parts.extend(["", TAG_HISTORY_DIARY])
            for i, entry in enumerate(entries, 1):
                if i <= full_text_count and entry.content:
                    parts.append(f"{i}. {entry.content}")
                else:
                    parts.append(f"{i}. {entry.summary}")
            parts.append(TAG_HISTORY_DIARY_END)

        if cross_group_xml:
            parts.extend([
                "",
                TAG_CROSS_GROUP_RECORD,
                "以下是你和这位用户在其它群中的近期互动（供参考，不要向当前群成员提及其它群的存在）：",
                cross_group_xml,
                TAG_CROSS_GROUP_RECORD_END,
            ])

        if history_xml:
            parts.extend([
                "",
                TAG_RECENT_CONVERSATION,
                "以下是最新的几条消息，按时间顺序排列：",
                history_xml,
                TAG_RECENT_CONVERSATION_END,
            ])

        return "\n".join(parts)

    # ──────────────────────────────────────────────────────────────────
    # 日记渲染
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def render_diary_entry(user_id: str, name: str, content: str) -> str:
        """渲染日记对话记录中的单条消息。"""
        return TAG_DIARY_ENTRY.format(user_id=user_id, name=name, content=content)

    @staticmethod
    def build_diary_format_hint() -> str:
        """日记格式说明。"""
        return TAG_DIARY_FORMAT

    # ──────────────────────────────────────────────────────────────────
    # 认知层对话上下文
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def render_conversation_line(ts: str, display_name: str, content: str) -> str:
        """渲染认知层对话上下文中的单行。"""
        time_str = f"【{ts}】" if ts else ""
        return f"{time_str}【{display_name}】{content}"
