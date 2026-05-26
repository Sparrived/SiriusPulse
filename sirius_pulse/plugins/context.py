"""Plugin 运行时上下文 —— PluginContext、EngineProxy。

Plugin 通过 PluginContext 安全地访问引擎和平台能力。
引擎能力通过 EngineProxy 代理，平台能力通过 adapter 直接访问
（adapter 是 BaseAdapter 实例，PluginExecutor 在运行时时注入）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# EngineProxy —— 引擎能力的安全代理
# ═══════════════════════════════════════════════════════════════════════

class EngineProxy:
    """引擎代理，暴露 Plugin 可安全调用的引擎能力。

    注意：此代理由 PluginExecutor 在运行时注入，Plugin 不应自行创建。
    """

    def __init__(self) -> None:
        self._engine: Any = None                                 # EmotionalGroupChatEngine 引用
        self._plugin_name: str = ""

    def _bind(self, engine: Any, plugin_name: str) -> None:
        """绑定到实际的引擎实例。"""
        self._engine = engine
        self._plugin_name = plugin_name

    async def generate_text(self, prompt: str, *, group_id: str = "", **kwargs: Any) -> str:
        """调用 Brain.generate_text() 生成人格化文本。

        走完整的框架生成链路：模型路由、token 记录、人格注入、语气对齐。
        """
        return await self._engine.brain.generate_text(
            system_prompt=prompt,
            messages=[],
            group_id=group_id,
            task_name="plugin_generate",
        )

    async def generate_text_analysis(self, prompt: str, *, group_id: str = "", **kwargs: Any) -> str:
        """调用 Brain.generate_text() 使用分析小模型生成结构化分析文本。

        用于 Plugin 内部的轻量分析任务（如事件链摘要、话题标签提取），
        走 plugin_analyze 任务路由，使用更快/更便宜的 analysis_model。
        """
        return await self._engine.brain.generate_text(
            system_prompt=prompt,
            messages=[],
            group_id=group_id,
            task_name="plugin_analyze",
        )

    async def generate_raw(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        messages: list[dict] | None = None,
        inject_persona: bool = False,
        task_name: str = "plugin_raw",
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        json_mode: bool = False,
        return_reasoning: bool = False,
    ) -> str | tuple[str, str]:
        """直接调用 LLM provider，绕过引擎管线，但保留 token 用量追踪。

        不经过 tone_alignment、rhythm_analysis、conversation_depth 等 chat 场景下
        才有意义的步骤，适合 Plugin 中纯编程/分析类文本生成。

        Args:
            prompt: 用户消息（作为 messages 中最后一条 user 消息）
            system_prompt: 系统指令（可选）
            messages: 可选的已有会话历史（不含 system_prompt），将插入到 system_prompt 之后、prompt 之前
            inject_persona: 是否自动在 system_prompt 开头注入当前人格信息
            task_name: 认知任务名，用于模型路由（当 model 未指定时）
            model: 强制指定模型名，优先级高于 task_name 路由
            temperature: 采样温度
            max_tokens: 最大生成 token 数
        """
        if self._engine is None:
            return "[引擎未绑定]"
        provider = getattr(self._engine, "provider_async", None)
        if provider is None:
            return "[未配置 provider]"

        from sirius_pulse.providers.base import GenerationRequest, estimate_generation_request_input_tokens

        # 1. 人格注入（自动拼装到 system_prompt 开头）
        if inject_persona:
            persona = getattr(self._engine, "persona", None)
            if persona:
                persona_lines = []
                name = getattr(persona, "name", "")
                if name:
                    persona_lines.append(f"你当前的角色身份是「{name}」。")
                summary = getattr(persona, "persona_summary", "")
                if summary:
                    persona_lines.append(f"角色简介：{summary}")
                traits = getattr(persona, "personality_traits", [])
                if traits:
                    persona_lines.append(f"性格特征：{'、'.join(traits[:3])}")
                style = getattr(persona, "communication_style", "")
                if style:
                    persona_lines.append(f"沟通风格：{style}")
                if persona_lines:
                    persona_block = "\n".join(persona_lines)
                    system_prompt = f"{persona_block}\n\n{system_prompt}" if system_prompt else persona_block

        # 2. 模型解析：model 参数 > task_name 路由 > 默认
        resolved_model = model
        if resolved_model is None:
            model_router = getattr(self._engine, "model_router", None)
            if model_router is not None:
                cfg = model_router.resolve(task_name)
                resolved_model = cfg.model_name
                # 路由配置中有 temperature/max_tokens 时使用，但保持用户显式传入的优先级
                if temperature == 0.7:  # 用户未显式修改
                    temperature = cfg.temperature
                if max_tokens == 4096:  # 用户未显式修改
                    max_tokens = cfg.max_tokens

        # 3. 构建消息
        msgs: list[dict[str, object]] = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        if messages:
            msgs.extend(messages)
        msgs.append({"role": "user", "content": prompt})

        # 4. 构建 GenerationRequest
        request = GenerationRequest(
            model=resolved_model or "",
            system_prompt="",
            messages=msgs,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=60.0,
            purpose=task_name,
            response_format={"type": "json_object"} if json_mode else None,
        )

        # 5. 估算输入 token
        estimated_input_tokens = estimate_generation_request_input_tokens(request)

        # 6. 调用 provider
        reply = ""
        reasoning = ""
        duration_ms = 0.0
        try:
            t0 = time.perf_counter()
            result = await provider.generate_async(request, return_reasoning=return_reasoning)
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            if return_reasoning:
                reasoning, reply = result
            else:
                reply = result
        except Exception as exc:
            logger.warning("[%s] generate_raw 失败: %s", task_name, exc)
            raise

        # 7. token 用量追踪
        try:
            from sirius_pulse.config import TokenUsageRecord
            from sirius_pulse.providers.base import get_last_generation_usage
            from sirius_pulse.token.utils import estimate_tokens

            output_chars = len(reply)
            estimated_output_tokens = estimate_tokens(reply) if reply else 0
            real_usage = get_last_generation_usage()
            if real_usage and isinstance(real_usage, dict):
                prompt_tokens = int(real_usage.get("prompt_tokens", estimated_input_tokens))
                completion_tokens = int(real_usage.get("completion_tokens", estimated_output_tokens))
                total_tokens = int(real_usage.get("total_tokens", prompt_tokens + completion_tokens))
                estimation_method = "provider_real"
            else:
                prompt_tokens = estimated_input_tokens
                completion_tokens = estimated_output_tokens
                total_tokens = estimated_input_tokens + estimated_output_tokens
                estimation_method = "tiktoken" if estimated_output_tokens > 0 else "char_div4"

            persona_name = getattr(getattr(self._engine, "persona", None), "name", "")
            provider_name = getattr(provider, "_last_provider_name", getattr(provider, "_provider_name", "unknown"))

            record = TokenUsageRecord(
                actor_id="assistant",
                task_name=task_name,
                model=resolved_model or "",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                input_chars=len(system_prompt) + len(prompt) + sum(len(str(m.get("content", ""))) for m in (messages or [])),
                output_chars=output_chars,
                estimation_method=estimation_method,
                retries_used=0,
                persona_name=persona_name,
                group_id="",
                provider_name=provider_name,
                breakdown_json="",
                duration_ms=duration_ms,
                conversation_depth=1,
            )

            records = getattr(self._engine, "token_usage_records", None)
            if records is not None:
                records.append(record)

            token_store = getattr(self._engine, "token_store", None)
            if token_store is not None:
                try:
                    token_store.add(record)
                except Exception:
                    logger.warning("token_store.add() 失败", exc_info=True)
                    pass
        except Exception as exc:
            logger.warning("generate_raw token 追踪异常（不阻断）: %s", exc)

        if return_reasoning:
            return (reasoning, reply)
        return reply

    def get_persona_name(self) -> str:
        """获取当前人格名称。"""
        if self._engine is None:
            return ""
        persona = getattr(self._engine, "persona", None)
        if persona is None:
            return ""
        return getattr(persona, "name", "") or ""

    def get_persona_info(self) -> dict[str, Any]:
        """获取当前人格基本信息。"""
        if self._engine is None:
            return {}
        persona = getattr(self._engine, "persona", None)
        if persona is None:
            return {}
        return {
            "name": getattr(persona, "name", ""),
            "persona_summary": getattr(persona, "persona_summary", ""),
            "personality_traits": getattr(persona, "personality_traits", []),
            "communication_style": getattr(persona, "communication_style", ""),
        }

    def get_engine(self) -> Any:
        """获取原始引擎引用（高级用法，谨慎使用）。"""
        return self._engine

    def emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        """发射引擎事件（用于跨插件通信）。"""
        if self._engine is None:
            return
        try:
            # 依赖引擎内部的 event_bus
            event_bus = getattr(self._engine, "event_bus", None)
            if event_bus is not None:
                from sirius_pulse.core.events import SessionEvent, SessionEventType

                try:
                    evt_type = SessionEventType(event_type)
                except ValueError:
                    evt_type = SessionEventType.CUSTOM
                event = SessionEvent(type=evt_type, data=data)
                # 使用同步方式发射（简化）
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(event_bus.emit(event))
                except RuntimeError:
                    logger.warning("获取 event loop 失败", exc_info=True)
                    pass
        except Exception as exc:
            logger.warning("Plugin %s 发射事件失败: %s", self._plugin_name, exc)


# ═══════════════════════════════════════════════════════════════════════
# PluginDataStore —— 插件独立数据存储
# ═══════════════════════════════════════════════════════════════════════

class PluginDataStore:
    """Plugin 独立的 JSON 文件数据存储。

    每个 Plugin 有独立的 JSON 文件，隔离存储。
    """

    def __init__(self, data_dir: Path, plugin_name: str) -> None:
        from pathlib import Path as _Path

        self._data_dir = _Path(data_dir)
        self._plugin_name = plugin_name
        self._file = self._data_dir / f"_plugin_{plugin_name}_data.json"
        self._cache: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        """从磁盘加载数据。"""
        import json as _json

        if self._file.exists():
            try:
                self._cache = _json.loads(self._file.read_text(encoding="utf-8"))
            except Exception:
                self._cache = {}

    def _save(self) -> None:
        """保存数据到磁盘。"""
        import json as _json

        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._file.write_text(_json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, key: str, default: Any = None) -> Any:
        """读取数据。"""
        return self._cache.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """写入数据并持久化。"""
        self._cache[key] = value
        self._save()

    def delete(self, key: str) -> None:
        """删除数据。"""
        self._cache.pop(key, None)
        self._save()

    def all(self) -> dict[str, Any]:
        """获取所有数据。"""
        return dict(self._cache)


# ═══════════════════════════════════════════════════════════════════════
# PluginContext —— Plugin 执行上下文
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class MessageContext:
    """消息上下文。"""

    group_id: str = ""
    user_id: str = ""
    channel: str = ""
    channel_user_id: str = ""
    message_id: str = ""
    content: str = ""
    speaker_name: str = ""


@dataclass
class PluginContext:
    """Plugin 执行时的完整上下文。

    由 PluginExecutor 在调用 Plugin.execute() 前注入。

    Attributes:
        engine: 引擎代理（EngineProxy），安全调用 _generate() / 事件发射
        adapter: 平台适配器实例（BaseAdapter），直接调用 send_message / API
        message: 当前消息上下文
        data_store: 插件独立数据存储
        config: 插件配置
        plugin_name: 插件名称
    """

    engine: EngineProxy = field(default_factory=EngineProxy)
    adapter: Any = None  # BaseAdapter 实例，由 PluginExecutor 运行时注入
    message: MessageContext = field(default_factory=MessageContext)
    data_store: PluginDataStore | None = None
    config: dict[str, Any] = field(default_factory=dict)
    plugin_name: str = ""

    @property
    def logger(self) -> logging.Logger:
        """获取 Plugin 专用 logger。"""
        return logging.getLogger(f"plugin.{self.plugin_name}")

    @staticmethod
    def create(
        *,
        engine: Any = None,
        adapter: Any = None,
        plugin_name: str = "",
        message: MessageContext | None = None,
        data_store: PluginDataStore | None = None,
        config: dict[str, Any] | None = None,
    ) -> PluginContext:
        """工厂方法：创建 PluginContext 并绑定引擎和适配器。"""
        ctx = PluginContext(
            plugin_name=plugin_name,
            message=message or MessageContext(),
            adapter=adapter,  # 直接赋值，不再通过代理
            data_store=data_store,
            config=config or {},
        )
        if engine is not None:
            ctx.engine._bind(engine, plugin_name)
        return ctx
