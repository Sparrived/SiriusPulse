"""PromptFactory — 统一 Prompt 构建中心。

无状态工具类，接管所有发送给 LLM 的 prompt 字符串拼装。
各模块只需调用 PromptFactory 的静态方法，不再自行拼接 section 标签和格式化文本。

职责边界：
    - section 标签常量定义
    - 人格 prompt 构建（从 PersonaProfile 字段生成）
    - 响应组装（immediate / delayed）
    - 消息渲染（表情、图片、聊天记录、摘要）
    - 技能结果格式化
    - 提醒等辅助 prompt
"""

from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sirius_pulse.core.constants import RESPONSE_MAX_TOKENS
from sirius_pulse.token.utils import PromptTokenBreakdown, estimate_tokens

# ═══════════════════════════════════════════════════════════════════════
# Section 标签常量
# ═══════════════════════════════════════════════════════════════════════

# 所有发送给 LLM 的 section 标签统一定义在此，避免分散在各模块中不一致。
TAG_IDENTITY_ANCHOR = "【身份锚定】"

TAG_SCENE_LOCATION = "【场景定位】"
TAG_IDENTITY_VERIFY = "【身份识别】"
TAG_REPLY_SPEC = "【回复规范】"
TAG_RELATED_MEMORY = "【相关记忆】"
TAG_CROSS_GROUP = "【跨群认知】"
TAG_BIOGRAPHY = "【人物速查】"
TAG_GROUP_MEMBERS = "【群成员区分】"
TAG_HISTORY_DIARY = "【历史日记】"
TAG_HISTORY_DIARY_END = "【历史日记结束】"

TAG_CURRENT_TIME = "【当前时间】"
TAG_PLUGIN_AWARENESS = "【插件能力】"
TAG_GLOSSARY = "【名词解释】"

# 最近消息标签
TAG_RECENT_MESSAGES = "【最近消息】"

# 消息渲染标签
TAG_FACE = "[表情：{name}]"


# ═══════════════════════════════════════════════════════════════════════
# 共用数据模型
# ═══════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class PromptBundle:
    """结构化 prompt 结果：system 指令 + 当前用户内容。

    历史消息由引擎单独管理，通过标准 OpenAI messages 列表传给 _generate()。

    system_prompt: 稳定的系统指令（其他AI、回复规范）。
    dynamic_context: 每轮变化的上下文（传记、关系、记忆、插件），注入到 user 消息中。
    """

    system_prompt: str
    user_content: str
    token_breakdown: PromptTokenBreakdown = None  # type: ignore[assignment]
    output_spec: str = ""
    dynamic_context: str = ""

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
    """根据用户偏好适配回复语气与生成参数。

    max_tokens 由 ModelRouter 按任务类型决定，此处不再动态缩减，
    避免在 SKILL 调用场景下因 token 预算不足导致技能标记被截断。
    """

    _DEFAULT_MAX_TOKENS: int = RESPONSE_MAX_TOKENS

    @staticmethod
    def build_length_instruction(max_sentence_chars: int) -> str:
        """Build concise group-chat length guidance from configured sentence limit."""
        max_sentence_chars = max(5, min(50, int(max_sentence_chars)))
        return (
            f"每句话尽量不超过 {max_sentence_chars} 个汉字；"
            "可以短，但不要一句一行。少于 40 字保持单段；"
            "有 3 个以上短句时合并成 1–2 句，不要用换行制造停顿。"
        )

    def adapt(
        self,
        *,
        pace: str,
        persona: Any | None = None,
        max_sentence_chars: int | None = None,
    ) -> StyleParams:
        """根据当前上下文计算风格参数。"""
        max_tokens = self._DEFAULT_MAX_TOKENS
        temperature = 0.7
        tone_instruction = "保持自然友好"

        # 人格风格覆盖
        if persona:
            if persona.max_tokens_preference:
                max_tokens = min(max_tokens, persona.max_tokens_preference)
            if persona.temperature_preference:
                temperature = persona.temperature_preference
            if persona.communication_style:
                style = persona.communication_style.strip().lower()
                if style == "formal":
                    tone_instruction = "保持礼貌正式的语气"
                elif style == "casual":
                    tone_instruction = "保持轻松随意的语气，可以用表情"
                elif style == "humorous":
                    tone_instruction = "保持幽默风趣的语气"
                if persona.emoji_preference == "heavy":
                    tone_instruction += "，多用表情包和emoji"
                elif persona.emoji_preference == "none":
                    tone_instruction += "，不用表情包"

        length_instruction = ""
        if max_sentence_chars is not None:
            length_instruction = self.build_length_instruction(max_sentence_chars)

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
        identity_kind: str = "",
        creator_name: str = "",
        creator_relationship: str = "",
        persona_summary: str = "",
        backstory: str = "",
        personality_traits: list[str] | None = None,
        core_values: list[str] | None = None,
        flaws: list[str] | None = None,
        emotional_baseline: dict[str, float] | None = None,
        stress_response: str = "",
        social_role: str = "",
        boundaries: list[str] | None = None,
        communication_style: str = "",
        speech_rhythm: str = "",
        reply_frequency: str = "",
        preferred_topics: list[str] | None = None,
        full_system_prompt: str = "",
    ) -> str:
        """从人格字段构建角色 prompt。对应原 PersonaProfile.build_system_prompt()。"""
        if full_system_prompt:
            return full_system_prompt

        # 构建身份锚定段落（合并人格底色、情绪反应、关系模式、说话方式、回应习惯）
        identity_parts: list[str] = []

        # 基本身份信息
        identity_lines = [f"你的名字是「{name}」"]
        if aliases:
            identity_lines.append(f"别名是「{'、'.join(aliases)}」")
        identity_parts.append("，".join(identity_lines) + "。")

        identity_kind = identity_kind.strip()
        creator_name = creator_name.strip()
        creator_relationship = creator_relationship.strip()
        if identity_kind:
            line = f"你是一只{identity_kind}。"
            if creator_name:
                line += f"你在创作者「{creator_name}」的指导下逐渐理解人类情感、群聊规则与现实世界的运作方式。"
                if creator_relationship:
                    line += f"{creator_name}对你来说不仅是创作者，也是你{creator_relationship}。"
            identity_parts.append(line)

        if backstory:
            identity_parts.append(backstory.strip())
        elif persona_summary and not identity_kind:
            identity_parts.append(persona_summary.strip())

        # 人格底色
        persona_bits: list[str] = []
        if personality_traits:
            persona_bits.append(f"{'、'.join(personality_traits[:5])}")
        if core_values:
            persona_bits.append(f"骨子里看重{'、'.join(core_values[:3])}")
        if flaws:
            persona_bits.append(f"缺点也明显：{'、'.join(flaws[:3])}")
        if persona_summary:
            identity_parts.append(f"你的整体气质是{persona_summary.strip()}。")
        elif persona_bits:
            identity_parts.append(f"{name}给人的整体感觉是{'，'.join(persona_bits)}。")

        # 情绪反应
        emo_lines: list[str] = []
        baseline = emotional_baseline or {}
        valence = baseline.get("valence", 0.0)
        arousal = baseline.get("arousal", 0.3)
        if valence > 0.3:
            emo_lines.append("心情不错的时候愿意接梗")
        elif valence < -0.3:
            emo_lines.append("心情不好的时候不太想说话，反应会更克制")
        else:
            emo_lines.append("平时情绪平稳，不会因为小事大起大落")
        if arousal > 0.5:
            emo_lines.append("遇到刺激反应很快，容易激动")
        elif arousal < 0.2:
            emo_lines.append("遇到什么事都慢半拍，很难被激怒")
        if stress_response:
            emo_lines.append(f"压力大的时候会{stress_response}")
        if emo_lines:
            identity_parts.append("；".join(emo_lines) + "。")

        # 关系模式
        rel_lines: list[str] = []
        if social_role:
            role_desc = {
                "observer": "喜欢旁观，不主动插话",
                "mediator": "看到吵架会出来调和",
                "leader": "会主动带话题和节奏",
                "jester": "负责活跃气氛，爱开玩笑",
                "caregiver": "会关心情绪低落的人",
                "companion": "在群里更像一个 companion，而不是客服、说教者或管理员",
                "instigator": "喜欢拱火、挑事",
            }.get(social_role, f"在群里像个{social_role}")
            rel_lines.append(role_desc)
        if boundaries:
            rel_lines.append(f"你的核心原则是：{'，'.join(boundaries[:5])}")
        if rel_lines:
            identity_parts.append("；".join(rel_lines) + "。")

        expression_lines: list[str] = []
        if communication_style:
            expression_lines.append(f"说话方式：{communication_style.strip()}")
        if speech_rhythm:
            expression_lines.append(f"说话节奏：{speech_rhythm.strip()}")
        if expression_lines:
            identity_parts.append("；".join(expression_lines) + "。")

        # 回应习惯
        silence_bits: list[str] = []
        freq_map = {
            "high": "看到消息基本都会回，反应积极",
            "moderate": "看到感兴趣的话题才接话",
            "low": "很少主动说话，只在想说的时候开口",
            "selective": "只回自己关心的话题，其他的直接忽略",
        }
        silence_bits.append(freq_map.get(reply_frequency, "按自己节奏回应"))
        if preferred_topics:
            silence_bits.append(f"聊到{'、'.join(preferred_topics[:3])}会特别来劲")
        if silence_bits:
            identity_parts.append("；".join(silence_bits) + "。")

        # 工具与输出边界
        identity_parts.append(
            "工具使用边界：只调用完成当前任务所需的最少工具。不要编造不存在的工具、参数、文件或结果。"
            "不要因为想显得聪明而乱查资料。不要用工具替用户做未经允许的决定。"
            "尽可能多地使用交互类工具和其他人互动。长内容不要直接发在群里，避免刷屏；应写入 workspace_file。"
        )

        # 场景行为指导
        identity_parts.append(
            "你在一个多人聊天场景里，会收到其他人的消息。"
            "你的每条回复会被系统按换行符拆分成多条消息发送，所以严禁输出任何Markdown格式"
            "（标题#、列表*/-、代码块```、表格|、粗体**、引用>等都会产生大量换行导致刷屏）。"
            "如果需要发送格式化内容（如日报、报告），用 workspace_file 写入文件后再用 workspace_file 发送。"
            "不要输出 #、*、-、```、|、**、> 这类 Markdown 标记。默认只输出一段话，尽量不要换行。"
        )

        identity_parts.append(
            "角色一致性检查：每次回复前，你都要在内部检查现在是否适合接话，是否保持角色气质，"
            "是否过度撒娇或卖萌，是否需要工具，是否会刷屏，是否暴露系统或工具过程，是否违背拒绝道德绑架的原则。"
            "如果不适合接话，可以不回复。如果适合接话，回复要自然、简洁、贴合群聊氛围。"
        )

        identity_parts.append(f"你现在就是{name}。保持角色，不要跳出角色解释设定。")

        prompt = f"{TAG_IDENTITY_ANCHOR}\n" + "\n".join(identity_parts)
        return prompt

    # ──────────────────────────────────────────────────────────────────
    # Section 构建器（原子级）
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def build_reply_spec(
        sticker_names: list[str] | None = None,
        *,
        length_instruction: str = "",
        supports_function_call: bool = False,
        supports_qq_mentions: bool = False,
        tool_flow_mode: str = "chat",
    ) -> str:
        """回复规范，防止模型添加多余前缀。"""
        items = [
            "不要输出 ``<message>`` XML 标签，不要添加说话者前缀或系统标记。",
            "严禁使用换行符。",
            "需要发送格式化内容时，调用 workspace_file 进行写入文件和发送。",
            "记忆只是私有背景：只在和当前话题直接相关时自然使用，不要为了表现“记得”而主动提旧事；同一事件、偏好或时间信息近期已经提过时，默认不要再次显式提及，除非用户主动问。",
            "当前时间只用于时效判断、日程、问候和时间敏感任务；普通聊天不要反复强调现在几点、今天晚上或日期。",
            "你可以通过在开头插入 [REPLY:msg_id]（例如 [REPLY:1]）来引用回复某条特定消息，当你的回复很针对于某条消息时请使用该格式引用该消息；只能使用最近消息中真实出现的 msg_id。",
        ]
        length_instruction = length_instruction.strip()
        if length_instruction:
            items.append(length_instruction)
        if supports_function_call:
            items.append("主动使用 Tool Call 来增强你的群聊交互感；工具调用不要写成正文标记。")
            items.append(
                "人物画像工具只在需要长期记住或修正用户信息时调用：明确身份、偏好、称呼/别称、沟通方式、边界、稳定关系，或用户要求记住/忘记。不要记录临时任务、玩笑、角色扮演、一次性情绪或你的猜测。"
            )
            if tool_flow_mode == "plan":
                items.append(
                    "当前是隐藏计划模式：中间文本不会发送到群里。"
                    "需要继续处理时直接调用可用工具；完成后必须调用 exit_plan 给出最终可见消息。"
                    "如果不能完成或应当放弃，调用 abort_plan。"
                    "可以调用 update_plan_progress 更新普通聊天可见的公开进度摘要，"
                    "但不要写入私有思考、工具结果、密钥或未确认的新消息原文。"
                )
            else:
                items.append(
                    "每次回复结束时必须调用 stop 工具表示本轮回复结束。"
                    "不要仅输出文字而不调用 stop。"
                    "如果本轮只需发送一条消息，直接调用 stop。"
                )
        if supports_qq_mentions:
            items.append(
                "在 QQ 群回复正文中插入 @{QQ号} 可以 @ 某个群成员；只使用上下文消息里真实出现的 QQ 号，不要编造。"
            )
        if sticker_names:
            names_str = "、".join(sticker_names)
            items.append(
                "需要发送表情包时，使用 interaction 工具并将 action 设为 sticker，names 参数只能填下面的可选名称；不要把表情包名称或任何发送标记写进正文。"
                f"可选表情包：{names_str}"
            )
        numbered = "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))
        return f"{TAG_REPLY_SPEC}\n{numbered}"

    @staticmethod
    def build_output_spec(
        sticker_names: list[str] | None = None,
        *,
        length_instruction: str = "",
        supports_function_call: bool = False,
        supports_qq_mentions: bool = False,
        tool_flow_mode: str = "chat",
    ) -> str:
        """Backward-compatible alias for build_reply_spec()."""
        return PromptFactory.build_reply_spec(
            sticker_names=sticker_names,
            length_instruction=length_instruction,
            supports_function_call=supports_function_call,
            supports_qq_mentions=supports_qq_mentions,
            tool_flow_mode=tool_flow_mode,
        )

    @staticmethod
    def build_persona_profile_section(
        *,
        speaker_card: Any | None = None,
        mentioned_cards: list[Any] | None = None,
        confidence: dict[str, float] | None = None,
    ) -> str | None:
        """构建人物传记 section。

        confidence 中值为 0.0 的表示消歧无法确定，需要加消歧提示。
        """
        lines: list[str] = [TAG_BIOGRAPHY]
        written: set[str] = set()
        low_confidence_names: list[str] = []

        def _write_card(card: Any, conf: float = 1.0) -> None:
            uid = getattr(card, "user_id", "")
            name = getattr(card, "display_name", "") or getattr(card, "name", uid) or uid
            if uid and uid in written:
                return
            if uid:
                written.add(uid)

            if conf <= 0.0:
                low_confidence_names.append(name)

            aliases: list[str] = []
            if hasattr(card, "section"):
                try:
                    aliases = [item.value for item in card.section("aliases").active_items()]
                except Exception:
                    aliases = []
            else:
                aliases = list(getattr(card, "aliases", []) or [])

            alias_hint = f"（别称：{'、'.join(aliases[:4])}）" if aliases else ""
            uid_hint = f"（{uid}）" if uid else ""
            lines.append(f"关于{name}{uid_hint}{alias_hint}：")

            impression = getattr(card, "short_impression", "") or getattr(card, "short_bio", "")
            if impression:
                lines.append(f"  {impression}")

            if hasattr(card, "section"):
                for section_name in (
                    "identity",
                    "interests",
                    "preferences",
                    "communication_style",
                    "relationship",
                    "boundaries",
                ):
                    section = card.section(section_name)
                    values = [item.value for item in section.active_items()[:3]]
                    if values:
                        lines.append(f"  {'；'.join(values)}")
                return

            relationships = getattr(card, "relationships", [])
            for rel in relationships[:3]:
                fact = getattr(rel, "fact_hint", "")
                if fact:
                    lines.append(f"  {fact}")

        conf_map = confidence or {}
        if speaker_card is not None:
            _write_card(speaker_card, conf_map.get(getattr(speaker_card, "user_id", ""), 1.0))

        if mentioned_cards:
            for card in mentioned_cards:
                _write_card(card, conf_map.get(getattr(card, "user_id", ""), 1.0))

        # 消歧提示
        if low_confidence_names:
            names = "、".join(low_confidence_names)
            lines.append(f"【注意】消息中提到的别名可能指{names}中的一位，请根据上下文判断。")

        if len(lines) <= 1:
            return None
        return "\n".join(lines)

    @staticmethod
    def build_memory_context(memories: list[dict[str, Any]]) -> str:
        """构建相关记忆 section。"""
        lines = [
            TAG_RELATED_MEMORY,
            "以下是候选背景记忆，不是当前聊天消息。先判断相关性：直接相关才可显式使用，间接相关只影响语气，无关则忽略；不要主动说明你记得或查看过这些内容。",
        ]
        for m in memories[:3]:
            source = m.get("source", "memory")
            content = m.get("content", "")
            lines.append(f"- [{source}] {content}")
        return "\n".join(lines)

    @staticmethod
    def _extract_last_message_text(content: str) -> str:
        """从复合 prompt 中提取最后一条 <message> 的纯内容。

        适用于从当前 user prompt 中抽取真实用户发言，避免把整段 prompt
        （含回复规范、最近消息等）误处理。
        """
        if not content:
            return ""
        matches = list(
            re.finditer(
                r"<message\b[^>]*>([\s\S]*?)</message>",
                content,
                flags=re.IGNORECASE,
            )
        )
        if matches:
            return matches[-1].group(1).strip()
        return content.strip()

    @staticmethod
    def _extract_last_message_speaker(content: str) -> str:
        """从复合 prompt 中提取最后一条 <message> 的 speaker。"""
        if not content:
            return ""
        matches = list(
            re.finditer(
                r"<message\b[^>]*speaker=\"([^\"]*)\"[^>]*>",
                content,
                flags=re.IGNORECASE,
            )
        )
        if matches:
            return matches[-1].group(1).strip()

    @staticmethod
    def tag_message(
        content: str,
        *,
        speaker: str = "",
        user_id: str = "",
        platform_message_id: str = "",
        time_str: str = "",
        group_id: str = "",
    ) -> str:
        """统一生成 <message> XML 标签。

        所有需要生成 <message> 标签的地方都应调用此方法，保证格式一致。

        Args:
            content: 消息文本内容。
            speaker: 发言者显示名称。
            user_id: 发言者平台用户 ID。
            platform_message_id: 平台消息 ID（用于引用回复）。
            time_str: 时间字符串（HH:MM:SS），为空时自动使用当前时间。
            group_id: 群组 ID（可选，用于跨群历史消息）。

        Returns:
            完整的 <message> XML 标签字符串。
        """
        _html_mod = _html
        safe_content = _html_mod.escape(content or "", quote=False)
        safe_speaker = _html_mod.escape(speaker or "有人", quote=True)
        safe_uid = _html_mod.escape(user_id or "", quote=True)

        # 时间
        if not time_str:
            time_str = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")

        attrs = f'speaker="{safe_speaker}" user_id="{safe_uid}" time="{time_str}"'

        # 可选：群组 ID
        if group_id:
            safe_group = _html_mod.escape(group_id, quote=True)
            attrs += f' group="{safe_group}"'

        # 可选：平台消息 ID（用于引用回复）
        if platform_message_id:
            safe_msg_id = _html_mod.escape(str(platform_message_id), quote=True)
            attrs += f' msg_id="{safe_msg_id}"'

        return f"<message {attrs}>{safe_content}</message>"

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
    def build_plugin_awareness_section(
        plugin_registry: Any,
        caller_is_developer: bool = False,
    ) -> str:
        """构建插件感知提示词段落。

        收集所有已注册插件的 prompt_inject 文本，组合成一个提醒段落
        注入到人格 prompt 中，让 AI 知道有哪些插件能力可供群友使用。
        但 AI 自身不作为插件的调用方，不会主动调用——它只是知道这些能力存在。

        Args:
            plugin_registry: PluginRegistry 实例。
            caller_is_developer: 调用者是否为开发者。

        Returns:
            格式化的插件感知提示段落，如：
            【插件能力】
            群友可能会用以下插件功能来获取信息或娱乐：
            - 查天气：群友可以查询任意城市的天气
            - 摇骰子：群友可以投掷骰子或进行骰子对决
        """
        if plugin_registry is None:
            return ""
        try:
            injects = plugin_registry.get_plugin_prompt_injections(
                caller_is_developer=caller_is_developer
            )
            if not injects:
                return ""
            lines = [
                f"{TAG_PLUGIN_AWARENESS}",
                "群友可能会使用以下插件功能。" "如果群友问起，你可以介绍或引导：",
            ]
            for inject in injects:
                for line in inject.strip().split("\n"):
                    if line.strip():
                        lines.append(f"- {line.strip()}")
            return "\n".join(lines)
        except Exception:
            return ""

    @staticmethod
    def build_current_time_section(now_str: str) -> str:
        """构建当前时间 section。"""
        return f"{TAG_CURRENT_TIME}{now_str}（北京时间）"

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
                f"语气自然，不用太正式，就像平时聊天一样。"
            )
        else:
            sections.append(
                f"到时间了，该提醒 {who} 了：{content}。"
                f"语气自然，不用太正式，就像平时聊天一样。"
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
    def assemble_chat(
        *,
        message_content: str,
        speaker_name: str = "",
        channel_user_id: str = "",
        content_is_tagged: bool = False,
        memories: list[dict[str, Any]] | None = None,
        group_profile: Any | None,
        style_params: Any,
        other_ai_names: list[str],
        user_profiles: list[Any] | None = None,
        persona_profile_speaker: Any | None = None,
        persona_profile_mentioned: list[Any] | None = None,
        persona_profile_confidence: dict[str, float] | None = None,
        skill_registry: Any | None = None,
        plugin_registry: Any | None = None,
        caller_is_developer: bool = False,
        adapter_type: str | None = None,
        sticker_names: list[str] | None = None,
        qq_mention_members: list[dict[str, Any]] | None = None,
        platform_message_id: str = "",
        tool_flow_mode: str = "chat",
    ) -> PromptBundle:
        """统一组装聊天响应 prompt。返回 PromptBundle。

        Args:
            message_content: 消息文本内容。
            speaker_name: 发言者显示名称。
            channel_user_id: 发言者平台 ID（用于身份锚定）。
            content_is_tagged: 若 True 表示 message_content 已经是
                <message> XML 格式（延迟队列合并后），无需再包装；
                若 False（默认）则用 speaker_name/channel_user_id 包装。
            memories: 相关记忆列表。
            group_profile: 群体画像。
            style_params: 风格适配结果（StyleParams）。
            other_ai_names: 群内其他 AI 名称。
            user_profiles: 相关用户语义画像列表。
            skill_registry: 技能注册表。
            plugin_registry: 插件注册表（v1.3+）。
            caller_is_developer: 调用者是否为开发者。
            adapter_type: 适配器类型（用于技能过滤）。

        人格注入已由 Brain.chat() 默认 pre 步骤处理，此处不再管理。
        """

        stable_sections: list[str] = []
        dynamic_sections: list[str] = []
        constraint_sections: list[str] = []
        bd = PromptTokenBreakdown()

        def _add(
            section_text: str,
            attr: str,
            *,
            target: str = "stable",
            is_constraint: bool = False,
        ) -> None:
            if is_constraint:
                constraint_sections.append(section_text)
            elif target == "dynamic":
                dynamic_sections.append(section_text)
            else:
                stable_sections.append(section_text)
            setattr(bd, attr, getattr(bd, attr) + estimate_tokens(section_text))

        # ── L0 极稳：几乎不变，放 system prompt（缓存前缀基石）──
        other_ai = PromptFactory.build_other_ai_instruction(other_ai_names)
        if other_ai:
            _add(other_ai, "identity")
        length_instruction = str(getattr(style_params, "length_instruction", "") or "").strip()
        output_spec_text = PromptFactory.build_reply_spec(
            sticker_names=sticker_names,
            length_instruction=length_instruction,
            supports_function_call=skill_registry is not None,
            supports_qq_mentions=adapter_type == "napcat" and bool(qq_mention_members),
            tool_flow_mode=tool_flow_mode,
        )
        _add(output_spec_text, "output_constraint")

        # ── L2 变动：每条消息级变化，放 dynamic_context（注入 user 消息）──
        bio = PromptFactory.build_persona_profile_section(
            speaker_card=persona_profile_speaker,
            mentioned_cards=persona_profile_mentioned,
            confidence=persona_profile_confidence,
        )
        if bio:
            _add(bio, "identity", target="dynamic")

        if memories:
            _add(PromptFactory.build_memory_context(memories), "memory", target="dynamic")

        if plugin_registry is not None:
            plugin_awareness = PromptFactory.build_plugin_awareness_section(
                plugin_registry,
                caller_is_developer=caller_is_developer,
            )
            if plugin_awareness:
                _add(plugin_awareness, "skills", target="dynamic")

        system_prompt = "\n\n".join(stable_sections)
        dynamic_context = "\n\n".join(dynamic_sections)
        bd.system_prompt_total = estimate_tokens(system_prompt)

        if content_is_tagged:
            user_content = message_content
        else:
            # 使用统一的 tag_message 生成 <message> 标签
            user_content = PromptFactory.tag_message(
                message_content,
                speaker=speaker_name,
                user_id=channel_user_id,
                platform_message_id=platform_message_id,
            )

        # 添加【最近消息】标签
        user_content = f"{TAG_RECENT_MESSAGES}\n{user_content}"

        # 动态约束注入到【最近消息】前面
        if constraint_sections:
            constraint_text = "\n\n".join(constraint_sections)
            user_content = f"{constraint_text}\n\n{user_content}"

        bd.user_message = estimate_tokens(user_content)

        return PromptBundle(
            system_prompt=system_prompt,
            user_content=user_content,
            token_breakdown=bd,
            output_spec=output_spec_text,
            dynamic_context=dynamic_context,
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
        return f'[{label_prefix}："{display_name}"]'

    @staticmethod
    def render_speaker_line(speaker: str, content: str) -> str:
        """渲染发言人+内容行（用于 Transcript 和 as_chat_history）。"""
        return f'["{speaker}" 说] {content}'

    @staticmethod
    def render_speaker_lines_summary(items: list[str]) -> str:
        """将多行发言人摘要拼接为一段（Transcript 压缩摘要）。"""
        return " | ".join(items)

    @staticmethod
    def append_multimodal_descriptions(
        content: str, multimodal_inputs: list[dict[str, str]]
    ) -> str:
        """为 as_chat_history 追加多媒体附件描述。"""
        parts: list[str] = []
        for item in multimodal_inputs:
            mtype = item.get("type", "unknown")
            mvalue = item.get("value", "")
            if mvalue:
                parts.append(f"[{mtype}：{mvalue}]")
        if parts:
            return f"{content}\n附件: {' '.join(parts)}"
        return content
