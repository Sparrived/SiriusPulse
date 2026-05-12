"""Plugin 输出调度器 —— 根据 RenderMode 选择输出策略。

三种输出模式：
    - direct: 直接使用 PluginResult.text 发送给用户
    - llm: 将 PluginResult.data 委托给引擎做人格化风格生成
    - silent: 无输出，仅执行副作用

是 Plugin 执行流程的最后一环。
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sirius_chat.plugins.models import PluginDefinition, PluginResult, RenderMode

logger = logging.getLogger(__name__)


class OutputDispatcher:
    """Plugin 输出调度器。

    根据 RenderMode 选择合适的输出策略。
    """

    async def dispatch(
        self,
        result: "PluginResult",
        definition: "PluginDefinition",
        *,
        engine: Any = None,
        adapter: Any = None,
        group_id: str = "",
        user_id: str = "",
        message_id: str = "",
        **kwargs: Any,
    ) -> str:
        """调度 PluginResult 的输出。

        Args:
            result: Plugin 执行结果
            definition: Plugin 定义（读取 render 配置）
            engine: 引擎实例（llm 模式需要）
            adapter: 适配器实例（发送消息需要）
            group_id: 群号
            user_id: 用户 ID
            message_id: 消息 ID（私聊/回复用）

        Returns:
            最终发送的文本内容（silent 模式返回空字符串），失败时返回错误文本
        """
        # 确定实际渲染模式：PluginResult 可覆盖 plugin.json 的配置
        render_mode = self._resolve_mode(result, definition)

        if render_mode.value == "silent":
            logger.debug("Plugin %s: silent 模式，无输出", definition.name)
            return ""

        if render_mode.value == "direct":
            return self._handle_direct(result, definition, adapter=adapter, group_id=group_id, user_id=user_id)

        if render_mode.value == "llm":
            return await self._handle_llm(
                result, definition,
                engine=engine, adapter=adapter,
                group_id=group_id, user_id=user_id,
                **kwargs,
            )

        # 未知模式，fallback 到 direct
        logger.warning("Plugin %s: 未知 render_mode=%s，fallback 到 direct", definition.name, render_mode.value)
        return self._handle_direct(result, definition, adapter=adapter, group_id=group_id, user_id=user_id)

    # ── 各模式处理 ──

    def _handle_direct(
        self,
        result: "PluginResult",
        definition: "PluginDefinition",
        *,
        adapter: Any = None,
        group_id: str = "",
        user_id: str = "",
    ) -> str:
        """Direct 模式：直接使用 result.text。"""
        text = result.text
        if not text and result.data:
            text = str(result.data)
        if not text and not result.success:
            text = f"[{definition.display_name or definition.name}] 执行出错: {result.error}"
        return text

    async def _handle_llm(
        self,
        result: "PluginResult",
        definition: "PluginDefinition",
        *,
        engine: Any = None,
        adapter: Any = None,
        group_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> str:
        """LLM 模式：委托引擎做人格化风格生成。"""
        if engine is None:
            # 无引擎可用，降级到 direct
            logger.warning("Plugin %s: LLM 模式但无引擎可用，降级到 direct", definition.name)
            return self._handle_direct(result, definition, adapter=adapter, group_id=group_id, user_id=user_id)

        # 构建风格化 prompt
        prompt = self._build_stylized_prompt(result, definition, engine=engine, **kwargs)

        # 调用引擎的 LLM 生成
        try:
            from sirius_chat.core.prompt_factory import PromptFactory

            generated = await PromptFactory.generate_plugin_response(
                engine=engine,
                plugin_data=result.data if result.data else {"text": result.text},
                plugin_name=definition.display_name or definition.name,
                system_prompt_suffix=definition.render.system_prompt_suffix,
                mood_hint=result.mood_hint,
                tone_override=result.tone_override,
                max_tokens=definition.render.max_tokens,
                temperature=definition.render.temperature,
            )
            return generated if generated else result.text
        except Exception as exc:
            logger.error("Plugin %s: LLM 风格化生成失败: %s", definition.name, exc)
            # 降级到 direct
            return self._handle_direct(result, definition, adapter=adapter, group_id=group_id, user_id=user_id)

    @staticmethod
    def _build_stylized_prompt(
        result: "PluginResult",
        definition: "PluginDefinition",
        *,
        engine: Any = None,
        **kwargs: Any,
    ) -> str:
        """构建 LLM 风格化的 prompt。"""
        import json

        persona_name = ""
        if engine:
            persona = getattr(engine, "persona", None)
            if persona:
                persona_name = getattr(persona, "name", "") or ""

        data_json = json.dumps(result.data if result.data else {"text": result.text}, ensure_ascii=False, indent=2)

        parts: list[str] = []
        parts.append(f"【角色：{persona_name}】")

        parts.append(f"\n【指令执行结果】")
        parts.append(f"你刚刚执行了用户的 '{definition.display_name or definition.name}' 指令，获得以下数据：")
        parts.append(data_json)

        parts.append(f"\n【表达要求】")
        parts.append("- 请以自然的人格风格向用户传达以上信息")
        if result.mood_hint:
            parts.append(f"- 当前情绪提示：{result.mood_hint}")
        if result.tone_override:
            parts.append(f"- 语气要求：{result.tone_override}")
        if definition.render.system_prompt_suffix:
            parts.append(f"- {definition.render.system_prompt_suffix}")
        parts.append("- 不要暴露这是'执行结果'，要像自己知道的一样自然表达")

        return "\n".join(parts)

    # ── 辅助 ──

    @staticmethod
    def _resolve_mode(result: "PluginResult", definition: "PluginDefinition") -> "RenderMode":
        """确定实际渲染模式。"""
        from sirius_chat.plugins.models import RenderMode

        # PluginResult 可以覆盖配置
        if result.render_mode:
            mode = result.render_mode.lower()
            if mode == "llm":
                return RenderMode.LLM
            if mode == "silent":
                return RenderMode.SILENT
            return RenderMode.DIRECT
        return definition.get_render_mode()
