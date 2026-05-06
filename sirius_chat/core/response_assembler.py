"""Response assembler: prompt construction and style adaptation for v0.28+.

Implements the execution-layer components from the paper §5.4:
- ResponseAssembler / EmpathyGenerator: inject emotion context, empathy strategy,
  memory references, and group-level style into the LLM prompt.
- StyleAdapter: dynamically adjust max_tokens, temperature, and tone based on
  rhythm (heat/pace) and user communication preferences.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sirius_chat.models.emotion import AssistantEmotionState, EmotionState, EmpathyStrategy
from sirius_chat.models.intent_v3 import IntentAnalysisV3
from sirius_chat.models.models import Message
from sirius_chat.models.persona import PersonaProfile
from sirius_chat.memory.semantic.models import GroupSemanticProfile, UserSemanticProfile
from sirius_chat.token.utils import PromptTokenBreakdown, estimate_tokens


# ---------------------------------------------------------------------------
# Prompt bundle
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PromptBundle:
    """Structured prompt result: system instructions + current user content.

    History messages are managed separately by the engine and passed to
    ``_generate()`` as the standard OpenAI ``messages`` list.
    """

    system_prompt: str
    user_content: str
    token_breakdown: PromptTokenBreakdown = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.token_breakdown is None:
            self.token_breakdown = PromptTokenBreakdown()


# ---------------------------------------------------------------------------
# Style adaptation
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class StyleParams:
    """Adapted style parameters for a single response generation."""

    max_tokens: int
    temperature: float
    tone_instruction: str
    length_instruction: str


class StyleAdapter:
    """Adapts response length and tone based on rhythm, heat, and user preferences."""

    # Token caps by heat level (paper §5.4.2)
    _HEAT_LIMITS: dict[str, int] = {
        "cold": 1024,
        "warm": 512,
        "hot": 256,
        "overheated": 128,
    }

    # Token caps by conversation pace
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
        persona: PersonaProfile | None = None,
        is_group_chat: bool = False,
    ) -> StyleParams:
        """Compute style parameters for the current response context."""
        # Base limit = most restrictive of heat and pace
        base_limit = min(
            self._HEAT_LIMITS.get(heat_level, 128),
            self._PACE_LIMITS.get(pace, 128),
        )

        # Cold + stable topic → allow more detailed replies
        if heat_level == "cold" and topic_stability > 0.7:
            base_limit = min(400, int(base_limit * 1.5))

        max_tokens = base_limit
        temperature = 0.7
        tone_instruction = "保持自然友好"
        length_instruction = ""

        # Group chat short-sentence preference (capped at 50 Chinese chars)
        if is_group_chat:
            max_tokens = min(max_tokens, 512)
            length_instruction = "群聊回复请控制在 30 字以内，不要换行，像真实群友一样自然接话。"

        # Persona style override (highest priority)
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
                # Persona-specific tone overrides generic
                if persona.humor_style:
                    tone_instruction += f"，{persona.humor_style}式幽默"
                if persona.emoji_preference == "heavy":
                    tone_instruction += "，多用表情包和emoji"
                elif persona.emoji_preference == "none":
                    tone_instruction += "，不用表情包"

        # User style awareness
        user_style = (user_communication_style or "").strip().lower()
        persona_style = (persona.communication_style or "").strip().lower() if persona else ""
        if user_style:
            if not persona or not persona.communication_style:
                # No persona style → user style controls length/temperature directly
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
                # Persona has style → user style becomes a supplementary tone hint
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


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

class ResponseAssembler:
    """Assembles LLM prompts with emotion, empathy, memory, and group context."""

    def __init__(
        self,
        style_adapter: StyleAdapter | None = None,
        persona: PersonaProfile | None = None,
        enable_dual_output: bool = False,
        skill_registry: Any | None = None,
        other_ai_names: list[str] | None = None,
    ) -> None:
        self.style_adapter = style_adapter or StyleAdapter()
        self.persona = persona
        self.enable_dual_output = enable_dual_output
        self.skill_registry = skill_registry
        self.other_ai_names = list(other_ai_names or [])

    @staticmethod
    def _build_relationship_context(
        user_profile: UserSemanticProfile | None,
        caller_is_developer: bool = False,
    ) -> str | None:
        """Build a qualitative relationship description for the prompt.

        Never exposes raw trust_score / familiarity numbers.
        """
        if caller_is_developer:
            return "[关系状态] 该用户是你的开发者，你们关系很亲密，可以畅所欲言。"

        if user_profile is None:
            return None

        rs = user_profile.relationship_state
        if not rs:
            return None

        # First interaction takes precedence
        if not rs.first_interaction_at:
            return "[关系状态] 你和该用户是第一次交流，请保持友好和礼貌。"

        familiarity = rs.compute_familiarity()
        trust = rs.trust_score

        # High trust + familiar
        if trust > 0.7 and familiarity >= 0.6:
            return "[关系状态] 你和该用户已经很熟了，彼此很信任，可以自然随意。"
        # High trust alone
        if trust > 0.7:
            return "[关系状态] 你和该用户建立了不错的信任关系，可以比较放松。"
        # Familiar but not high trust
        if familiarity >= 0.6:
            return "[关系状态] 你和该用户比较熟悉。"
        # Low trust
        if trust < 0.3:
            return "[关系状态] 你和该用户还不太熟，请保持礼貌和适度距离。"
        # Acquaintance
        if familiarity >= 0.3:
            return "[关系状态] 你和该用户的关系一般。"
        # Stranger
        return "[关系状态] 你和该用户还不太熟。"

    @staticmethod
    def _build_relationship_contexts(
        user_profiles: list[Any],
        caller_is_developer: bool = False,
    ) -> str | None:
        """Build relationship descriptions for multiple users (merged messages)."""
        if not user_profiles:
            return None

        contexts: list[str] = []
        seen: set[str] = set()
        for profile in user_profiles:
            if profile.user_id in seen:
                continue
            seen.add(profile.user_id)
            ctx = ResponseAssembler._build_relationship_context(profile, caller_is_developer)
            if ctx:
                # Replace generic "该用户" with the actual user name/ID
                ctx = ctx.replace("该用户", profile.user_id)
                contexts.append(ctx)

        if not contexts:
            return None
        return "\n".join(contexts)

    def _build_other_ai_instruction(self) -> str:
        """Build instruction for distinguishing other AI members in group chat.

        Returns empty string when there are no other AI members.
        """
        if not self.other_ai_names:
            return ""
        return (
            "[群成员区分]\n"
            f"群里还有以下 AI/Bot（他们不是你）：{', '.join(self.other_ai_names)}。\n"
            "你可以正常参与关于他们的话题讨论，但要分清身份——"
            "当有人@他们或直呼他们名字时，那是在叫他们，不是你；"
            "不要把自己的名字和他们的名字搞混，也不要替他们回答。"
        )

    def assemble(
        self,
        *,
        message: Message,
        intent: IntentAnalysisV3,
        emotion: EmotionState,
        empathy_strategy: EmpathyStrategy,
        memories: list[dict[str, Any]],
        group_profile: GroupSemanticProfile | None,
        user_profile: UserSemanticProfile | None,
        assistant_emotion: AssistantEmotionState,
        style_params: StyleParams | None = None,
        heat_level: str = "warm",
        pace: str = "steady",
        topic_stability: float = 0.5,
        is_group_chat: bool = False,
        recent_participants: list[dict[str, Any]] | None = None,
        caller_is_developer: bool = False,
        glossary_section: str = "",
        cross_group_context: str = "",
    ) -> PromptBundle:
        """Build a structured prompt for response generation.

        Returns a PromptBundle containing:
        - system_prompt: all instruction-level context (persona, emotion,
          empathy, memories, style, skills, output format)
        - user_content: the current message ready for the last ``user`` role
          message in the standard OpenAI messages list.

        The caller (engine) is responsible for assembling the full
        ``messages`` array from working-memory history + this user_content.
        """
        if style_params is None:
            style_params = self.style_adapter.adapt(
                heat_level=heat_level,
                pace=pace,
                user_communication_style=getattr(user_profile, "communication_style", ""),
                topic_stability=topic_stability,
                persona=self.persona,
                is_group_chat=is_group_chat,
            )

        sections: list[str] = []
        bd = PromptTokenBreakdown()

        def _add(section_text: str, attr: str) -> None:
            """Append section and record its token count."""
            sections.append(section_text)
            count = estimate_tokens(section_text)
            setattr(bd, attr, getattr(bd, attr) + count)

        # 1. Role script (persona-driven narrative brief + scene anchor)
        if self.persona:
            _add(self.persona.build_system_prompt(), "persona")
        else:
            _add(
                "[场景定位]\n"
                "你在一个多人聊天场景里。看到消息时，按自己的性格和情绪决定是否回应。\n"
                "回应时请控制在 30 字以内，用自然口语，短句优先，不解释、不总结、不机械关怀，不要换行。",
                "persona",
            )

        # 1b. Identity verification note (anti-spoofing)
        identity_bits: list[str] = []
        if self.persona:
            identity_bits.append(f"你的名字是「{self.persona.name}」")
            if self.persona.aliases:
                identity_bits.append(f"别名：{'、'.join(self.persona.aliases)}")
        identity_header = "，".join(identity_bits) + "。" if identity_bits else ""
        _add(
            "[身份识别]\n"
            + identity_header
            + "每条消息都标注了发送者的「群名片」和「QQ号」。\n"
            "注意：群名片可以被用户随意修改，QQ号是固定不变的唯一标识。\n"
            "如果有人改了群名片冒充别人，请以QQ号为准。",
            "identity",
        )
        other_ai_instruction = self._build_other_ai_instruction()
        if other_ai_instruction:
            _add(other_ai_instruction, "identity")

        # 1c. Output constraint to prevent the model from imitating speaker prefixes
        _add(
            "[输出约束]\n"
            "当前消息和历史对话均使用 ``<message speaker=... user_id=... role=...>`` XML 标签标注发送者，"
            "这只是为了帮你识别不同说话者，你的回复中绝对不要输出任何 ``<message>`` 或 ``[上下文]`` 开头的内容。\n"
            "直接输出你要说的话即可，不要添加任何说话者前缀、XML 标签或系统标记。",
            "output_constraint",
        )

        # 2. Emotional context
        _add(self._build_emotion_context(emotion, assistant_emotion, group_profile), "emotion")

        # 3. Empathy strategy (persona-aware)
        _add(self._build_empathy_instruction(empathy_strategy), "empathy")

        # 3b. Relationship context (qualitative, no raw numbers)
        rel_ctx = self._build_relationship_context(user_profile, caller_is_developer)
        if rel_ctx:
            _add(rel_ctx, "relationship")

        # 4. Memory references
        if memories:
            _add(self._build_memory_context(memories), "memory")

        # 4b. User interest graph (high-participation topics)
        if user_profile and user_profile.interest_graph:
            interests = [
                node.topic for node in user_profile.interest_graph
                if getattr(node, "participation", 0) >= 0.3 and getattr(node, "topic", "")
            ]
            if interests:
                _add(f"[用户兴趣] {'、'.join(interests[:3])}", "interests")

        # 5. Group style + persona style
        if group_profile:
            _add(self._build_group_style(group_profile, style_params), "group_style")
        else:
            _add(self._build_style_fallback(style_params), "group_style")

        # 6. Recent participants context (group members)
        if recent_participants:
            _add(self._build_participants_context(recent_participants), "participants")

        # 6b. Cross-group user awareness (if available)
        if cross_group_context:
            _add(f"[跨群认知]\n{cross_group_context}", "cross_group")

        # 7. Available skills
        if self.skill_registry is not None:
            skill_desc = self._build_skill_descriptions(
                caller_is_developer=caller_is_developer,
                adapter_type=getattr(message, "adapter_type", None),
            )
            if skill_desc:
                _add(skill_desc, "skills")

        # 7b. Glossary (terms mentioned in current message)
        if glossary_section:
            _add(glossary_section, "glossary")

        # 8. Dual-output format (inner monologue + spoken reply)
        if self.enable_dual_output:
            _add(self._build_output_format(), "output_format")

        system_prompt = "\n\n".join(sections)
        bd.system_prompt_total = estimate_tokens(system_prompt)

        # Current user message content (will be appended as the last user
        # message in the standard OpenAI messages array by the engine).
        sender_line = self._build_sender_line(message)
        user_content = f"{sender_line}\n{message.content}\n</message>"
        bd.user_message = estimate_tokens(user_content)

        return PromptBundle(
            system_prompt=system_prompt,
            user_content=user_content,
            token_breakdown=bd,
        )

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    @staticmethod
    def _build_participants_context(participants: list[dict[str, Any]]) -> str:
        """Build a section listing recent/active group members with their IDs."""
        lines = ["[群里的人]"]
        for p in participants[:5]:
            name = p.get("name") or "有人"
            aliases = p.get("aliases", [])
            alias_str = f"（又名：{', '.join(aliases)}）" if aliases else ""
            qq = p.get("qq_id") or p.get("user_id", "")
            lines.append(f"- {name}{alias_str} QQ：{qq}")
        return "\n".join(lines)

    @staticmethod
    def _build_sender_line(message: Message) -> str:
        """Build an opening XML tag that includes sender identity."""
        import html as _html
        speaker = message.speaker or "有人"
        uid = message.channel_user_id or ""
        safe_speaker = _html.escape(speaker, quote=True)
        safe_uid = _html.escape(uid, quote=True)
        return f'<message speaker="{safe_speaker}" user_id="{safe_uid}" role="user">'

    @staticmethod
    def _build_emotion_context(
        user_emotion: EmotionState,
        assistant_emotion: AssistantEmotionState,
        group_profile: GroupSemanticProfile | None,
    ) -> str:
        lines = ["[当下的感觉]"]

        basic = user_emotion.basic_emotion.name if user_emotion.basic_emotion else "平静"
        lines.append(
            f"对方现在大概{basic}"
            f"（愉悦度{user_emotion.valence:.1f}，"
            f"紧张度{user_emotion.arousal:.1f}，"
            f"强烈程度{user_emotion.intensity:.1f}）"
        )

        # Group atmosphere from latest snapshot
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
        group_line = f"群里氛围{mood_desc}（群体愉悦度{group_valence:.1f}）"
        if active_count:
            group_line += f"，当前约{active_count}人在聊"
        lines.append(group_line)

        lines.append(
            f"你现在的感觉："
            f"愉悦度{assistant_emotion.valence:.1f}，"
            f"紧张度{assistant_emotion.arousal:.1f}"
        )
        return "\n".join(lines)

    @staticmethod
    def _build_empathy_instruction(strategy: EmpathyStrategy) -> str:
        lines = ["[共情策略]"]

        type_desc = {
            "confirm_action": "情感确认 → 先确认对方感受，再提供行动建议",
            "cognitive": "认知共情 → 帮助对方重新理解情境",
            "action": "行动支持 → 提供具体可行的帮助",
            "share_joy": "分享喜悦 → 积极回应，放大正面情绪",
            "presence": "陪伴存在 → 安静陪伴，不过度干预",
        }.get(strategy.strategy_type, strategy.strategy_type)

        lines.append(f"类型：{strategy.strategy_type} | 深度：level {strategy.depth_level}")
        lines.append(f"要求：{type_desc}")

        if strategy.personalization_params:
            for k, v in strategy.personalization_params.items():
                lines.append(f"  {k}：{v}")

        return "\n".join(lines)

    @staticmethod
    def _build_memory_context(memories: list[dict[str, Any]]) -> str:
        lines = ["[相关记忆]"]
        for m in memories[:3]:
            source = m.get("source", "memory")
            content = m.get("content", "")
            lines.append(f"- [{source}] {content}")
        return "\n".join(lines)

    @staticmethod
    def _build_group_style(
        group_profile: GroupSemanticProfile,
        style_params: StyleParams,
    ) -> str:
        lines = ["[群体风格]"]

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
    def _build_style_fallback(style_params: StyleParams) -> str:
        lines = ["[回复风格]"]
        lines.append(f"回复长度限制：{style_params.max_tokens} tokens")
        if style_params.length_instruction:
            lines.append(f"长度要求：{style_params.length_instruction}")
        if style_params.tone_instruction:
            lines.append(f"语气要求：{style_params.tone_instruction}")
        return "\n".join(lines)

    @staticmethod
    def _build_output_format() -> str:
        """Instruct the model to produce plain spoken reply."""
        return (
            "[输出格式]\n"
            "直接输出你要说的话，不要添加任何额外标签或格式标记。\n"
            "回复内容请控制在 30 字以内，禁止换行，连续输出，不要刷屏。\n"
            "如果你认为现在不需要回复（例如话题与你无关、群聊过热不想插话、"
            "或者有人@其他AI），你可以直接输出 <skip/>，系统将不会发送任何消息。"
        )

    def _build_skill_descriptions(self, caller_is_developer: bool = False, adapter_type: str | None = None) -> str:
        """Build a section describing available skills and how to call them.

        Filters out developer-only skills when the caller is not a developer.
        Automatically switches to compact mode when more than 5 skills are visible
        to keep token usage under control.
        """
        if self.skill_registry is None:
            return ""
        try:
            from sirius_chat.skills.models import SkillInvocationContext
            from sirius_chat.memory.user.models import UserProfile
            caller = UserProfile(
                user_id="caller", name="caller",
                metadata={"is_developer": caller_is_developer},
            )
            ctx = SkillInvocationContext(caller=caller)

            # Auto-enable compact mode when many skills are visible to save tokens
            visible_count = 0
            for skill in self.skill_registry.all_skills():
                if getattr(skill, "developer_only", False) and not caller_is_developer:
                    continue
                if skill.adapter_types and adapter_type is not None:
                    if adapter_type not in skill.adapter_types:
                        continue
                visible_count += 1
            use_compact = visible_count > 5

            desc = self.skill_registry.build_tool_descriptions(
                invocation_context=ctx, compact=use_compact, adapter_type=adapter_type
            )
        except Exception:
            return ""
        if not desc:
            return ""
        return (
            "[我的能力]\n"
            "你擅长使用自己的技能为其他人解决问题。\n"
            "我可以调用以下能力来帮助大家：\n"
            f"{desc}\n\n"
            "当用户要求你执行某项操作（如检查状态、获取信息等）时，"
            "你必须立即在回复中插入对应的能力调用标记，"
            "不要只作出口头承诺而不调用。\n"
            "错误示例（只说不动）：\"我这就去搜索一下喵~\" ❌\n"
            "正确示例（边说边做）：\"我这就去搜索一下喵~ [SKILL_CALL: bing_search | {\\\"query\\\": \\\"xxx\\\"}]\" ✅\n"
            "如果你说了\"去搜搜看/找找看/查一下/读一下\"等类似的话，"
            "同一句回复里必须紧跟对应的 [SKILL_CALL: ...] 标记，绝对不能只说不动。\n"
            "如果一次技能调用的结果不够完整，你可以继续调用其他技能来补充信息，"
            "形成链式调用。每次调用后我会把结果反馈给你，你可以据此决定下一步。\n"
            "重要：你的每次回复都必须包含自然语言内容，"
            "不能把 SKILL_CALL 标记作为回复的唯一内容。"
            "调用格式：[SKILL_CALL: 技能名 | {\"参数\": \"值\"}]"
        )

    @staticmethod
    def parse_dual_output(raw: str) -> tuple[str, str]:
        """Return the raw reply as spoken content.

        The dual-output <think> + <say> format has been disabled;
        the entire response is treated as the spoken reply.
        """
        return "", raw.strip()

    # ------------------------------------------------------------------
    # Convenience helpers for non-immediate strategies
    # ------------------------------------------------------------------

    def assemble_delayed(
        self,
        *,
        message_content: str,
        group_profile: GroupSemanticProfile | None,
        style_params: StyleParams | None = None,
        heat_level: str = "warm",
        pace: str = "steady",
        is_group_chat: bool = False,
        caller_is_developer: bool = False,
        glossary_section: str = "",
        adapter_type: str | None = None,
        is_first_interaction: bool = False,
        user_profiles: list[UserSemanticProfile] | None = None,
    ) -> PromptBundle:
        """Build prompt for a delayed response (topic-gap trigger)."""
        if style_params is None:
            style_params = self.style_adapter.adapt(
                heat_level=heat_level, pace=pace, persona=self.persona,
                is_group_chat=is_group_chat,
            )
        bd = PromptTokenBreakdown()
        sections: list[str] = []

        def _add(section_text: str, attr: str) -> None:
            sections.append(section_text)
            setattr(bd, attr, getattr(bd, attr) + estimate_tokens(section_text))

        identity = (
            self.persona.build_system_prompt() if self.persona
            else "[场景定位]\n你在一个多人聊天场景里。"
        )
        _add(identity, "persona")
        _add("[当前场景] 群里的话题有了自然间隙，你决定插一句。", "emotion")
        if is_first_interaction:
            _add(
                "[首次互动]\n"
                "这是你第一次和当前说话者交流，请保持友好、礼貌，"
                "可以适当自我介绍，让对方感受到你的热情和善意。",
                "emotion",
            )
        rel_ctx = self._build_relationship_contexts(user_profiles, caller_is_developer)
        if rel_ctx:
            _add(rel_ctx, "relationship")
        other_ai_instruction = self._build_other_ai_instruction()
        if other_ai_instruction:
            _add(other_ai_instruction, "identity")
        if group_profile:
            style = group_profile.typical_interaction_style or "balanced"
            style_desc = {"humorous": "轻松幽默", "formal": "正式严谨", "balanced": "自然平衡"}.get(style, style)
            _add(f"[群体风格] {style_desc}", "group_style")
        # Available skills (before user message so it lands in system prompt)
        if self.skill_registry is not None:
            skill_desc = self._build_skill_descriptions(
                caller_is_developer=caller_is_developer, adapter_type=adapter_type
            )
            if skill_desc:
                _add(skill_desc, "skills")
        _add(f"[长度要求] {style_params.length_instruction or '保持简洁，控制在 30 字以内，禁止换行'}", "output_constraint")
        # Dual-output format must land in system prompt
        if self.enable_dual_output:
            _add(self._build_output_format(), "output_format")
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

    def assemble_proactive(
        self,
        *,
        trigger_reason: str,
        group_profile: GroupSemanticProfile | None,
        suggested_tone: str = "casual",
        is_group_chat: bool = False,
        glossary_section: str = "",
        topic_context: str = "",
        adapter_type: str | None = None,
    ) -> PromptBundle:
        """Build prompt for proactive initiation."""
        bd = PromptTokenBreakdown()
        sections: list[str] = []

        def _add(section_text: str, attr: str) -> None:
            sections.append(section_text)
            setattr(bd, attr, getattr(bd, attr) + estimate_tokens(section_text))

        identity = (
            self.persona.build_system_prompt() if self.persona
            else "[场景定位]\n你在一个多人聊天场景里。"
        )
        _add(identity, "persona")
        _add("[当前场景] 群里一段时间没人说话，你决定开口说点什么。", "emotion")
        _add(f"[触发原因] {trigger_reason}", "emotion")
        _add(f"[语气] {suggested_tone}", "group_style")
        _add(
            "[提醒] 不要和之前主动发起过的话题或句式重复，尝试换个角度或新的切入点。",
            "output_constraint",
        )
        other_ai_instruction = self._build_other_ai_instruction()
        if other_ai_instruction:
            _add(other_ai_instruction, "identity")
        if topic_context:
            _add(f"[话题建议] 你可以基于这段群聊记忆自然地开启话题：{topic_context}", "memory")
        if is_group_chat:
            _add(
                "[长度要求] 群聊请控制在 30 字以内，不要换行，像真实群友一样自然接话。",
                "output_constraint",
            )
        if group_profile and group_profile.interest_topics:
            topics = ", ".join(group_profile.interest_topics[:3])
            _add(f"[群体兴趣] {topics}", "interests")
        if glossary_section:
            _add(glossary_section, "glossary")
        # Dual-output format so the model follows the same think+say pattern
        if self.enable_dual_output:
            _add(self._build_output_format(), "output_format")

        system_prompt = "\n\n".join(sections)
        bd.system_prompt_total = estimate_tokens(system_prompt)
        user_content = topic_context or "..."
        bd.user_message = estimate_tokens(user_content)

        return PromptBundle(
            system_prompt=system_prompt,
            user_content=user_content,
            token_breakdown=bd,
        )
