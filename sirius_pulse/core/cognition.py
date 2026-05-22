"""Unified cognition analyzer: joint emotion + intent inference.

Philosophy alignment (v0.28+):
    Perceiving others' feelings and understanding their intent are two
    sides of the same cognitive act. We analyze them jointly:

    - Rule engine covers ~90% of cases at zero LLM cost.
    - Single LLM fallback covers the remaining ~10% with one cheap call.
    - Emotion flows naturally into intent scoring without async boundary.

"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any

from sirius_pulse.models.emotion import BasicEmotion, EmotionState, EmpathyStrategy
from sirius_pulse.models.intent_v3 import (
    EmotionalSubtype,
    HelpSubtype,
    IntentAnalysisV3,
    SilentSubtype,
    SocialIntent,
    SocialSubtype,
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Emotion rule engine
# ------------------------------------------------------------------

_DEFAULT_LEXICON: dict[str, float] = {
    # Positive
    "开心": 0.8,
    "高兴": 0.9,
    "快乐": 0.85,
    "棒": 0.8,
    "好": 0.6,
    "喜欢": 0.7,
    "爱": 0.9,
    "感动": 0.7,
    "欣慰": 0.6,
    "满足": 0.7,
    "期待": 0.5,
    "兴奋": 0.85,
    "激动": 0.8,
    "惊喜": 0.7,
    "感谢": 0.6,
    "哈哈": 0.5,
    "嘿嘿": 0.4,
    "yyds": 0.9,
    "xswl": 0.8,
    "awsl": 0.7,
    "绝绝子": 0.7,
    "赞": 0.7,
    "牛逼": 0.7,
    "太棒了": 0.8,
    # Negative
    "难过": -0.7,
    "伤心": -0.8,
    "悲伤": -0.85,
    "痛苦": -0.9,
    "生气": -0.6,
    "愤怒": -0.8,
    "恼火": -0.5,
    "烦": -0.5,
    "讨厌": -0.6,
    "恶心": -0.7,
    "厌恶": -0.6,
    "失望": -0.6,
    "害怕": -0.7,
    "担心": -0.5,
    "焦虑": -0.6,
    "紧张": -0.5,
    "累": -0.4,
    "疲惫": -0.5,
    "绝望": -0.9,
    "崩溃": -0.9,
    "无语": -0.3,
    "郁闷": -0.5,
    "emo": -0.6,
    "蚌埠住了": -0.3,
    "呜呜": -0.6,
    "泪目": -0.4,
    "扎心": -0.5,
    "难受": -0.6,
    # Ambiguous / context-dependent
    "确实": 0.0,
    "好吧": -0.1,
    "哦": 0.0,
    "嗯": 0.0,
}

# ------------------------------------------------------------------
# Intent rule engine
# ------------------------------------------------------------------

_HELP_PATTERNS = [
    r"怎么\s*\S+",
    r"如何\s*\S+",
    r"为什么\s*\S+",
    r"有人.*吗",
    r"求助",
    r"请教",
    r"大佬",
    r"救命",
    r"报错",
    r"错误",
    r"exception",
    r"error",
    r"failed",
]

_EMOTIONAL_INDICATORS = [
    "感觉",
    "觉得",
    "心情",
    "难受",
    "开心",
    "难过",
    "累",
    "烦",
    "郁闷",
    "兴奋",
    "sad",
    "happy",
    "upset",
    "excited",
    "tired",
    "孤独",
    "寂寞",
    "压力",
]

_SOCIAL_INDICATORS = [
    "大家觉得",
    "有没有人",
    "一起",
    "推荐",
    "分享",
    "讨论",
    "聊聊",
    "怎么样",
    "如何看",
]

_URGENCY_KEYWORDS = {
    "high": {
        "崩溃",
        "救命",
        "急",
        "马上",
        "立刻",
        "现在",
        "死了",
        "完了",
        "urgent",
        "emergency",
        "asap",
        "help",
        "broken",
        "crash",
    },
    "medium": {
        "求助",
        "请问",
        "怎么",
        "如何",
        "为什么",
        "不懂",
        "不会",
        "confused",
        "stuck",
        "problem",
        "issue",
        "question",
    },
    "low": {
        "想问问",
        "好奇",
        "了解一下",
        "有空的话",
        "方便时",
        "wondering",
        "curious",
        "when you have time",
    },
}

# ------------------------------------------------------------------
# Joint LLM fallback prompt
# ------------------------------------------------------------------

# 主观题/观点询问关键词 —— 被点名时出现这些词应触发 IMMEDIATE
_SUBJECTIVE_KEYWORDS: tuple[str, ...] = (
    "你觉得", "你认为", "你怎么看", "你的看法", "你喜欢",
    "你觉得呢", "你觉得怎么样", "你的意见", "你觉得如何",
    "你更喜欢", "你最", "你讨厌", "你不喜欢", "你觉得好",
    "你怎么看",
)

# 需要上下文才能正确理解的短消息模式 —— 单独看像 filler，但有上下文时应视为对话延续
_CONTEXT_DEPENDENT_PATTERNS: tuple[str, ...] = (
    "为什么", "怎么回事", "真的吗", "那怎么办", "怎么办呢",
    "然后呢", "后来呢", "什么意思", "怎么说", "不会吧",
    "这样啊", "原来如此", "懂了", "这样吗", "那行", "好吧",
    "哦", "嗯嗯", "对对", "确实", "可以", "好的", "行吧",
)

_LLM_COGNITION_PROMPT = """分析以下消息的【情感状态】、【社交意图】和【指向性】。

{ai_identity}{conversation_context}消息：{message}

{plugin_descriptions}
要求输出 JSON：
{{
  "valence": -1.0 到 1.0（愉悦度，负值负面，正值正面）,
  "arousal": 0.0 到 1.0（唤醒度，0平静，1激动）,
  "intensity": 0.0 到 1.0（情感强度）,
  "basic_emotion": "joy|anger|sadness|anxiety|loneliness|neutral",
  "social_intent": "help_seeking|emotional|social|silent|plugin_command",
  "intent_subtype": "tech_help|info_query|venting|seeking_empathy|topic_discussion|filler",
  "plugin_intent": "仅当用户消息【明确请求】某个插件功能时才填写对应插件ID。消息只是提及相关概念（如聊到AI/天气/代码）不等于请求插件。如果不确定，留空。{plugin_slots_hint}",
  "plugin_slots": {{ "参数名": 参数值（int/float类型传数字不要加引号，无对应信息时用默认值） }},
  "urgency_score": 0-100,
  "relevance_score": 0.0-1.0,
  "directed_score": 0.0-1.0,
  "directed_reason": "一句话解释指向性判断原因",
  "sarcasm_score": 0.0-1.0,
  "confidence": 0.0-1.0,
  "search_query": "用于检索记忆的一句话查询，概括用户核心需求（不是标签，是自然语言）。如果查询内容中包含双引号，请用单引号替代",
  "image_caption": "如果消息包含图片，请用1-2句话描述图片内容，并说明图片与消息意图的关系。如果是表情包/动画表情，请着重描述角色的神态、动作、表情细节、肢体语言、以及它传达的情绪和氛围（如'得意洋洋的挑眉'、'委屈巴巴地缩成一团'、'疯狂拍桌大笑'）。如果没有图片，留空。"
}}

定义：
- help_seeking: 求助、提问、报错
- emotional: 表达情绪、寻求安慰
- social: 闲聊、讨论、分享
- silent: 无意义 filler（哈哈、确实、+1）
- plugin_command: 【慎用】仅当用户消息是明确的插件功能请求时使用。判断标准：去掉插件后用户消息是否毫无意义？例如"帮我查一下北京的天气"去掉天气插件就没意义→是plugin_command；"今天天气真好"去掉天气插件仍是正常闲聊→social。消息中仅仅提到和插件相关的词语（如"天气"、"分析"）不等于插件请求。

【评分标准】
urgency_score（紧急程度，参考）：
- 80-100：紧急求助、情绪崩溃、明确要求立刻回复的提问
- 60-79：被点名询问看法、有明确问题需要回答
- 30-59：普通闲聊、话题讨论、分享日常（大部分群聊属于此类）
- 0-29：无意义附和、filler、表情包

relevance_score（相关程度，参考）：
- 0.7-1.0：与 AI 角色直接相关、被点名、需要 AI 参与决策
- 0.4-0.69：一般群聊话题，AI 可参与但不是必须
- 0.0-0.39：与 AI 无关、私人话题、纯发泄

directed_score（消息指向 AI 的程度，最关键）：
- 1.0：明确@AI、回复AI消息、直呼名字提问（最强指向）
- 0.7-0.9：没叫名字但语义明显在问AI（如"你觉得呢"且AI是最近发言者）
- 0.4-0.6：话题与AI人设/兴趣相关，或群聊中泛泛提及AI
- 0.0-0.3：与AI无关、纯群友闲聊、提到AI名字但只是举例/引用
注意：要综合考虑消息内容、对话上下文和当前发言者身份。如果群聊里只有AI和当前发言者活跃，"你"大概率指向AI。
{ai_identity_note}
只输出 JSON，不要其他内容。"""


class CognitionAnalyzer:
    """Joint emotion + intent analyzer with unified rule engine and single LLM fallback.

    Replaces the sequential EmotionAnalyzer → IntentAnalyzerV3 pipeline with:
        1. Parallel rule-based emotion + intent scoring (zero cost)
        2. Single joint LLM fallback when either score is low-confidence
        3. Shared context fusion (trajectory + group sentiment)
        4. Unified empathy strategy selection
    """

    def __init__(
        self,
        lexicon: dict[str, float] | None = None,
        provider_async: Any | None = None,
        model_name: str = "gpt-4o-mini",
        ai_name: str = "",
        ai_aliases: list[str] | None = None,
        persona: Any | None = None,
        plugin_registry: Any | None = None,  # Plugin 注册表（v1.2+）
        brain: Any | None = None,  # Brain 实例（用于统一 LLM 调用）
    ) -> None:
        self.lexicon = lexicon or dict(_DEFAULT_LEXICON)
        self.provider_async = provider_async
        self.model_name = model_name
        self.ai_name = ai_name
        self.ai_aliases = [a.lower() for a in (ai_aliases or []) if a]
        self.persona = persona
        self.plugin_registry = plugin_registry  # Plugin 注册表引用（v1.2+）
        self.brain = brain  # Brain LLM 交互中枢（v1.2+）

        # Expose the last GenerationRequest for token recording
        self._last_request: Any | None = None

        # Emotion state tracking
        self.trajectories: dict[str, list[tuple[str, EmotionState]]] = {}
        self.group_cache: dict[str, EmotionState] = {}
        self.empathy_prefs: dict[str, dict[str, Any]] = {}

        # Intent state tracking
        self.group_activity_history: dict[str, list[tuple[float, float]]] = {}
        self.user_response_prefs: dict[str, dict[str, Any]] = {}

        # Image caption cache: url/path -> caption, avoids repeated vision calls
        self._image_caption_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyze(
        self,
        message: str,
        user_id: str,
        group_id: str | None = None,
        context_messages: list[dict[str, Any]] | None = None,
        *,
        sender_type: str = "human",
        multimodal_inputs: list[dict[str, str]] | None = None,
        caller_is_developer: bool = False,
    ) -> tuple[EmotionState, IntentAnalysisV3, EmpathyStrategy]:
        """Joint analysis: emotion, intent, directedness, and empathy in one pass.

        Returns:
            (emotion_state, intent_analysis, empathy_strategy)
        """
        # 1. Rule-based emotion analysis
        text_emotion = self._text_analysis(message)

        # 2. Rule-based intent classification (fallback only)
        social_intent, subtype, intent_confidence = self._classify_intent(
            message, context_messages, caller_is_developer=caller_is_developer
        )
        search_query = message  # fallback when no LLM or LLM fails

        # 提取 Plugin 命令信息（v1.2+）
        plugin_intent: str | None = None
        plugin_confidence: float = 0.0
        plugin_slots: dict[str, Any] = {}
        plugin_render_mode: str = "direct"
        if social_intent == SocialIntent.PLUGIN_COMMAND and hasattr(subtype, 'plugin_name'):
            plugin_intent = subtype.plugin_name
            plugin_confidence = subtype.confidence
            plugin_slots = subtype.slots
            plugin_render_mode = subtype.render_mode

        # 3. Compute 12-dimensional directedness scores (rule-based, zero cost)
        directed_scores = self._compute_directed_scores(
            message, user_id, context_messages
        )

        # 4. Intent analysis via LLM when provider is available.
        #    LLM failure → rule-based scores remain (safe degradation).
        llm_result: dict[str, Any] | None = None
        llm_urgency: float | None = None
        llm_relevance: float | None = None
        llm_directed_score: float | None = None
        if self.provider_async is not None:
            try:
                llm_result = await self._llm_cognition(
                    message, context_messages, current_user_id=user_id, sender_type=sender_type,
                    multimodal_inputs=multimodal_inputs,
                    caller_is_developer=caller_is_developer,
                )
                if llm_result is not None:
                    social_intent = llm_result["social_intent"]
                    subtype = llm_result["subtype"]
                    intent_confidence = llm_result.get("confidence", 0.85)
                    llm_urgency = llm_result.get("urgency_score")
                    llm_relevance = llm_result.get("relevance_score")
                    llm_directed_score = llm_result.get("directed_score")
                    search_query = llm_result.get("search_query", message)
                    if text_emotion.confidence < 0.6:
                        text_emotion = llm_result["emotion"]
                    # LLM 覆盖了 social_intent → 清除规则匹配残留的 plugin 信息
                    if social_intent != SocialIntent.PLUGIN_COMMAND:
                        plugin_intent = None
                        plugin_confidence = 0.0
                        plugin_slots = {}
                        plugin_render_mode = "direct"
                    # 从 LLM 结果中提取 Plugin 字段（v1.2+）
                    # 规则匹配已在 _classify_intent 中优先处理精确前缀，
                    # 此处 LLM 返回 plugin_command 说明是自然语言触发（如"帮我查天气"）
                    if social_intent == SocialIntent.PLUGIN_COMMAND:
                        llm_plugin = llm_result.get("plugin_intent")
                        if llm_plugin:
                            validated = self._validate_plugin_intent(
                                llm_plugin, caller_is_developer=caller_is_developer
                            )
                            if validated:
                                plugin_intent = validated
                                plugin_confidence = max(plugin_confidence, intent_confidence)
                                llm_slots = llm_result.get("plugin_slots", {})
                                if isinstance(llm_slots, dict) and llm_slots:
                                    plugin_slots.update(llm_slots)
                            else:
                                # 无效的 plugin_intent → 降级
                                logger.info(
                                    "LLM plugin_intent '%s' 未通过校验，降级为 help_seeking",
                                    llm_plugin,
                                )
                                social_intent = SocialIntent.HELP_SEEKING
                                subtype = HelpSubtype.INFO_QUERY
                                plugin_intent = None
                                plugin_confidence = 0.0
                                plugin_slots = {}
                        else:
                            # plugin_command 但没有 plugin_intent → 降级
                            social_intent = SocialIntent.HELP_SEEKING
                            subtype = HelpSubtype.INFO_QUERY
                else:
                    # LLM parse failure → safe SILENT
                    social_intent = SocialIntent.SILENT
                    subtype = SilentSubtype.IRRELEVANT
                    intent_confidence = 0.3
                    plugin_intent = None
                    plugin_confidence = 0.0
                    plugin_slots = {}
            except Exception as exc:
                logger.warning("LLM cognition failed: %s", exc)
                social_intent = SocialIntent.SILENT
                subtype = SilentSubtype.IRRELEVANT
                intent_confidence = 0.3
                plugin_intent = None
                plugin_confidence = 0.0
                plugin_slots = {}

        # 5. Emotion context fusion
        context_emotion = self._context_inference(user_id)
        group_emotion = self.group_cache.get(group_id) if group_id else None
        emotion = self._fuse_emotion(text_emotion, context_emotion, group_emotion)
        self._update_trajectory(user_id, emotion)

        # 规范化 subtype（PluginMatchInfo → 字符串，v1.2+）
        if hasattr(subtype, 'plugin_name'):
            subtype_str = "plugin_command"
        elif hasattr(subtype, 'value'):
            subtype_str = subtype.value
        else:
            subtype_str = str(subtype)

        # 6. Intent scoring
        urgency = self._calculate_urgency(
            message, user_id, group_id, emotion, context_messages
        )
        relevance = self._calculate_relevance(message, social_intent, user_id, group_id)
        # Prefer LLM's urgency/relevance when available
        if llm_urgency is not None and llm_urgency > 0:
            urgency = llm_urgency
        if llm_relevance is not None and llm_relevance > 0:
            relevance = llm_relevance
        threshold = 0.45
        priority = 4
        response_time = 45.0

        # 7. Social signal decoding
        sarcasm_score = self._detect_sarcasm_score(message)
        # Blend with LLM sarcasm score if available
        if llm_result is not None:
            llm_sarcasm = llm_result.get("sarcasm_score", 0.0)
            if llm_sarcasm > 0.0:
                sarcasm_score = max(sarcasm_score, llm_sarcasm)
        entitlement_score = self._compute_entitlement_score(message, social_intent)

        # 8. Synthesize directed score (rule-based 12-dim + LLM semantic)
        llm_confidence = intent_confidence if llm_result is not None else 0.5
        directed_score = self._synthesize_directed_score(
            directed_scores, llm_directed_score, llm_confidence
        )
        # Boost directed_score if sarcasm is detected (sarcasm often targets someone)
        if sarcasm_score >= 0.4:
            directed_score = min(1.0, directed_score + sarcasm_score * 0.15)

        # Discount directedness when message is from another AI
        if sender_type == "other_ai":
            directed_score = min(directed_score, directed_score * 0.5 + 0.1)
        directed = directed_score >= 0.6

        if directed:
            # If explicitly addressed, never treat as silent filler
            if social_intent == SocialIntent.SILENT:
                social_intent = SocialIntent.SOCIAL
                subtype = SocialSubtype.TOPIC_DISCUSSION if subtype == SilentSubtype.FILLER else subtype

            is_question = "?" in message or "？" in message
            is_subjective = any(kw in message for kw in _SUBJECTIVE_KEYWORDS)

            if is_question or is_subjective:
                urgency = max(urgency, 80.0)
                relevance = max(relevance, 0.75)
            else:
                urgency = max(urgency, 70.0)
                relevance = max(relevance, 0.65)

        # Plugin 命令始终视为高指向性、高紧急度（v1.2+）
        if social_intent == SocialIntent.PLUGIN_COMMAND:
            directed = True
            directed_score = max(directed_score, 0.9)
            urgency = max(urgency, 80.0)
            relevance = max(relevance, 0.8)
            priority = 10  # 最高优先级

        intent = IntentAnalysisV3(
            intent_type=self._intent_type_from_social(social_intent, message),
            social_intent=social_intent,
            intent_subtype=subtype_str,
            urgency_score=urgency,
            relevance_score=relevance,
            confidence=intent_confidence,
            response_priority=priority,
            estimated_response_time=response_time,
            search_query=search_query,
            threshold=threshold,
            directed_at_current_ai=directed,
            # 12-dimensional directedness scores
            mention_score=directed_scores["mention_score"],
            reference_score=directed_scores["reference_score"],
            at_all_score=directed_scores["at_all_score"],
            name_match_score=directed_scores["name_match_score"],
            second_person_score=directed_scores["second_person_score"],
            question_score=directed_scores["question_score"],
            imperative_score=directed_scores["imperative_score"],
            topic_relevance_score=directed_scores["topic_relevance_score"],
            emotional_disclosure_score=directed_scores["emotional_disclosure_score"],
            attention_seeking_score=directed_scores["attention_seeking_score"],
            recency_score=directed_scores["recency_score"],
            turn_taking_score=directed_scores["turn_taking_score"],
            directed_score=directed_score,
            sarcasm_score=sarcasm_score,
            entitlement_score=entitlement_score,
            image_caption=llm_result.get("image_caption", "") if llm_result else "",
            # Plugin 字段（v1.2+）
            plugin_intent=plugin_intent,
            plugin_confidence=plugin_confidence,
            plugin_slots=plugin_slots,
            plugin_render_mode=plugin_render_mode,
        )

        if plugin_intent:
            logger.info(
                "意图分析 → 插件 %s: confidence=%.2f, slots=%s",
                plugin_intent,
                plugin_confidence,
                {k: (v, type(v).__name__) for k, v in plugin_slots.items()},
            )

        # 8. Empathy strategy
        empathy = self.select_empathy_strategy(emotion, user_id)

        return emotion, intent, empathy

    def select_empathy_strategy(
        self,
        emotion: EmotionState,
        user_id: str,
    ) -> EmpathyStrategy:
        """Select empathy strategy based on emotion state."""
        user_pref = self.empathy_prefs.get(user_id, {})

        if emotion.valence < -0.5 and emotion.arousal > 0.7:
            strategy_type = "confirm_action"
            priority = 1
            depth = 3
        elif emotion.valence < -0.3:
            strategy_type = "cognitive"
            priority = 2
            depth = 2
        elif emotion.valence > 0.5:
            strategy_type = "share_joy"
            priority = 3
            depth = 2
        else:
            strategy_type = "presence"
            priority = 4
            depth = 1

        if user_pref.get("prefer_direct") and strategy_type == "cognitive":
            strategy_type = "action"

        return EmpathyStrategy(
            strategy_type=strategy_type,
            priority=priority,
            depth_level=depth,
            personalization_params=user_pref,
        )

    # ------------------------------------------------------------------
    # Group sentiment
    # ------------------------------------------------------------------

    def update_group_sentiment(
        self,
        group_id: str,
        emotion: EmotionState,
    ) -> None:
        """Update group sentiment cache with exponential moving average."""
        existing = self.group_cache.get(group_id)
        if existing is None:
            self.group_cache[group_id] = EmotionState(
                valence=emotion.valence,
                arousal=emotion.arousal,
                intensity=emotion.intensity,
                confidence=0.5,
            )
        else:
            alpha = 0.3
            self.group_cache[group_id] = EmotionState(
                valence=existing.valence * (1 - alpha) + emotion.valence * alpha,
                arousal=existing.arousal * (1 - alpha) + emotion.arousal * alpha,
                intensity=existing.intensity * (1 - alpha) + emotion.intensity * alpha,
                confidence=min(1.0, existing.confidence + 0.05),
            )

    # ------------------------------------------------------------------
    # LLM fallback
    # ------------------------------------------------------------------

    def _build_persona_identity(self) -> str:
        """Build a concise persona description for the LLM cognition prompt."""
        if not self.persona and not self.ai_name:
            return ""

        parts: list[str] = []
        name = self.ai_name
        if self.persona:
            name = self.persona.name or name
        if name:
            parts.append(f"你是{name}。")

        if self.persona:
            p = self.persona
            if p.persona_summary:
                parts.append(p.persona_summary)
            elif p.backstory:
                # First sentence only, max 40 chars
                first = p.backstory.split("。")[0] + "。" if "。" in p.backstory else p.backstory
                parts.append(first[:60])
            if p.personality_traits:
                parts.append(f"你的性格是{'、'.join(p.personality_traits[:3])}。")
            if p.communication_style:
                parts.append(f"说话风格：{p.communication_style}。")
            if p.social_role:
                parts.append(f"在群里通常是{p.social_role}角色。")

        if not parts:
            return ""
        return "\n【角色身份】" + "".join(parts) + "\n"

    @staticmethod
    def _format_context_for_prompt(
        context_messages: list[dict[str, Any]] | None,
        max_turns: int = 4,
        ai_name: str = "",
        current_user_id: str = "",
    ) -> tuple[str, list[str]]:
        """Format recent conversation context for LLM prompt.

        Returns (context_text, active_participants).
        """
        if not context_messages:
            return "", []
        participants: set[str] = set()
        lines: list[str] = []
        for msg in context_messages[-max_turns:]:
            uid = msg.get("user_id", "unknown")
            content = msg.get("content", "")
            ts = msg.get("timestamp", "")
            if uid:
                participants.add(uid)
            if not content:
                continue
            # Mark AI messages explicitly
            display_name = f"{uid}(AI)" if uid == "assistant" or (ai_name and uid == ai_name) else uid
            time_str = f"【{ts}】" if ts else ""
            lines.append(f"{time_str}【{display_name}】{content}")
        context_text = "\n最近对话上下文：\n" + "\n".join(lines) + "\n" if lines else ""
        return context_text, sorted(participants)

    @staticmethod
    def _build_multimodal_messages(
        message: str,
        multimodal_inputs: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Build OpenAI-compatible multimodal messages for vision cognition."""
        # Detect if any item is a sticker so we can tell the model explicitly.
        has_sticker = any(
            item.get("type") == "image" and item.get("sub_type") == "1"
            for item in multimodal_inputs
        )
        prefix = "【动画表情】" if has_sticker else "【图片】"
        content: list[dict[str, Any]] = [
            {"type": "text", "text": message or prefix},
        ]
        for item in multimodal_inputs:
            if item.get("type") == "image":
                content.append(
                    {"type": "image_url", "image_url": {"url": str(item["value"])}}
                )
        return [{"role": "user", "content": content}]

    async def _llm_cognition(
        self,
        message: str,
        context_messages: list[dict[str, Any]] | None = None,
        *,
        current_user_id: str = "",
        sender_type: str = "human",
        multimodal_inputs: list[dict[str, str]] | None = None,
        caller_is_developer: bool = False,
    ) -> dict[str, Any] | None:
        """Single LLM call for joint emotion + intent + directedness analysis."""
        from sirius_pulse.providers.base import GenerationRequest, LLMProvider
        import asyncio

        persona_identity = self._build_persona_identity()
        if self.ai_name:
            ai_id = f"{persona_identity}当前 AI 名字：{self.ai_name}"
            if self.ai_aliases:
                ai_id += f"，别名：{', '.join(self.ai_aliases)}"
            ai_id += "\n"
            ai_note = (
                f"注意：如果消息中提到了当前 AI 的名字或别名，"
                f"social_intent 必须是 social（不是 silent），"
                f"且如果消息是提问或询问看法，urgency_score 至少为 80，relevance_score 至少为 0.75。\n"
            )
        else:
            ai_id = persona_identity
            ai_note = ""

        context_text, _ = self._format_context_for_prompt(
            context_messages, ai_name=self.ai_name or ""
        )
        # Build conversation context block with participant info
        conv_ctx = ""
        if context_messages:
            participants = sorted({
                m.get("user_id", "")
                for m in context_messages
                if m.get("user_id")
            })
            if participants:
                conv_ctx += f"\n【对话参与者】{', '.join(participants)}\n"
            if current_user_id:
                conv_ctx += f"【当前发言者】{current_user_id}\n"
        if sender_type == "other_ai":
            conv_ctx += "【注意：当前消息来自群里的另一个 AI，不是人类用户】\n"

        prompt = _LLM_COGNITION_PROMPT.format(
            ai_identity=ai_id,
            conversation_context=conv_ctx,
            message=context_text + f"【当前消息】[{current_user_id}] {message}",
            ai_identity_note=ai_note,
            plugin_descriptions=self._get_plugin_descriptions_for_prompt(caller_is_developer),
            plugin_slots_hint=self._get_plugin_slots_hint(caller_is_developer),
        )

        # Check image caption cache before calling LLM.
        # Use content hash extracted from local cache path as key, so the same
        # image (same MD5) hits cache even if the original QQ URL changes.
        # Stickers (sub_type=1) are also cached on first sight; subsequent
        # occurrences reuse the caption without another vision call.
        cached_caption = ""
        sticker_caption = ""
        if multimodal_inputs:
            for item in multimodal_inputs:
                if item.get("type") != "image":
                    continue
                path = str(item.get("value", ""))
                cache_key = self._image_cache_key(path)
                if cache_key and cache_key in self._image_caption_cache:
                    hit = self._image_caption_cache[cache_key]
                    if item.get("sub_type") == "1":
                        sticker_caption = hit
                        logger.debug("Sticker caption cache hit for %s", cache_key)
                    else:
                        cached_caption = hit
                        logger.debug("Image caption cache hit for %s", cache_key)
                        break

        # Build the list of images to actually send to the vision model.
        # Normal images are always sent unless cached.
        # Stickers are sent only on first sight (not yet cached).
        filtered_mm: list[dict[str, str]] = []
        if multimodal_inputs:
            for item in multimodal_inputs:
                if item.get("type") != "image":
                    continue
                is_sticker = item.get("sub_type") == "1"
                if is_sticker and sticker_caption:
                    # Already cached: skip vision
                    continue
                filtered_mm.append(item)

        # 多模态消息：如果存在图片，使用 vision model 并通过 messages 传递图片
        if filtered_mm:
            mm_messages = self._build_multimodal_messages(message, filtered_mm)
            request = GenerationRequest(
                model=self.model_name,
                system_prompt=prompt,
                messages=mm_messages,
                temperature=0.2,
                max_tokens=1024,
                purpose="cognition_analyze",
            )
        else:
            request = GenerationRequest(
                model=self.model_name,
                system_prompt=prompt,
                messages=[],
                temperature=0.2,
                max_tokens=512,
                purpose="cognition_analyze",
            )
        self._last_request = request

        # If all normal images are cached and no stickers need first-analysis,
        # skip the LLM call entirely.
        if cached_caption and not filtered_mm:
            # Use rule-based emotion since we skip LLM
            text_emotion = self._text_analysis(message)
            context_emotion = self._context_inference(current_user_id)
            group_emotion = self.group_cache.get(current_user_id)
            emotion = self._fuse_emotion(text_emotion, context_emotion, group_emotion)
            return {
                "emotion": emotion,
                "social_intent": SocialIntent.SOCIAL,
                "subtype": SocialSubtype.TOPIC_DISCUSSION,
                "confidence": 0.7,
                "urgency_score": 30.0,
                "relevance_score": 0.5,
                "directed_score": 0.3,
                "directed_reason": "cached_image_caption",
                "sarcasm_score": 0.0,
                "search_query": message or "",
                "image_caption": cached_caption,
                "plugin_intent": None,
                "plugin_slots": {},
            }

        if self.provider_async is None and self.brain is None:
            return None

        # 优先通过 Brain 统一调用（v1.2+），回退到直接 provider 调用
        if self.brain is not None:
            from sirius_pulse.core.brain import RawRequest

            raw = await self.brain.raw_call(
                RawRequest(
                    model=self.model_name,
                    system_prompt=prompt,
                    messages=request.messages,
                    temperature=request.temperature,
                    max_tokens=request.max_tokens,
                    timeout_seconds=request.timeout_seconds or 30.0,
                    purpose="cognition_analyze",
                )
            )
        elif hasattr(self.provider_async, "generate_async"):
            raw = await self.provider_async.generate_async(request)
        elif isinstance(self.provider_async, LLMProvider):
            raw = await asyncio.to_thread(self.provider_async.generate, request)
        else:
            return None

        try:
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0]
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0]
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to parse LLM cognition JSON: %s | raw=%r", exc, raw)
            # Fallback: try regex extraction for critical fields
            data = self._extract_json_fields(raw)
            if not data:
                return None

        try:
            # Parse emotion
            be_raw = data.get("basic_emotion", "neutral")
            basic_emotion = self._parse_basic_emotion(be_raw)

            emotion = EmotionState(
                valence=max(-1.0, min(1.0, float(data.get("valence", 0)))),
                arousal=max(0.0, min(1.0, float(data.get("arousal", 0.3)))),
                intensity=max(0.0, min(1.0, float(data.get("intensity", 0.5)))),
                confidence=0.85,
                basic_emotion=basic_emotion,
            )

            # Parse intent
            si_raw = data.get("social_intent", "social")
            social_intent = self._parse_social_intent(si_raw)

            subtype_str = data.get("intent_subtype", "topic_discussion")
            subtype = self._parse_subtype(subtype_str, social_intent)

            caption = data.get("image_caption", "")
            # Cache the caption for future reuse (keyed by content hash).
            # Cache ALL images that were sent to vision model, including stickers.
            if caption and filtered_mm:
                for item in filtered_mm:
                    if item.get("type") == "image":
                        path = str(item.get("value", ""))
                        cache_key = self._image_cache_key(path)
                        if cache_key:
                            self._image_caption_cache[cache_key] = caption
                            logger.debug("Cached image caption for %s", cache_key)

            return {
                "emotion": emotion,
                "social_intent": social_intent,
                "subtype": subtype,
                "confidence": float(data.get("confidence", 0.85)),
                "urgency_score": float(data.get("urgency_score", 0)),
                "relevance_score": float(data.get("relevance_score", 0.5)),
                "directed_score": float(data.get("directed_score", 0.0)),
                "directed_reason": data.get("directed_reason", ""),
                "sarcasm_score": float(data.get("sarcasm_score", 0.0)),
                "search_query": data.get("search_query", ""),
                "image_caption": caption,
                # Plugin 字段（v1.2+）
                "plugin_intent": data.get("plugin_intent"),
                "plugin_slots": data.get("plugin_slots", {}) if isinstance(data.get("plugin_slots"), dict) else {},
            }
        except (ValueError, KeyError) as exc:
            logger.warning("Failed to extract cognition fields: %s | raw=%r", exc, raw)
            return None

    @staticmethod
    def _extract_json_fields(raw: str) -> dict[str, Any] | None:
        """Best-effort field extraction from malformed JSON using regex.

        Handles common LLM output issues like unescaped quotes inside string
        values (e.g. search_query containing Chinese quotation marks).
        """
        import re

        fields: dict[str, Any] = {}

        # Extract string fields: "key": "value"
        for key in (
            "basic_emotion", "social_intent", "intent_subtype", "search_query",
        ):
            pattern = rf'"{key}"\s*:\s*"([^"]*)"'
            m = re.search(pattern, raw)
            if m:
                fields[key] = m.group(1)

        # Extract numeric fields
        for key in ("valence", "arousal", "intensity", "confidence",
                    "urgency_score", "relevance_score", "directed_score"):
            pattern = rf'"{key}"\s*:\s*([-\d.]+)'
            m = re.search(pattern, raw)
            if m:
                try:
                    fields[key] = float(m.group(1))
                except ValueError:
                    pass

        return fields if fields else None

    @staticmethod
    def _image_cache_key(path: str) -> str:
        """Extract content hash from local image cache path for stable cache keys.

        NapCatAdapter caches images as ``{md5_hash}{ext}`` under ``image_cache/``
        or ``sticker_cache/``. The same image always gets the same hash, so we
        use the hash as the cache key regardless of the original (possibly
        transient) QQ URL.
        """
        from pathlib import Path

        p = Path(path)
        # filename like "a1b2c3d4.jpg" -> stem "a1b2c3d4"
        stem = p.stem
        # Simple heuristic: 32-char hex string is likely an MD5 hash
        if len(stem) == 32 and all(c in "0123456789abcdef" for c in stem.lower()):
            return stem.lower()
        # Fallback: use the full path (for non-cached images or direct URLs)
        return path

    @staticmethod
    def _parse_basic_emotion(emotion_str: str) -> BasicEmotion | None:
        mapping = {
            "joy": BasicEmotion.JOY,
            "anger": BasicEmotion.ANGER,
            "sadness": BasicEmotion.SADNESS,
            "anxiety": BasicEmotion.ANXIETY,
            "loneliness": BasicEmotion.LONELINESS,
            "neutral": None,
        }
        return mapping.get(emotion_str.lower())

    @staticmethod
    def _parse_social_intent(intent_str: str) -> SocialIntent:
        mapping = {
            "help_seeking": SocialIntent.HELP_SEEKING,
            "emotional": SocialIntent.EMOTIONAL,
            "social": SocialIntent.SOCIAL,
            "silent": SocialIntent.SILENT,
            "plugin_command": SocialIntent.PLUGIN_COMMAND,  # v1.2+
        }
        return mapping.get(intent_str.lower(), SocialIntent.SOCIAL)

    @staticmethod
    def _parse_subtype(subtype_str: str, social_intent: SocialIntent) -> Any:
        """Parse subtype string into the correct Enum based on social_intent."""
        mapping: dict[str, Any] = {
            "tech_help": HelpSubtype.TECH_HELP,
            "info_query": HelpSubtype.INFO_QUERY,
            "venting": EmotionalSubtype.VENTING,
            "seeking_empathy": EmotionalSubtype.SEEKING_EMPATHY,
            "topic_discussion": SocialSubtype.TOPIC_DISCUSSION,
            "filler": SilentSubtype.FILLER,
        }
        subtype = mapping.get(subtype_str)
        if subtype is None:
            # Fallback based on social_intent
            if social_intent == SocialIntent.HELP_SEEKING:
                subtype = HelpSubtype.INFO_QUERY
            elif social_intent == SocialIntent.EMOTIONAL:
                subtype = EmotionalSubtype.SEEKING_EMPATHY
            elif social_intent == SocialIntent.SILENT:
                subtype = SilentSubtype.FILLER
            else:
                subtype = SocialSubtype.TOPIC_DISCUSSION
        return subtype

    # ------------------------------------------------------------------
    # Emotion text analysis (rule-based)
    # ------------------------------------------------------------------

    def _text_analysis(self, message: str) -> EmotionState:
        if not message:
            return EmotionState(valence=0.0, arousal=0.3, intensity=0.1, confidence=0.5)

        scores = []
        for word, score in self.lexicon.items():
            if word in message:
                scores.append(score)

        if not scores:
            return EmotionState(valence=0.0, arousal=0.3, intensity=0.1, confidence=0.5)

        avg = sum(scores) / len(scores)
        intensity = min(
            1.0, len(scores) / max(1, len(message)) * 3 + self._punctuation_intensity(message)
        )
        arousal = self._estimate_arousal(message, scores)
        return EmotionState(
            valence=max(-1.0, min(1.0, avg)),
            arousal=arousal,
            intensity=intensity,
            confidence=0.7 if len(scores) >= 2 else 0.5,
        )

    @staticmethod
    def _punctuation_intensity(message: str) -> float:
        intensity = 0.0
        intensity += min(0.3, (message.count("!") + message.count("！")) * 0.1)
        if message.count("?") + message.count("？") >= 3:
            intensity += 0.2
        if "..." in message or "…" in message:
            intensity += 0.1
        return min(0.5, intensity)

    @staticmethod
    def _estimate_arousal(message: str, sentiment_scores: list[float]) -> float:
        avg_abs = sum(abs(s) for s in sentiment_scores) / len(sentiment_scores)
        upper_ratio = sum(1 for c in message if c.isupper()) / max(1, len(message))
        length_factor = 1.0 - min(1.0, len(message) / 200.0)
        arousal = avg_abs * 0.5 + upper_ratio * 0.3 + length_factor * 0.2
        return max(0.0, min(1.0, arousal))

    # ------------------------------------------------------------------
    # Emotion context inference
    # ------------------------------------------------------------------

    def _context_inference(self, user_id: str) -> EmotionState | None:
        traj = self.trajectories.get(user_id, [])
        if len(traj) < 2:
            return None
        recent = [state for _, state in traj[-5:]]
        valence_trend = recent[-1].valence - recent[0].valence
        arousal_trend = recent[-1].arousal - recent[0].arousal
        return EmotionState(
            valence=max(-1.0, min(1.0, recent[-1].valence + valence_trend * 0.3)),
            arousal=max(0.0, min(1.0, recent[-1].arousal + arousal_trend * 0.3)),
            intensity=recent[-1].intensity,
            confidence=0.6,
        )

    def _update_trajectory(self, user_id: str, emotion: EmotionState) -> None:
        if user_id not in self.trajectories:
            self.trajectories[user_id] = []
        from sirius_pulse.core.utils import now_iso
        self.trajectories[user_id].append((now_iso(), emotion))
        if len(self.trajectories[user_id]) > 100:
            self.trajectories[user_id] = self.trajectories[user_id][-100:]

    @staticmethod
    def _fuse_emotion(
        text: EmotionState,
        context: EmotionState | None,
        group: EmotionState | None,
    ) -> EmotionState:
        w_text = 0.5
        w_context = 0.3 if context else 0.0
        w_group = 0.2 if group else 0.0
        total = w_text + w_context + w_group
        w_text /= total
        w_context = (w_context / total) if w_context else 0.0
        w_group = (w_group / total) if w_group else 0.0

        valence = text.valence * w_text
        arousal = text.arousal * w_text
        if context:
            valence += context.valence * w_context
            arousal += context.arousal * w_context
        if group:
            valence += group.valence * w_group
            arousal += group.arousal * w_group

        return EmotionState(
            valence=max(-1.0, min(1.0, valence)),
            arousal=max(0.0, min(1.0, arousal)),
            intensity=text.intensity,
            confidence=text.confidence,
        )

    # ------------------------------------------------------------------
    # Intent classification (rule-based)
    # ------------------------------------------------------------------

    def _detect_directed_at_ai(self, message: str) -> bool:
        """Check if message directly addresses the current AI by name or alias.

        Deprecated: use directed_score >= 0.6 instead.
        """
        if not self.ai_name:
            return False
        text = message.lower()
        names = [self.ai_name.lower()] + self.ai_aliases
        return any(name in text for name in names if name)

    def _detect_sarcasm_score(self, message: str) -> float:
        """Detect sarcasm / irony in message via heuristic patterns.

        Returns score in [0.0, 1.0].
        """
        text = message or ""
        text_lower = text.lower()
        if not text_lower:
            return 0.0

        indicators = 0.0

        # Positive word + negative punctuation/context
        positive_words = ["棒", "好", "厉害", "优秀", "完美", "真棒", "太好了", "赞", "佩服"]
        negative_markers = ["...", "。。。", "呵呵", "嗯", "哦", "切", "行吧", "好吧", "随你"]
        for pw in positive_words:
            if pw in text_lower:
                for nm in negative_markers:
                    if nm in text_lower:
                        indicators += 0.25
                        break
                break

        # Quotation emphasis (Chinese & English)
        if re.search(r'["""].*?["""]', text) or '"' in text:
            indicators += 0.15

        # Excessive laughter emoji/punctuation
        laugh_count = text_lower.count("哈哈") + text_lower.count("haha") + text.count("😂") + text.count("🤣")
        if laugh_count >= 3:
            indicators += 0.15

        # Common sarcasm patterns
        sarcasm_patterns = [
            r"真[的]?[是]?.*[啊呢]",
            r"太.*了[吧]",
            r"不愧是.*",
            r"厉害.*厉害",
            r"服了[你]?[了]?",
            r"(?:真是|确实).*(?:优秀|厉害|棒|好)",
        ]
        for pat in sarcasm_patterns:
            if re.search(pat, text_lower):
                indicators += 0.15
                break

        # Emoji-text valence mismatch (positive emoji in negative text)
        positive_emojis = ["😂", "🤣", "😊", "👍", "🎉", "😁"]
        negative_words = ["烦", "累", "崩溃", "无语", "恶心", "讨厌", "生气", "失望"]
        has_pos_emoji = any(e in text for e in positive_emojis)
        has_neg_word = any(w in text_lower for w in negative_words)
        if has_pos_emoji and has_neg_word:
            indicators += 0.2

        return min(1.0, indicators)

    def _compute_entitlement_score(
        self,
        message: str,
        social_intent: SocialIntent,
    ) -> float:
        """Compute how qualified the AI is to reply to this message.

        Based on persona expertise vs message topic alignment.
        Returns score in [0.0, 1.0].
        """
        text = message or ""
        text_lower = text.lower()
        if not text_lower:
            return 0.5

        base = 0.5

        # If persona has defined interests/traits, check overlap
        if self.persona:
            persona_keywords: set[str] = set()
            if getattr(self.persona, "interests", None):
                persona_keywords.update(self.persona.interests)
            if getattr(self.persona, "traits", None):
                persona_keywords.update(self.persona.traits)
            if getattr(self.persona, "personality_traits", None):
                persona_keywords.update(self.persona.personality_traits)
            if getattr(self.persona, "social_role", None):
                persona_keywords.add(self.persona.social_role)

            if persona_keywords:
                text_words = set(re.findall(r"[\u4e00-\u9fff]+", text)) | set(re.findall(r"[a-zA-Z]+", text_lower))
                pk_lower = {k.lower() for k in persona_keywords}
                overlap = len(text_words & pk_lower)
                # Strong overlap → high entitlement
                if overlap >= 2:
                    base = 0.8
                elif overlap == 1:
                    base = 0.65
                else:
                    base = 0.4

        # Help-seeking intents that don't match persona → lower entitlement
        if social_intent == SocialIntent.HELP_SEEKING and base < 0.5:
            base -= 0.1

        # Emotional intents → high entitlement (empathy is universal)
        if social_intent == SocialIntent.EMOTIONAL:
            base = max(base, 0.7)

        return max(0.0, min(1.0, base))

    def _compute_directed_scores(
        self,
        message: str,
        user_id: str,
        context_messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, float]:
        """Compute 12-dimensional directedness scores via rule engine.

        All scores are in [0.0, 1.0]. Dimensions where the current architecture
        cannot obtain reliable signals return 0.0 as placeholders.
        """
        text = message or ""
        text_lower = text.lower()
        scores: dict[str, float] = {}

        # --- Layer 1: Structural ---
        # mention_score: @name / @alias exact match
        mention = 0.0
        if self.ai_name:
            for name in [self.ai_name] + self.ai_aliases:
                if not name:
                    continue
                pattern = rf"@\s*{re.escape(name)}"
                if re.search(pattern, text_lower):
                    mention = 1.0
                    break
        scores["mention_score"] = mention

        # other_mention_score: message clearly addresses someone other than AI
        other_mention = 0.0
        if mention < 0.5 and re.search(r"@\S+", text):
            other_mention = 1.0
        scores["other_mention_score"] = other_mention

        # at_all_score: @all / @everyone / @所有人
        at_all = 0.0
        if re.search(r"@\s*(all|everyone|所有人|全体)", text_lower):
            at_all = 0.3
        scores["at_all_score"] = at_all

        # reference_score: text-level quote/reply markers (platform metadata unavailable)
        ref = 0.0
        if context_messages:
            # Check if message starts with a quote pattern referencing recent AI message
            ai_contents = [
                m.get("content", "")[:40]
                for m in context_messages[-3:]
                if m.get("user_id") == "assistant"
            ]
            for ai_txt in ai_contents:
                if ai_txt and ai_txt in text:
                    ref = 0.8
                    break
            # Standard quote markers (require leading > to avoid matching normal short messages)
            if ref == 0.0 and re.search(r"^>[>\s]*.{3,200}$", text, re.MULTILINE):
                ref = 0.4
        scores["reference_score"] = ref

        # --- Layer 2: Linguistic ---
        # name_match_score: nickname/name in text (word-boundary aware)
        name_match = 0.0
        if self.ai_name:
            for name in [self.ai_name] + self.ai_aliases:
                if not name:
                    continue
                if re.search(rf"\\b{re.escape(name.lower())}\\b", text_lower):
                    name_match = max(name_match, 1.0)
                elif name.lower() in text_lower:
                    name_match = max(name_match, 0.6)
        scores["name_match_score"] = name_match

        # second_person_score: density of "你/您"
        second_person_words = ["你", "您", "you"]
        sp_count = sum(text_lower.count(w) for w in second_person_words)
        scores["second_person_score"] = min(1.0, sp_count * 0.25)

        # question_score: interrogative patterns
        question_markers = [
            r"吗[？?]", r"呢[？?]", r"什么[？?]", r"怎么[？?]", r"为什么[？?]",
            r"如何[？?]", r"哪里[？?]", r"谁[？?]", r"多少[？?]", r"能不能",
            r"可以吗", r"行不行", r"好不好", r"怎么样", r"如何看待",
        ]
        q_count = sum(1 for p in question_markers if re.search(p, text_lower))
        scores["question_score"] = min(1.0, q_count * 0.3 + (0.2 if "?" in text or "？" in text else 0.0))

        # imperative_score: imperative/request patterns
        imperative_markers = [
            r"帮我", r"给我", r"替我", r"为?我", r"翻译[一下]?",
            r"想[要个]?[ ]?.*[吧吗]?", r"来[ ]?.*[吧吗]?", r"请[ ]?.*",
            r"试试", r"看看", r"听听", r"说说",
        ]
        i_count = sum(1 for p in imperative_markers if re.search(p, text_lower))
        scores["imperative_score"] = min(1.0, i_count * 0.3)

        # --- Layer 3: Semantic ---
        # topic_relevance_score: keyword overlap with AI persona interests
        topic_rel = 0.0
        if self.persona:
            ai_keywords: set[str] = set()
            if getattr(self.persona, "interests", None):
                ai_keywords.update(self.persona.interests)
            if getattr(self.persona, "traits", None):
                ai_keywords.update(self.persona.traits)
            if getattr(self.persona, "name", None):
                ai_keywords.add(self.persona.name)
            if ai_keywords:
                text_words = set(re.findall(r"[\u4e00-\u9fff]+", text)) | set(re.findall(r"[a-zA-Z]+", text_lower))
                ai_words = {k.lower() for k in ai_keywords}
                overlap = len(text_words & ai_words)
                topic_rel = min(1.0, overlap / max(1, len(ai_words)) * 3)
        scores["topic_relevance_score"] = topic_rel

        # emotional_disclosure_score: emotional expression seeking support
        emotional_markers = [
            "难过", "伤心", "痛苦", "累", "烦", "郁闷", "崩溃", "绝望",
            "开心", "高兴", "兴奋", "激动", "感动", "欣慰",
            "孤独", "寂寞", "害怕", "担心", "焦虑", "紧张",
            "呜呜", "泪目", "扎心", "难受", "emo",
        ]
        ed_count = sum(1 for w in emotional_markers if w in text_lower)
        scores["emotional_disclosure_score"] = min(1.0, ed_count * 0.25 + (0.15 if any(w in text_lower for w in ["感觉", "觉得", "心情"]) else 0.0))

        # attention_seeking_score: attention-seeking phrases
        attention_markers = [
            "有人吗", "在吗", "在不在", "理我", "理一下", "看看我",
            "回我", "回复我", "说话", "说句话", "吱个声",
        ]
        at_count = sum(1 for w in attention_markers if w in text_lower)
        scores["attention_seeking_score"] = min(1.0, at_count * 0.4)

        # --- Layer 4: Contextual ---
        # recency_score: recent interaction with AI in context
        recency = 0.0
        if context_messages:
            ai_msgs = [m for m in context_messages if m.get("user_id") == "assistant"]
            if ai_msgs:
                # More recent AI messages → higher recency
                recency = min(1.0, len(ai_msgs) * 0.3)
        scores["recency_score"] = recency

        # turn_taking_score: alternating pattern AI→user→AI→user
        turn = 0.0
        if context_messages and len(context_messages) >= 2:
            recent = context_messages[-4:]
            uids = [m.get("user_id", "") for m in recent]
            # Check if last non-current message is from AI
            if len(uids) >= 2 and uids[-2] == "assistant":
                turn = 0.6
            # Check for strong alternation pattern
            if len(set(uids)) == 2 and "assistant" in uids:
                turn = max(turn, 0.8)
        scores["turn_taking_score"] = turn

        return scores

    @staticmethod
    def _synthesize_directed_score(
        rule_scores: dict[str, float],
        llm_score: float | None,
        llm_confidence: float = 0.8,
    ) -> float:
        """Synthesize final directed_score from rule-based 12-dim + LLM semantic.

        Formula:
            - mention_score >= 0.5: explicit @AI → strong LLM trust (confidence-aware)
            - structural < 0.3: implicit directedness → conservative LLM trust
            - otherwise: weak signals → LLM as auxiliary, capped +0.15
        """
        mention_score = rule_scores.get("mention_score", 0.0)
        other_mention = rule_scores.get("other_mention_score", 0.0)

        # @others guard: message explicitly addresses someone other than AI
        # → force low directed_score regardless of LLM prediction
        if other_mention >= 0.5 and mention_score < 0.5:
            return min(0.3, max(
                0.0,
                rule_scores.get("recency_score", 0.0) * 0.15,
            ))

        name_match = rule_scores.get("name_match_score", 0.0)
        second_person = rule_scores.get("second_person_score", 0.0)
        question = rule_scores.get("question_score", 0.0)
        imperative = rule_scores.get("imperative_score", 0.0)
        turn_taking = rule_scores.get("turn_taking_score", 0.0)

        # Strong linguistic signals: name match or imperative patterns
        strong_linguistic = max(name_match, imperative)

        # Turn-taking + second person: user is continuing conversation with AI
        # e.g. "那你推荐一下" after AI just replied → strong directed signal
        if turn_taking >= 0.5 and second_person >= 0.2:
            strong_linguistic = max(strong_linguistic, turn_taking * 0.7)
        # Weak linguistic signals: "你" or question alone (not sufficient without name)
        weak_linguistic = max(second_person, question)

        structural = max(
            mention_score,
            rule_scores.get("reference_score", 0.0),
            rule_scores.get("at_all_score", 0.0) * 0.5,
        )
        # Only signals that convey direct addressee intent count here.
        # topic_relevance is excluded: discussing AI-related topics ≠ addressing the AI.
        # attention_seeking ("有人吗", "在吗") implies wanting a response but not
        # necessarily targeting the AI specifically, so it contributes minimally.
        semantic = max(
            rule_scores.get("emotional_disclosure_score", 0.0),
            rule_scores.get("attention_seeking_score", 0.0) * 0.3,
        )
        contextual = max(
            rule_scores.get("recency_score", 0.0),
            rule_scores.get("turn_taking_score", 0.0),
        ) * 0.15

        base = max(structural, strong_linguistic)

        if base >= 0.5:
            score = min(1.0, base + semantic * 0.15 + contextual)
        elif semantic >= 0.6:
            score = min(0.65, 0.35 + semantic * 0.3 + contextual)
        else:
            score = base + contextual

        # LLM semantic override with confidence-aware blending
        if llm_score is not None and llm_score > 0.0:
            if mention_score >= 0.5 and llm_score >= 0.5:
                # Explicit @AI: strong trust, but confidence modulates coefficient
                coef = 0.6 + 0.3 * llm_confidence
                score = max(score, min(0.92, llm_score * coef))
            elif structural < 0.3 and llm_score >= 0.6:
                # Implicit directedness (e.g. "你觉得呢"): conservative trust
                # Zero signal guard: when no rule signals at all, LLM alone is
                # unreliable (e.g. "yuki有没有作业" is about another person, not AI)
                if base < 0.1 and semantic < 0.3:
                    blend = score * 0.7 + llm_score * 0.3 * llm_confidence
                    score = max(score, min(score + 0.15, blend))
                else:
                    coef = 0.5 + 0.3 * llm_confidence
                    score = max(score, min(0.75, llm_score * coef))
            else:
                # Weak signals: LLM is auxiliary, capped at +0.15 above rule score
                blend = score * 0.7 + llm_score * 0.3 * llm_confidence
                score = max(score, min(score + 0.15, blend))

        return max(0.0, min(1.0, score))

    def _classify_intent(
        self,
        message: str,
        context_messages: list[dict[str, Any]] | None = None,
        *,
        caller_is_developer: bool = False,
    ) -> tuple[SocialIntent, Any, float]:
        """分类消息的社交意图。

        返回值: (SocialIntent, subtype, confidence)

        v1.2+: 新增 Plugin 命令匹配层，在传统规则之前优先检查。
        """
        text = message.lower()
        has_context = bool(context_messages)

        # === ✅ Plugin 命令匹配层（最高优先级，v1.2+）===
        plugin_match = self._match_plugin_command(message, caller_is_developer=caller_is_developer)
        if plugin_match is not None:
            return SocialIntent.PLUGIN_COMMAND, plugin_match, 0.95

        # Help seeking
        help_score = 0
        for pat in _HELP_PATTERNS:
            if re.search(pat, text):
                help_score += 1
        if "?" in message or "？" in message:
            help_score += 1

        # Emotional
        emotional_score = sum(1 for w in _EMOTIONAL_INDICATORS if w in text)

        # Social
        social_score = sum(1 for w in _SOCIAL_INDICATORS if w in text)

        # Context-aware: short messages that look like filler may actually be
        # follow-ups to previous messages (e.g. "为什么？", "那怎么办", "懂了")
        if has_context and len(message) <= 8:
            if any(p in message for p in _CONTEXT_DEPENDENT_PATTERNS):
                # This is likely a conversational follow-up, not filler
                if help_score >= 1:
                    return SocialIntent.HELP_SEEKING, HelpSubtype.INFO_QUERY, 0.75
                return SocialIntent.SOCIAL, SocialSubtype.TOPIC_DISCUSSION, 0.65

        # Silent indicators (filler)
        if len(message) <= 4 or message in {"哈哈", "确实", "+1", "嗯", "哦"}:
            return SocialIntent.SILENT, SilentSubtype.FILLER, 0.9

        if help_score >= 1 and help_score >= emotional_score and help_score >= social_score:
            subtype = (
                HelpSubtype.TECH_HELP
                if any(k in text for k in {"报错", "错误", "exception", "bug"})
                else HelpSubtype.INFO_QUERY
            )
            return SocialIntent.HELP_SEEKING, subtype, min(0.95, 0.6 + help_score * 0.1)

        if emotional_score >= 2 and emotional_score >= help_score and emotional_score >= social_score:
            subtype = (
                EmotionalSubtype.VENTING
                if any(k in text for k in {"烦", "累", "难受", "崩溃"})
                else EmotionalSubtype.SEEKING_EMPATHY
            )
            return SocialIntent.EMOTIONAL, subtype, min(0.9, 0.5 + emotional_score * 0.1)

        if social_score >= 1:
            subtype = SocialSubtype.TOPIC_DISCUSSION
            return SocialIntent.SOCIAL, subtype, min(0.8, 0.5 + social_score * 0.1)

        # Default: social or silent based on length
        if len(message) <= 10:
            return SocialIntent.SILENT, SilentSubtype.FILLER, 0.6
        return SocialIntent.SOCIAL, SocialSubtype.TOPIC_DISCUSSION, 0.5

    # ------------------------------------------------------------------
    # Intent scoring
    # ------------------------------------------------------------------

    def _calculate_urgency(
        self,
        message: str,
        user_id: str,
        group_id: str | None,
        emotion: EmotionState | None,
        context_messages: list[dict[str, Any]] | None = None,
    ) -> float:
        text = message.lower()
        score = 0.0

        # Language markers (0-25)
        if any(kw in text for kw in _URGENCY_KEYWORDS["high"]):
            score += 25.0
        elif any(kw in text for kw in _URGENCY_KEYWORDS["medium"]):
            score += 12.0
        elif any(kw in text for kw in _URGENCY_KEYWORDS["low"]):
            score += 5.0

        # Time constraint (0-15)
        if any(kw in text for kw in {"明天", "今天", "马上", "立刻", "今晚", " asap"}):
            score += 15.0

        # Emotional intensity (0-18)
        if emotion:
            if emotion.valence < -0.5 and emotion.arousal > 0.7:
                score += 18.0
            elif emotion.intensity > 0.7:
                score += 12.0

        # Context-aware: follow-up questions in short messages carry implicit urgency
        if context_messages and len(message) <= 8:
            if any(p in message for p in _CONTEXT_DEPENDENT_PATTERNS):
                score += 20.0

        return max(0.0, min(100.0, score))

    def _calculate_relevance(
        self,
        message: str,
        social_intent: SocialIntent,
        user_id: str,
        group_id: str | None,
    ) -> float:
        # Lower base relevance to reduce overall reply frequency.
        # Only help-seeking and emotional intents get a modest boost.
        role_match = 0.8 if social_intent in (SocialIntent.HELP_SEEKING, SocialIntent.EMOTIONAL) else 0.1
        return min(1.0, 0.22 + role_match * 0.4)

    @staticmethod
    def _intent_type_from_social(social_intent: SocialIntent, message: str) -> str:
        if social_intent == SocialIntent.HELP_SEEKING:
            return "question" if "?" in message or "？" in message else "request"
        if social_intent in (SocialIntent.EMOTIONAL, SocialIntent.SOCIAL):
            return "chat"
        if social_intent == SocialIntent.PLUGIN_COMMAND:
            return "command"  # v1.2+
        return "chat"

    # ------------------------------------------------------------------
    # Plugin 命令匹配（v1.2+）
    # ------------------------------------------------------------------

    def _match_plugin_command(
        self, message: str, *, caller_is_developer: bool = False
    ) -> "PluginMatchInfo | None":
        """尝试将用户消息匹配到已注册的 Plugin 命令。

        匹配优先级（从高到低）：
            1. 精确指令匹配（/天气, #roll）→ confidence=1.0
            2. 模板/关键词匹配（"查查无锡的天气"）→ confidence=0.85

        Args:
            message: 用户输入的原始文本
            caller_is_developer: 调用者是否为开发者。非开发者不匹配 developer_only 插件。

        Returns:
            PluginMatchInfo 或 None
        """
        if self.plugin_registry is None:
            return None

        try:
            match_result = self.plugin_registry.match_message(message)
            if match_result is not None:
                # 构建 PluginMatchInfo 供上游使用
                plugin_name = match_result.plugin_name
                definition = self.plugin_registry.get(plugin_name)
                if definition is None:
                    return None
                # 非开发者不匹配 developer_only 插件
                if definition.permissions.developer_only and not caller_is_developer:
                    return None
                render_mode = definition.render.mode if definition else "direct"

                # 尝试参数解析（如果有 LexedCommand）
                slots: dict[str, Any] = {}
                if match_result.lexed is not None and definition is not None:
                    from sirius_pulse.plugins.lexer import CommandParser
                    parser = CommandParser()
                    ast = parser.parse(match_result.lexed, definition)
                    # 将 kwargs 中的值提取出来作为 slots
                    for name, node in ast.kwargs.items():
                        slots[name] = node.value
                    # 也提取位置参数
                    for i, node in enumerate(ast.args):
                        slots[f"_{i}"] = node.value

                from dataclasses import dataclass

                @dataclass
                class PluginMatchInfo:
                    plugin_name: str
                    confidence: float
                    render_mode: str
                    slots: dict[str, Any]

                return PluginMatchInfo(
                    plugin_name=plugin_name,
                    confidence=match_result.confidence,
                    render_mode=render_mode,
                    slots=slots,
                )
        except Exception as exc:
            logger.debug("Plugin 命令匹配异常: %s", exc)

        return None

    def _get_plugin_descriptions_for_prompt(self, caller_is_developer: bool = False) -> str:
        """生成 Plugin 指令描述文本（用于 LLM Cognition Prompt）。

        仅输出极简描述，避免 LLM 被详细描述诱导误判 plugin_command。
        """
        if self.plugin_registry is None:
            return ""
        try:
            descriptions = self.plugin_registry.get_plugin_descriptions(caller_is_developer)
            if not descriptions:
                return ""
            return (
                "\n【可用插件指令 — 仅当用户消息明确请求以下功能时才标记为 plugin_command，"
                "日常聊天中提到相关词语不等于插件请求】\n"
                f"{descriptions}\n"
            )
        except Exception:
            return ""

    def _get_plugin_slots_hint(self, caller_is_developer: bool = False) -> str:
        """生成插件参数槽位提示（帮助 LLM 知道提取哪些字段名和期望类型）。"""
        if self.plugin_registry is None:
            return "（无插件）"
        try:
            parts: list[str] = []
            for name in self.plugin_registry.plugin_names:
                definition = self.plugin_registry.get(name)
                if definition is None:
                    continue
                if definition.permissions.developer_only and not caller_is_developer:
                    continue
                # hidden_from_intent 插件对 LLM 认知隐藏
                if definition.permissions.hidden_from_intent:
                    continue
                # 构建参数提示
                param_hints: list[str] = []
                if definition.natural_language and definition.natural_language.slots:
                    for slot_name, slot_info in definition.natural_language.slots.items():
                        slot_type = slot_info.get("type", "str")
                        slot_desc = slot_info.get("description", "")
                        slot_default = slot_info.get("default")
                        hint = f"{slot_name}={slot_type}"
                        if slot_desc:
                            hint += f"({slot_desc})"
                        if slot_default is not None:
                            hint += f",默认={slot_default}"
                        param_hints.append(hint)
                elif definition.parameters:
                    for param in definition.parameters:
                        hint = f"{param.name}={param.type}"
                        if param.description:
                            hint += f"({param.description})"
                        if param.default is not None:
                            hint += f",默认={param.default}"
                        param_hints.append(hint)
                if param_hints:
                    parts.append(f"{name}(参数: {', '.join(param_hints)})")
                else:
                    parts.append(name)
            if not parts:
                return "（无插件）"
            return f"可用插件ID与参数：{'；'.join(parts)}。请从消息中提取对应参数值，数值类型请传数字（非空字符串）。"
        except Exception:
            return "（无插件）"

    def _validate_plugin_intent(
        self, plugin_intent: str, *, caller_is_developer: bool = False
    ) -> str | None:
        """校验 LLM 返回的 plugin_intent 是否为已注册且调用者可用的插件。

        Returns:
            有效的 plugin_intent 或 None（无效/仅开发者可用时回退）
        """
        if not plugin_intent:
            return None
        if self.plugin_registry is None:
            return None
        definition = self.plugin_registry.get(plugin_intent)
        if definition is None:
            logger.debug("LLM 返回了无效的 plugin_intent: %s，已降级", plugin_intent)
            return None
        # 非开发者不能使用 developer_only 插件
        if definition.permissions.developer_only and not caller_is_developer:
            logger.debug("LLM 返回了仅开发者可用的 plugin_intent: %s，已降级", plugin_intent)
            return None
        # hidden_from_intent 插件不被 LLM 意图识别感知
        if definition.permissions.hidden_from_intent:
            logger.debug("LLM 返回了 hidden_from_intent plugin_intent: %s，已降级", plugin_intent)
            return None
        return plugin_intent


