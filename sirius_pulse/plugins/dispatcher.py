"""Plugin 输出调度器 —— 根据 RenderMode 选择输出策略。

三种输出模式：
    - direct: 直接使用 PluginResponse.text 发送给用户
    - llm: 将 PluginResponse.data 委托给引擎做人格化风格生成
    - silent: 无输出，仅执行副作用

是 Plugin 执行流程的最后一环。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sirius_pulse.plugins.models import PluginDefinition, PluginResponse, RenderMode

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DispatchedOutput:
    """调度后的输出结构。"""

    text: str = ""
    message_group: Any = None  # MessageGroup | None


class OutputDispatcher:

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
    ) -> DispatchedOutput:
        """调度 PluginResponse 的输出。返回 DispatchedOutput(text + 可选多模态)。"""
        render_mode = self._resolve_mode(result, definition)

        if render_mode.value == "silent":
            logger.debug("Plugin %s: silent 模式，无输出", definition.name)
            return DispatchedOutput()

        if render_mode.value == "direct":
            return self._handle_direct(result, definition)

        if render_mode.value == "llm":
            return await self._handle_llm(
                result, definition,
                engine=engine, adapter=adapter,
                group_id=group_id, user_id=user_id,
                **kwargs,
            )

        raise ValueError(f"Plugin {definition.name}: 未知 render_mode={render_mode.value}")

    def _handle_direct(
        self,
        result: "PluginResponse",
        definition: "PluginDefinition",
    ) -> DispatchedOutput:
        text = result.text
        if not text and result.data:
            text = str(result.data)
        if not text and not result.success:
            text = f"[{definition.display_name or definition.name}] 执行出错: {result.error}"
        return DispatchedOutput(text=text, message_group=result.message_group)

    async def _handle_llm(
        self,
        result: "PluginResponse",
        definition: "PluginDefinition",
        *,
        engine: Any,
        adapter: Any = None,
        group_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> DispatchedOutput:
        system_prompt = self._build_plugin_system_prompt(result, definition, engine)

        try:
            generated = await engine.brain.generate_text(
                system_prompt=system_prompt,
                messages=[],
                group_id=group_id,
                task_name="plugin_render",
            )
            text = generated if generated else result.text
            return DispatchedOutput(text=text, message_group=result.message_group)
        except Exception as exc:
            logger.error("Plugin %s: 引擎生成失败: %s", definition.name, exc)
            return DispatchedOutput(text=result.text, message_group=result.message_group)

    @staticmethod
    def _build_plugin_system_prompt(
        result: "PluginResponse",
        definition: "PluginDefinition",
        engine: Any,
    ) -> str:
        """构建 Plugin 结果的人格化 system prompt。

        人格 profile 已由 Brain.chat() 默认 pre 步骤自动注入，
        此处只负责 Plugin 特有的业务指令（输出规范 + 表达要求）。
        """
        import json

        data = result.data if result.data else {"text": result.text}
        data_json = json.dumps(data, ensure_ascii=False, indent=2)

        sections: list[str] = []

        # ── 1. 输出规范（人格已由 Brain 注入）──
        from sirius_pulse.core.prompt_factory import PromptFactory
        sections.append(PromptFactory.build_output_spec())

        # ── 2. 插件执行结果 ──
        sections.append("\n【指令执行结果】")
        sections.append(
            f"你刚刚执行了用户的 '{definition.display_name or definition.name}' 指令，获得以下数据："
        )
        sections.append(data_json)

        # ── 3. 表达要求 ──
        expression_lines: list[str] = ["\n【表达要求】"]
        expression_lines.append("- 请以自然的人格风格向用户传达以上信息")
        if result.mood_hint:
            expression_lines.append(f"- 当前情绪提示：{result.mood_hint}")
        if result.tone_override:
            expression_lines.append(f"- 语气要求：{result.tone_override}")
        if definition.render.system_prompt_suffix:
            expression_lines.append(f"- {definition.render.system_prompt_suffix}")
        expression_lines.append("- 不要暴露这是'执行结果'，要像自己知道的一样自然表达")
        sections.append("\n".join(expression_lines))

        return "\n\n".join(sections)

    # ── 已删除 _build_stylized_prompt，功能合并到 _build_plugin_system_prompt ──

    # ── 辅助 ──

    @staticmethod
    def _resolve_mode(result: "PluginResponse", definition: "PluginDefinition") -> "RenderMode":
        """确定实际渲染模式。"""
        from sirius_pulse.plugins.models import RenderMode

        # PluginResponse 可以覆盖配置
        if result.render_mode:
            mode = result.render_mode.lower()
            if mode == "llm":
                return RenderMode.LLM
            if mode == "silent":
                return RenderMode.SILENT
            return RenderMode.DIRECT
        return definition.get_render_mode()
