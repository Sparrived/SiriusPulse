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
import re
from dataclasses import dataclass
from typing import Any

from sirius_pulse.models.emotion import BasicEmotion, EmotionState, EmpathyStrategy

logger = logging.getLogger(__name__)
from sirius_pulse.models.intent_v3 import (
    EmotionalSubtype,
    HelpSubtype,
    SilentSubtype,
    SocialIntent,
    SocialSubtype,
)

logger = logging.getLogger(__name__)


_LENGTH_BIAS_KEYWORDS = (
    "简短",
    "短句",
    "短回复",
    "详细",
    "细说",
    "长篇",
    "长句",
    "字数",
    "篇幅",
    "回复长度",
    "多说",
    "少说",
    "话多",
    "话少",
    "少解释",
    "多解释",
    "concise",
    "detailed",
    "long-form",
)


def _drop_length_biased_text(text: str | None) -> str:
    if not text:
        return ""
    normalized = text.strip()
    lowered = normalized.lower()
    if any(keyword in lowered for keyword in _LENGTH_BIAS_KEYWORDS):
        return ""
    return normalized


# ═══════════════════════════════════════════════════════════════════════
# 关键词提取工具（含中文二元组 Bigram）
# ═══════════════════════════════════════════════════════════════════════


def extract_keywords(text: str) -> set[str]:
    """提取文本中的关键词，含中文单字、英文单词、中文二元组（bigram）与英文二元组。

    中文二元组示例："人工智能" → {"人", "工", "智", "能", "人工", "工智", "智能"}
    英文二元组示例："neural network" → {"neural", "network", "neural network"}

    覆盖原字符级 split 的粒度缺陷，将"灵魂提取器"作为完整短语加入匹配集。
    """
    text = text.lower().strip()
    if not text:
        return set()
    keywords: set[str] = set()
    # 中文字符连续块
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]+", text)
    # 英文单词
    english_words = re.findall(r"[a-zA-Z]+", text)
    # 单字/单词级
    for chunk in chinese_chunks:
        for char in chunk:
            keywords.add(char)
    keywords.update(english_words)
    # 中文二元组 bigram：连续两个字符
    for chunk in chinese_chunks:
        if len(chunk) >= 2:
            for i in range(len(chunk) - 1):
                keywords.add(chunk[i : i + 2])
    # 英文二元组：相邻单词
    if len(english_words) >= 2:
        for i in range(len(english_words) - 1):
            keywords.add(f"{english_words[i]} {english_words[i + 1]}")
    return keywords


# ═══════════════════════════════════════════════════════════════════════
# 辅助数据类
# ═══════════════════════════════════════════════════════════════════════


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
    "你觉得",
    "你认为",
    "你怎么看",
    "你的看法",
    "你喜欢",
    "你觉得呢",
    "你觉得怎么样",
    "你的意见",
    "你觉得如何",
    "你更喜欢",
    "你最",
    "你讨厌",
    "你不喜欢",
    "你觉得好",
    "你怎么看",
)

# 需要上下文才能正确理解的短消息模式 —— 单独看像 filler，但有上下文时应视为对话延续
_CONTEXT_DEPENDENT_PATTERNS: tuple[str, ...] = (
    "为什么",
    "怎么回事",
    "真的吗",
    "那怎么办",
    "怎么办呢",
    "然后呢",
    "后来呢",
    "什么意思",
    "怎么说",
    "不会吧",
    "这样啊",
    "原来如此",
    "懂了",
    "这样吗",
    "那行",
    "好吧",
    "哦",
    "嗯嗯",
    "对对",
    "确实",
    "可以",
    "好的",
    "行吧",
)

class CognitionAnalyzer:
    """纯规则认知分析器。

    所有分析方法均为纯规则计算，不涉及 LLM 调用。
    包含情绪分析、意图分类、指向性评分、社交信号检测。
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

    # ------------------------------------------------------------------
    # 纯规则计算（无 LLM 调用）
    # ------------------------------------------------------------------

    def compute_signal(
        self,
        message: str,
        user_id: str,
        group_id: str | None = None,
        context_messages: list[dict[str, Any]] | None = None,
        *,
        sender_type: str = "human",
        caller_is_developer: bool = False,
        group_aliases: dict[str, str] | None = None,
        rhythm: Any | None = None,
    ) -> "SignalAnalysis":
        """纯规则计算信号分析，不调用 LLM。

        Args:
            message: 消息文本
            user_id: 发言者 user_id
            group_id: 群组 ID
            context_messages: 最近消息上下文
            sender_type: "human" 或 "other_ai"
            caller_is_developer: 是否为开发者
            group_aliases: 群组别名映射
            rhythm: RhythmAnalyzer.analyze() 的结果（可选，外部传入避免重复计算）

        Returns:
            SignalAnalysis 实例
        """
        from sirius_pulse.models.signal import SignalAnalysis

        # 1. 规则情绪分析
        text_emotion = self._text_analysis(message)
        context_emotion = self._context_inference(user_id)
        group_emotion = self.group_cache.get(group_id) if group_id else None
        emotion = self._fuse_emotion(text_emotion, context_emotion, group_emotion)
        self._update_trajectory(user_id, emotion)

        # 2. 规则意图分类
        social_intent, subtype, _ = self._classify_intent(
            message, context_messages, caller_is_developer=caller_is_developer
        )
        social_intent_str = social_intent.value if hasattr(social_intent, "value") else str(social_intent)

        # 3. 12 维指向性评分
        directed_scores = self._compute_directed_scores(message, user_id, context_messages)

        # 4. 合成 directed_score（纯规则，无 LLM 混合）
        directed_score = self._synthesize_directed_score(directed_scores, None, 0.0)
        sarcasm_score = self._detect_sarcasm_score(message)
        if sarcasm_score >= 0.4:
            directed_score = min(1.0, directed_score + sarcasm_score * 0.15)
        if sender_type == "other_ai":
            directed_score = min(directed_score, directed_score * 0.5 + 0.1)
        is_mentioned = directed_score >= 0.6

        # 5. 指向时提升意图
        if is_mentioned:
            if social_intent == SocialIntent.SILENT:
                social_intent = SocialIntent.SOCIAL
                social_intent_str = "social"

        # 6. 紧迫度和相关性
        urgency = self._calculate_urgency(message, user_id, group_id, emotion, context_messages)
        relevance = self._calculate_relevance(message, social_intent, user_id, group_id)
        if is_mentioned:
            is_question = "?" in message or "？" in message
            is_subjective = any(kw in message for kw in _SUBJECTIVE_KEYWORDS)
            if is_question or is_subjective:
                urgency = max(urgency, 80.0)
                relevance = max(relevance, 0.75)
            else:
                urgency = max(urgency, 70.0)
                relevance = max(relevance, 0.65)

        # 7. 社交信号
        entitlement_score = self._compute_entitlement_score(message, social_intent)

        # 8. 共情策略
        empathy = self.select_empathy_strategy(emotion, user_id)

        # 9. 节奏（外部传入或默认值）
        if rhythm is not None:
            heat_level = rhythm.heat_level
            pace = rhythm.pace
            burst_detected = rhythm.burst_detected
            turn_gap_readiness = rhythm.turn_gap_readiness
        else:
            heat_level = "warm"
            pace = "steady"
            burst_detected = False
            turn_gap_readiness = 0.0

        return SignalAnalysis(
            emotion=emotion,
            directed_score=directed_score,
            is_mentioned=is_mentioned,
            is_question="?" in message or "？" in message,
            is_imperative=directed_scores.get("imperative_score", 0.0) >= 0.5,
            urgency_score=urgency,
            relevance_score=relevance,
            social_intent=social_intent_str,
            sarcasm_score=sarcasm_score,
            entitlement_score=entitlement_score,
            heat_level=heat_level,
            pace=pace,
            burst_detected=burst_detected,
            turn_gap_readiness=turn_gap_readiness,
            search_query=message,
            empathy=empathy,
        )

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
        laugh_count = (
            text_lower.count("哈哈")
            + text_lower.count("haha")
            + text.count("😂")
            + text.count("🤣")
        )
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
                text_words = set(re.findall(r"[\u4e00-\u9fff]+", text)) | set(
                    re.findall(r"[a-zA-Z]+", text_lower)
                )
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
        # 当 AI 名字已在文本中出现时，不算作 @别人（如 "月白，@我一下"）
        other_mention = 0.0
        if mention < 0.5 and re.search(r"@\S+", text):
            name_in_text = False
            if self.ai_name:
                for name in [self.ai_name] + self.ai_aliases:
                    if name and name.lower() in text_lower:
                        name_in_text = True
                        break
            if not name_in_text:
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
            r"吗[？?]",
            r"呢[？?]",
            r"什么[？?]",
            r"怎么[？?]",
            r"为什么[？?]",
            r"如何[？?]",
            r"哪里[？?]",
            r"谁[？?]",
            r"多少[？?]",
            r"能不能",
            r"可以吗",
            r"行不行",
            r"好不好",
            r"怎么样",
            r"如何看待",
        ]
        q_count = sum(1 for p in question_markers if re.search(p, text_lower))
        scores["question_score"] = min(
            1.0, q_count * 0.3 + (0.2 if "?" in text or "？" in text else 0.0)
        )

        # imperative_score: imperative/request patterns
        imperative_markers = [
            r"帮我",
            r"给我",
            r"替我",
            r"为?我",
            r"翻译[一下]?",
            r"想[要个]?[ ]?.*[吧吗]?",
            r"来[ ]?.*[吧吗]?",
            r"请[ ]?.*",
            r"试试",
            r"看看",
            r"听听",
            r"说说",
        ]
        i_count = sum(1 for p in imperative_markers if re.search(p, text_lower))
        scores["imperative_score"] = min(1.0, i_count * 0.3)

        # --- Layer 3: Semantic ---
        # topic_relevance_score: keyword overlap with AI persona interests
        # v1.3+: 使用 bigram 增强的关键词提取，提升短语级匹配精度
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
                text_words = extract_keywords(text)
                ai_words = {k.lower() for k in ai_keywords}
                overlap = len(text_words & ai_words)
                topic_rel = min(1.0, overlap / max(1, len(ai_words)) * 3)
        scores["topic_relevance_score"] = topic_rel

        # emotional_disclosure_score: emotional expression seeking support
        emotional_markers = [
            "难过",
            "伤心",
            "痛苦",
            "累",
            "烦",
            "郁闷",
            "崩溃",
            "绝望",
            "开心",
            "高兴",
            "兴奋",
            "激动",
            "感动",
            "欣慰",
            "孤独",
            "寂寞",
            "害怕",
            "担心",
            "焦虑",
            "紧张",
            "呜呜",
            "泪目",
            "扎心",
            "难受",
            "emo",
        ]
        ed_count = sum(1 for w in emotional_markers if w in text_lower)
        scores["emotional_disclosure_score"] = min(
            1.0,
            ed_count * 0.25
            + (0.15 if any(w in text_lower for w in ["感觉", "觉得", "心情"]) else 0.0),
        )

        # attention_seeking_score: attention-seeking phrases
        attention_markers = [
            "有人吗",
            "在吗",
            "在不在",
            "理我",
            "理一下",
            "看看我",
            "回我",
            "回复我",
            "说话",
            "说句话",
            "吱个声",
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
            return min(
                0.3,
                max(
                    0.0,
                    rule_scores.get("recency_score", 0.0) * 0.15,
                ),
            )

        name_match = rule_scores.get("name_match_score", 0.0)
        second_person = rule_scores.get("second_person_score", 0.0)
        imperative = rule_scores.get("imperative_score", 0.0)
        turn_taking = rule_scores.get("turn_taking_score", 0.0)

        # Strong linguistic signals: name match or imperative patterns
        strong_linguistic = max(name_match, imperative)

        # Turn-taking + second person: user is continuing conversation with AI
        # e.g. "那你推荐一下" after AI just replied → strong directed signal
        if turn_taking >= 0.5 and second_person >= 0.2:
            strong_linguistic = max(strong_linguistic, turn_taking * 0.7)
        # Weak linguistic signals: "你" or question alone (not sufficient without name)

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
        contextual = (
            max(
                rule_scores.get("recency_score", 0.0),
                rule_scores.get("turn_taking_score", 0.0),
            )
            * 0.15
        )

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

        if (
            emotional_score >= 2
            and emotional_score >= help_score
            and emotional_score >= social_score
        ):
            subtype = (
                EmotionalSubtype.VENTING  # type: ignore[assignment]
                if any(k in text for k in {"烦", "累", "难受", "崩溃"})
                else EmotionalSubtype.SEEKING_EMPATHY
            )
            return SocialIntent.EMOTIONAL, subtype, min(0.9, 0.5 + emotional_score * 0.1)

        if social_score >= 1:
            subtype = SocialSubtype.TOPIC_DISCUSSION  # type: ignore[assignment]
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
        role_match = (
            0.8 if social_intent in (SocialIntent.HELP_SEEKING, SocialIntent.EMOTIONAL) else 0.1
        )
        return min(1.0, 0.22 + role_match * 0.4)

    @staticmethod
    def _intent_type_from_social(social_intent: SocialIntent, message: str) -> str:
        if social_intent == SocialIntent.HELP_SEEKING:
            return "question" if "?" in message or "？" in message else "request"
        if social_intent in (SocialIntent.EMOTIONAL, SocialIntent.SOCIAL):
            return "chat"
        return "chat"
