"""轻量级插件意图验证器。

在嵌入向量匹配通过后，使用小型 LLM 调用确认是否真的是插件请求，
并提取插件参数。比完整管线（认知+决策+执行）更轻量。

依赖：
    - Brain（LLM 调用通道）
    - ModelRouter（获取任务配置）
    - PluginRegistry（获取插件定义）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sirius_pulse.core.brain import Brain
    from sirius_pulse.core.model_router import ModelRouter
    from sirius_pulse.plugins.registry import PluginRegistry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PluginIntentResult:
    """插件意图验证结果。"""

    is_plugin: bool  # 是否是插件请求
    plugin_name: str = ""  # 插件名称（仅当 is_plugin=True 时有效）
    confidence: float = 0.0  # 置信度
    slots: dict[str, Any] | None = None  # 提取的参数槽位
    reason: str = ""  # 判断理由（用于调试）


class PluginIntentVerifier:
    """轻量级插件意图验证器。

    工作流程：
        1. 从 PluginRegistry 收集插件信息（名称、描述、参数定义）
        2. 构建简洁的验证 prompt
        3. 调用 LLM 判断用户消息是否是插件请求
        4. 如果是，提取插件名称和参数

    与完整管线的区别：
        - 只关注插件意图，不分析情感/社交意图
        - Prompt 更短，输出更简单
        - 使用更快的模型（可配置）
    """

    def __init__(
        self,
        brain: Brain,
        model_router: ModelRouter,
        plugin_registry: PluginRegistry,
    ) -> None:
        self._brain = brain
        self._model_router = model_router
        self._plugin_registry = plugin_registry

        # 缓存插件描述（按插件名组合缓存）
        self._plugin_desc_cache: dict[str, str] = {}

    def _build_plugin_description(self, candidate_plugins: list[str] | None = None) -> str:
        """构建插件描述文本（用于 prompt）。

        Args:
            candidate_plugins: 候选插件名称列表。
                - None: 构建所有插件的描述
                - 非空列表: 只构建指定插件的描述

        Returns:
            插件描述文本。
        """
        # 确定要处理的插件列表
        if candidate_plugins is not None:
            plugin_names = candidate_plugins
        else:
            plugin_names = list(self._plugin_registry.plugin_names)

        # 生成缓存键
        cache_key = ",".join(sorted(plugin_names))
        if cache_key in self._plugin_desc_cache:
            return self._plugin_desc_cache[cache_key]

        parts: list[str] = []
        for name in plugin_names:
            definition = self._plugin_registry.get(name)
            if definition is None:
                continue
            if definition.permissions.hidden_from_intent:
                continue

            desc = definition.description or definition.display_name or name
            param_defs: list[str] = []

            # 收集参数定义
            for param in definition.parameters:
                param_desc = param.name
                if param.type_hint:  # type: ignore[attr-defined]
                    param_desc += f": {param.type_hint}"  # type: ignore[attr-defined]
                if param.description:
                    param_desc += f" ({param.description})"
                param_defs.append(param_desc)

            # 收集自然语言示例
            examples: list[str] = []
            if definition.natural_language and definition.natural_language.examples:
                examples.extend(definition.natural_language.examples[:3])  # 最多3个示例

            entry = f"- {name}: {desc}"
            if param_defs:
                entry += f"\n  参数: {', '.join(param_defs)}"
            if examples:
                entry += f"\n  示例: {'; '.join(examples)}"
            parts.append(entry)

        result = "\n".join(parts) if parts else "（无可用插件）"
        self._plugin_desc_cache[cache_key] = result
        return result

    async def verify(
        self,
        message: str,
        candidate_plugins: list[str] | None = None,
        context_xml: str = "",
        persona_name: str = "AI",
        persona_aliases: list[str] | None = None,
    ) -> PluginIntentResult:
        """验证用户消息是否是插件请求。

        Args:
            message: 用户输入文本
            candidate_plugins: 候选插件名称列表（由向量匹配提供）。
                - None: 使用所有插件
                - 非空列表: 只考虑这些插件
            context_xml: XML 格式的历史上下文（由 ContextAssembler.build_history_xml 生成）
            persona_name: 人格名称（如 "小星"）
            persona_aliases: 人格别名列表

        Returns:
            PluginIntentResult 包含判断结果和提取的参数。
        """
        plugin_desc = self._build_plugin_description(candidate_plugins)
        if not plugin_desc or plugin_desc == "（无可用插件）":
            return PluginIntentResult(is_plugin=False, reason="无可用插件")

        # 获取模型配置（使用 cognition_analyze 任务，通常配置较快的模型）
        cfg = self._model_router.resolve("cognition_analyze")

        # 构建上下文部分（复用 ContextAssembler 的 XML 格式）
        context_section = ""
        if context_xml:
            context_section = f"\n## 最近对话上下文\n{context_xml}\n"

        # 构建身份信息
        aliases_str = "、".join(persona_aliases) if persona_aliases else "无"
        identity_section = f"## AI 身份\n" f"- 名称：{persona_name}\n" f"- 别名：{aliases_str}\n"

        system_prompt = (
            f"你是一个插件意图验证器。你正在帮助 AI「{persona_name}」判断用户是否在请求它执行某个功能。\n"
            "\n"
            "## 重要判断原则\n"
            f"1. **只有用户明确请求「{persona_name}」执行功能时才返回 true**\n"
            "2. 仅仅提到相关话题 ≠ 插件请求\n"
            "3. 用户和别人聊天时提到相关内容 ≠ 请求AI执行\n"
            "4. 这是向量匹配后的二次确认，向量可能误判，你需要严格把关\n"
            "\n"
            "## 判断标准（满足任一 → true）\n"
            f"- 用户直接请求AI：「{persona_name}帮我查...」「帮我查一下...」\n"
            "- 用户明确指令（上下文中AI是对话对象）：「查一下...」「搜索...」「分析一下...」\n"
            f"- 用户询问AI功能：「{persona_name}你能查天气吗」「怎么用这个功能」\n"
            "- 结合上下文，用户在回应AI的提问并请求执行操作\n"
            "\n"
            "## 否定标准（满足任一 → false）\n"
            "- 闲聊中提到：「今天天气真好」「我在看天气预报」\n"
            "- 讨论话题：「天气预报说明天会下雨」「你们那边天气怎么样」\n"
            "- 表达感受：「今天好热啊」「下雨天心情不好」\n"
            "- 信息分享：「我看新闻说...」「我听说...」\n"
            "- 用户在和别人对话（不是在和AI对话）\n"
            f"\n{identity_section}"
            "\n"
            "## 可用插件\n"
            f"{plugin_desc}\n"
            f"{context_section}\n"
            "## 输出格式\n"
            "严格返回 JSON，不要添加其他文本：\n"
            '{"is_plugin": true/false, "plugin_name": "插件名或空字符串", '
            '"confidence": 0.0-1.0, "slots": {"参数名": "值"}, "reason": "一句话说明判断依据"}'
        )

        from sirius_pulse.core.brain import RawRequest

        request = RawRequest(
            model=cfg.model_name,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": message}],
            temperature=0.1,  # 低温度，确保输出稳定
            max_tokens=512,
            timeout_seconds=10.0,  # 短超时
            purpose="plugin_intent_verify",
            response_format={"type": "json_object"},
        )

        try:
            raw = await self._brain.raw_call(request)
            return self._parse_result(raw)
        except Exception as exc:
            logger.warning("插件意图验证失败: %s", exc)
            return PluginIntentResult(
                is_plugin=False,
                reason=f"LLM 调用失败: {exc}",
            )

    def _parse_result(self, raw: str) -> PluginIntentResult:
        """解析 LLM 输出。"""
        try:
            # 尝试直接解析 JSON
            data = json.loads(raw.strip())
            return PluginIntentResult(
                is_plugin=bool(data.get("is_plugin", False)),
                plugin_name=str(data.get("plugin_name", "")),
                confidence=float(data.get("confidence", 0.0)),
                slots=data.get("slots") or {},
                reason=str(data.get("reason", "")),
            )
        except json.JSONDecodeError:
            # 尝试从文本中提取 JSON
            import re

            match = re.search(r"\{[^{}]*\}", raw)
            if match:
                try:
                    data = json.loads(match.group())
                    return PluginIntentResult(
                        is_plugin=bool(data.get("is_plugin", False)),
                        plugin_name=str(data.get("plugin_name", "")),
                        confidence=float(data.get("confidence", 0.0)),
                        slots=data.get("slots") or {},
                        reason=str(data.get("reason", "")),
                    )
                except (json.JSONDecodeError, ValueError):
                    pass

            logger.debug("无法解析插件意图验证结果: %s", raw[:200])
            return PluginIntentResult(
                is_plugin=False,
                reason=f"解析失败: {raw[:100]}",
            )
