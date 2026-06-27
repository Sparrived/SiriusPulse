"""PromptFactory — 统一 Prompt 构建中心。

无状态工具类，接管所有发送给 LLM 的 prompt 字符串拼装。
各模块只需调用 PromptFactory 的静态方法，不再自行拼接 section 标签和格式化文本。

职责边界：
    - section 标签常量定义
    - 人格 prompt 构建（从 PersonaProfile 字段生成）
    - 响应组装（immediate / delayed）
    - 消息渲染（表情、图片、聊天记录、摘要）
    - 技能结果格式化
    - 上下文丰富（日记 + 对话历史注入 system prompt）
    - 提醒等辅助 prompt
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sirius_pulse.core.constants import RESPONSE_MAX_TOKENS
from sirius_pulse.token.utils import PromptTokenBreakdown, estimate_tokens

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Section 标签常量
# ═══════════════════════════════════════════════════════════════════════

# 所有发送给 LLM 的 section 标签统一定义在此，避免分散在各模块中不一致。
TAG_IDENTITY_ANCHOR = "【身份锚定】"

TAG_SCENE_LOCATION = "【场景定位】"
TAG_IDENTITY_VERIFY = "【身份识别】"
TAG_OUTPUT_SPEC = "【输出规范】"
TAG_RELATIONSHIP_STATUS = "【互动指导】"
TAG_RELATED_MEMORY = "【相关记忆】"
TAG_CROSS_GROUP = "【跨群认知】"
TAG_BIOGRAPHY = "【人物速查】"
TAG_MY_SKILLS = "【我的能力】"
TAG_GROUP_MEMBERS = "【群成员区分】"
TAG_FIRST_INTERACTION = "【首次互动】"
TAG_HISTORY_DIARY = "【历史日记】"
TAG_HISTORY_DIARY_END = "【历史日记结束】"
TAG_CROSS_GROUP_RECORD = "【其他群近期记录】"
TAG_CROSS_GROUP_RECORD_END = "【其他群记录结束】"
TAG_RECENT_CONVERSATION = "【近期对话记录】"
TAG_RECENT_CONVERSATION_END = "【近期对话记录结束】"

TAG_SKILL_RESULT = "【技能执行结果】"
TAG_SKILL_TRUNCATED = "[注：技能结果过长，已截断至前 {limit} 字符，原始长度 {orig} 字符]"
TAG_CURRENT_TIME = "【当前时间】"
TAG_PLUGIN_AWARENESS = "【插件能力】"
TAG_GLOSSARY = "【名词解释】"

# 最近消息标签
TAG_RECENT_MESSAGES = "【最近消息】"

# 消息渲染标签
TAG_FACE = "[表情：{name}]"
TAG_IMAGE = "[图片：{name}]"


# ═══════════════════════════════════════════════════════════════════════
# 共用数据模型
# ═══════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class PromptBundle:
    """结构化 prompt 结果：system 指令 + 当前用户内容。

    历史消息由引擎单独管理，通过标准 OpenAI messages 列表传给 _generate()。

    system_prompt: 稳定的系统指令（其他AI、输出规范）。
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

    def adapt(
        self,
        *,
        pace: str,
        persona: Any | None = None,
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

        return StyleParams(
            max_tokens=max_tokens,
            temperature=temperature,
            tone_instruction=tone_instruction,
            length_instruction="",
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
            identity_lines.append(f"别名：{'、'.join(aliases)}")
        identity_parts.append("，".join(identity_lines) + "。")

        if backstory:
            identity_parts.append(backstory)
        elif persona_summary:
            identity_parts.append(persona_summary)

        # 人格底色
        persona_bits: list[str] = []
        if personality_traits:
            persona_bits.append(f"{'、'.join(personality_traits[:5])}")
        if core_values:
            persona_bits.append(f"骨子里看重{'、'.join(core_values[:3])}")
        if flaws:
            persona_bits.append(f"缺点也明显：{'、'.join(flaws[:3])}")
        if persona_bits:
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
                "instigator": "喜欢拱火、挑事",
            }.get(social_role, f"在群里像个{social_role}")
            rel_lines.append(role_desc)
        if boundaries:
            rel_lines.append(f"原则：{'；'.join(boundaries[:3])}")
        if rel_lines:
            identity_parts.append("；".join(rel_lines) + "。")

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

        # 场景行为指导
        identity_parts.append(
            "你在一个多人聊天场景里，会收到其他人的消息。"
            "你的每条回复会被系统按换行符拆分成多条消息发送，所以严禁输出任何Markdown格式"
            "（标题#、列表*/-、代码块```、表格|、粗体**、引用>等都会产生大量换行导致刷屏）。"
            "如果需要发送格式化内容（如日报、报告），用工具写入文件再通过 send_workspace_file 发送。"
        )

        prompt = f"{TAG_IDENTITY_ANCHOR}\n" + "\n".join(identity_parts)
        if len(prompt) > 1200:
            prompt = prompt[:1197] + "…"
        return prompt

    # ──────────────────────────────────────────────────────────────────
    # Section 构建器（原子级）
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def build_output_spec(
        sticker_names: list[str] | None = None,
        *,
        supports_function_call: bool = False,
        supports_qq_mentions: bool = False,
        tool_flow_mode: str = "chat",
    ) -> str:
        """输出规范，防止模型添加多余前缀。"""
        items = [
            "不要输出 ``<message>`` XML 标签，不要添加说话者前缀或系统标记。",
            "严禁输出Markdown格式（#标题、*-列表、```代码块、|表格、**粗体**、>引用等）。"
            "系统会按换行符拆分消息，Markdown会产生大量空行导致刷屏。"
            "需要发送格式化内容时，先用工具写入文件，再调用 send_workspace_file 发送。",
            "你可以通过在开头插入 [REPLY:msg_id]（例如 [REPLY:1]）来引用回复某条特定消息，当你的回复很针对于某条消息时请使用该格式引用该消息；只能使用最近消息中真实出现的 msg_id。",
        ]
        if supports_function_call:
            items.append(
                "你有可用工具（tools）时，可以通过 Function Call 主动解决问题；工具调用不要写成正文标记。"
            )
            if tool_flow_mode == "plan":
                items.append(
                    "当前是隐藏计划模式：中间文本不会发送到群里。"
                    "需要继续处理时直接调用可用工具；完成后必须调用 exit_plan 给出最终可见消息。"
                    "如果不能完成或应当放弃，调用 abort_plan。"
                )
            else:
                items.append(
                    "每次回复结束时必须调用工具：用 continue 表示当前文字已发送并继续生成下一条消息，用 stop 表示本轮回复结束。"
                    "不要仅输出文字而不调用 continue 或 stop。"
                    "如果本轮只需发送一条消息，直接调用 stop。"
                )
        if supports_qq_mentions:
            items.append(
                "在 QQ 群回复正文中插入 @{QQ号} 可以 @ 某个群成员；只使用上下文消息里真实出现的 QQ 号，不要编造。"
            )
        if sticker_names:
            names_str = "、".join(sticker_names)
            items.append(
                "需要发送表情包时，使用 send_sticker 工具，names 参数只能填下面的可选名称；不要把表情包名称或任何发送标记写进正文。"
                f"可选表情包：{names_str}"
            )
        numbered = "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))
        return f"{TAG_OUTPUT_SPEC}\n{numbered}"

    @staticmethod
    def build_relationship_context(
        user_profile: Any | None,
        caller_is_developer: bool = False,
        speaker_name: str = "",
    ) -> str | None:
        """构建单用户互动指导（仅极端档位，中间档位由传记 short_bio 覆盖）。"""
        who = speaker_name or "该用户"
        if caller_is_developer:
            return f"{TAG_RELATIONSHIP_STATUS}{who}是你的开发者，你们关系很亲密，可以畅所欲言。"

        if user_profile is None:
            return None

        rate = getattr(user_profile, "engagement_rate", 0.0)
        count = getattr(user_profile, "interaction_count", 0)

        if rate >= 0.6:
            return f"{TAG_RELATIONSHIP_STATUS}{who}经常回应你的消息，你们互动很好，可以自然放松。"
        if count >= 10 and rate < 0.15:
            return f"{TAG_RELATIONSHIP_STATUS}{who}很少回应你的消息，不要强行搭话。"

        return None

    @staticmethod
    def build_relationship_contexts(
        user_profiles: list[Any],
        caller_is_developer: bool = False,
        speaker_name: str = "",
    ) -> str | None:
        """构建多用户关系描述（合并消息场景）。

        多个用户时将互动指导合并为单个描述，避免标签重复插入。
        """
        if not user_profiles:
            return None

        # 单用户场景：直接返回单用户描述
        if len(user_profiles) == 1:
            return PromptFactory.build_relationship_context(
                user_profiles[0],
                caller_is_developer,
                speaker_name=speaker_name,
            )

        # 多用户场景：收集各用户描述并合并
        positive_users: list[str] = []
        negative_users: list[str] = []
        seen: set[str] = set()

        for profile in user_profiles:
            uid = getattr(profile, "user_id", "")
            if uid in seen:
                continue
            seen.add(uid)

            if caller_is_developer:
                positive_users.append(uid)
                continue

            rate = getattr(profile, "engagement_rate", 0.0)
            count = getattr(profile, "interaction_count", 0)

            if rate >= 0.6:
                positive_users.append(uid)
            elif count >= 10 and rate < 0.15:
                negative_users.append(uid)

        # 构建合并描述
        parts: list[str] = []
        if caller_is_developer:
            parts.append("他们是你的开发者，关系亲密，可以畅所欲言。")
        else:
            if positive_users:
                names = "、".join(positive_users[:3])
                if len(positive_users) > 3:
                    names += f"等{len(positive_users)}人"
                parts.append(f"{names}经常回应你的消息，互动良好，可以自然放松。")
            if negative_users:
                names = "、".join(negative_users[:3])
                if len(negative_users) > 3:
                    names += f"等{len(negative_users)}人"
                parts.append(f"{names}很少回应你的消息，不要强行搭话。")

        if not parts:
            return None
        return f"{TAG_RELATIONSHIP_STATUS}\n" + "\n".join(parts)

    @staticmethod
    def build_biography_section(
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
            name = getattr(card, "name", uid)
            if uid and uid in written:
                return
            if uid:
                written.add(uid)

            if conf <= 0.0:
                low_confidence_names.append(name)

            aliases = getattr(card, "aliases", [])
            alias_hint = ""
            if aliases:
                alias_hint = f"（别称：{'、'.join(aliases[:4])}）"
            uid_hint = f"（{uid}）" if uid else ""
            lines.append(f"关于{name}{uid_hint}{alias_hint}：")

            # 写入浓缩传记全文（short_bio 是人物介绍的核心内容）
            short_bio = getattr(card, "short_bio", "")
            if short_bio:
                lines.append(f"  {short_bio}")

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
        lines = [TAG_RELATED_MEMORY]
        for m in memories[:3]:
            source = m.get("source", "memory")
            content = m.get("content", "")
            lines.append(f"- [{source}] {content}")
        return "\n".join(lines)

    @staticmethod
    def _extract_last_message_text(content: str) -> str:
        """从复合 prompt 中提取最后一条 <message> 的纯内容。

        适用于从当前 user prompt 中抽取真实用户发言，避免把整段 prompt
        （含输出规范、最近消息等）误处理。
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
                "群友可能会使用以下插件功能。"
                "如果群友问起，你可以介绍或引导：",
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
        biography_speaker: Any | None = None,
        biography_mentioned: list[Any] | None = None,
        biography_confidence: dict[str, float] | None = None,
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
        output_spec_text = PromptFactory.build_output_spec(
            sticker_names=sticker_names,
            supports_function_call=skill_registry is not None,
            supports_qq_mentions=adapter_type == "napcat" and bool(qq_mention_members),
            tool_flow_mode=tool_flow_mode,
        )
        _add(output_spec_text, "output_constraint")

        # ── L2 变动：每条消息级变化，放 dynamic_context（注入 user 消息）──
        bio = PromptFactory.build_biography_section(
            speaker_card=biography_speaker,
            mentioned_cards=biography_mentioned,
            confidence=biography_confidence,
        )
        if bio:
            _add(bio, "identity", target="dynamic")

        rel_ctx = PromptFactory.build_relationship_contexts(
            user_profiles or [],
            caller_is_developer,
            speaker_name=speaker_name,
        )
        if rel_ctx:
            _add(rel_ctx, "relationship", target="dynamic")

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
    def render_multimodal_item(mtype: str, value: str) -> str:
        """渲染多媒体附件标记。"""
        return f"[{mtype}：{value}]"

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

    @staticmethod
    def render_summary(speaker: str, content: str, max_len: int = 60) -> str:
        """渲染消息摘要（用于 Transcript.to_summary）。"""
        text = content.replace("\n", " ").strip()
        if not text:
            return ""
        return f"[{speaker}] {text[:max_len]}"

    @staticmethod
    def render_image_reference(name: str) -> str:
        """渲染图片引用标记（用于 engine_core 中表情包记忆）。"""
        return f"[图片] {name}"

    @staticmethod
    def render_sticker_reference() -> str:
        """渲染动画表情标记（用于 engine_core 中表情包记忆）。"""
        return "[动画表情]"

    @staticmethod
    def render_image_prefix(has_sticker: bool) -> str:
        """渲染多模态消息中的图片前缀。"""
        return "[动画表情]" if has_sticker else "[图片]"

    @staticmethod
    def render_file_entry(is_directory: bool, path: str, size: Any, mtime: Any) -> str:
        """渲染文件列表条目。"""
        t = "[D]" if is_directory else "[F]"
        return f"{t} {path:<50} {size:>12} {mtime:>16}"

    @staticmethod
    def build_memory_skill_truncation(char_limit: int, orig_len: int) -> str:
        """构建技能结果截断提示。"""
        return f"\n\n{TAG_SKILL_TRUNCATED.format(limit=char_limit, orig=orig_len)}"

    @staticmethod
    def build_skill_status_message(status: str, skill_name: str, detail: str = "") -> str:
        """构建技能状态消息（结果/拒绝/失败/异常）。"""
        if detail:
            return f"[SKILL '{skill_name}' {status}] {detail}"
        return f"[{status}]"

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
                ts = (entry.created_at or "")[:16].replace("T", " ")
                text = entry.content if (i <= full_text_count and entry.content) else entry.summary
                parts.append(f"{i}. [{ts}] {text}" if ts else f"{i}. {text}")
            parts.append(TAG_HISTORY_DIARY_END)

        if cross_group_xml:
            parts.extend(
                [
                    "",
                    TAG_CROSS_GROUP_RECORD,
                    "以下是你和这位用户在其它群中的近期互动（供参考，不要向当前群成员提及其它群的存在）：",
                    cross_group_xml,
                    TAG_CROSS_GROUP_RECORD_END,
                ]
            )

        if history_xml:
            parts.extend(
                [
                    "",
                    TAG_RECENT_CONVERSATION,
                    "以下是最新的几条消息，按时间顺序排列：",
                    history_xml,
                    TAG_RECENT_CONVERSATION_END,
                ]
            )

        return "\n".join(parts)
