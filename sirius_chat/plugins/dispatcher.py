"""Plugin 输出调度器 —— 根据 RenderMode 选择输出策略。

三种输出模式：
    - direct: 直接使用 PluginResponse.text 发送给用户
    - llm: 将 PluginResponse.data 委托给引擎做人格化风格生成
    - silent: 无输出，仅执行副作用

是 Plugin 执行流程的最后一环。
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sirius_chat.plugins.models import PluginDefinition, PluginResponse, RenderMode

logger = logging.getLogger(__name__)


class OutputDispatcher:
    """Plugin 输出调度器。

    根据 RenderMode 选择合适的输出策略。
    """

    async def dispatch(
        self,
        result: "PluginResponse",
        definition: "PluginDefinition",
        *,
        engine: Any = None,
        adapter: Any = None,
        group_id: str = "",
        user_id: str = "",
        message_id: str = "",
        **kwargs: Any,
    ) -> str:
        """调度 PluginResponse 的输出。

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
        # 确定实际渲染模式：PluginResponse 可覆盖 plugin.json 的配置
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
        result: "PluginResponse",
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
        result: "PluginResponse",
        definition: "PluginDefinition",
        *,
        engine: Any = None,
        adapter: Any = None,
        group_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> str:
        """LLM 模式：委托引擎做人格化风格生成。

        使用 engine._generate() 而非直接调用 provider，
        确保经过模型路由、token 记录、语气对齐等完整框架链路。
        """
        if engine is None:
            logger.warning("Plugin %s: LLM 模式但无引擎可用，降级到 direct", definition.name)
            return self._handle_direct(result, definition, adapter=adapter, group_id=group_id, user_id=user_id)

        system_prompt = self._build_plugin_system_prompt(result, definition, engine)
        if not system_prompt:
            return self._handle_direct(result, definition, adapter=adapter, group_id=group_id, user_id=user_id)

        try:
            generated = await engine._generate(
                system_prompt=system_prompt,
                messages=[],
                group_id=group_id,
                task_name="plugin_render",
            )
            return generated if generated else result.text
        except Exception as exc:
            logger.error("Plugin %s: 引擎生成失败: %s", definition.name, exc)
            return self._handle_direct(result, definition, adapter=adapter, group_id=group_id, user_id=user_id)

    @staticmethod
    def _build_plugin_system_prompt(
        result: "PluginResponse",
        definition: "PluginDefinition",
        engine: Any,
    ) -> str:
        """构建 Plugin 结果的人格化 system prompt。

        该 prompt 会通过 engine._generate() 注入人格、时间、语气对齐等上下文，
        无需手动拼接 PromptFactory 的完整人格块。
        """
        import json

        persona = getattr(engine, 'persona', None)
        persona_name = getattr(persona, 'name', '') or ''

        data = result.data if result.data else {"text": result.text}
        data_json = json.dumps(data, ensure_ascii=False, indent=2)

        parts: list[str] = []
        if persona_name:
            parts.append(f"【角色：{persona_name}】")

        parts.append("\n【指令执行结果】")
        parts.append(f"你刚刚执行了用户的 '{definition.display_name or definition.name}' 指令，获得以下数据：")
        parts.append(data_json)

        parts.append("\n【表达要求】")
        parts.append("- 请以自然的人格风格向用户传达以上信息")
        if result.mood_hint:
            parts.append(f"- 当前情绪提示：{result.mood_hint}")
        if result.tone_override:
            parts.append(f"- 语气要求：{result.tone_override}")
        if definition.render.system_prompt_suffix:
            parts.append(f"- {definition.render.system_prompt_suffix}")
        parts.append("- 不要暴露这是'执行结果'，要像自己知道的一样自然表达")

        return "\n".join(parts)

    # ── 已删除 _build_stylized_prompt，功能合并到 _build_plugin_system_prompt ──

    # ── 辅助 ──

    @staticmethod
    def _resolve_mode(result: "PluginResponse", definition: "PluginDefinition") -> "RenderMode":
        """确定实际渲染模式。"""
        from sirius_chat.plugins.models import RenderMode

        # PluginResponse 可以覆盖配置
        if result.render_mode:
            mode = result.render_mode.lower()
            if mode == "llm":
                return RenderMode.LLM
            if mode == "silent":
                return RenderMode.SILENT
            return RenderMode.DIRECT
        return definition.get_render_mode()
