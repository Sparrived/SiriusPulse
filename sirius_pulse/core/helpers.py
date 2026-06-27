"""Helper methods for EmotionalGroupChatEngine.

重构为组合模式：Helpers 类通过引擎实例访问属性，
基类通过委托方法保持 API 兼容。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from sirius_pulse.core.cognition import extract_keywords
from sirius_pulse.core.identity_resolver import IdentityContext

if TYPE_CHECKING:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

logger = logging.getLogger(__name__)


class Helpers:
    """提供辅助方法的组件类。

    通过引擎实例访问属性，实现组合模式。
    """

    def __init__(self, engine: _EmotionalGroupChatEngineBase) -> None:
        self._engine = engine

    # ==================================================================
    # SKILL integration
    # ==================================================================

    def set_skill_runtime(
        self,
        *,
        skill_registry: Any | None = None,
        skill_executor: Any | None = None,
    ) -> None:
        """Attach SKILL registry and executor to the engine."""
        self._engine._skill_registry = skill_registry
        self._engine._skill_executor = skill_executor
        if hasattr(self._engine, "brain"):
            self._engine.brain.skill_registry = skill_registry
            self._engine.brain.current_adapter_type_fn = (
                lambda: getattr(self._engine, "_current_adapter_type", "") or None
            )
            self._engine.brain.current_admin_allowed_fn = lambda group_id: (
                bool(self._engine.is_qq_bot_group_admin(group_id))
                if hasattr(self._engine, "is_qq_bot_group_admin")
                else False
            )
        if skill_executor is not None:
            from sirius_pulse.core.skill_engine_context import SkillEngineContextImpl

            skill_executor.set_engine_context(SkillEngineContextImpl(self._engine))
        self._register_passive_skills()

    # ==================================================================
    # Plugin integration（v1.2+）
    # ==================================================================

    def set_plugin_runtime(
        self,
        *,
        plugin_registry: Any | None = None,
        plugin_executor: Any | None = None,
        plugin_dispatcher: Any | None = None,
    ) -> None:
        """Attach Plugin registry, executor, and dispatcher to the engine."""
        self._engine._plugin_registry = plugin_registry
        self._engine._plugin_executor = plugin_executor
        self._engine._plugin_dispatcher = plugin_dispatcher

        # 同步更新 CognitionAnalyzer 的 plugin_registry
        if plugin_registry is not None:
            cog = getattr(self._engine, "cognition_analyzer", None)
            if cog is not None:
                cog.plugin_registry = plugin_registry

        # 初始化插件意图匹配器（嵌入向量相似度，用于管线短路合并）
        embedding_client = getattr(self._engine, "_embedding_client", None)
        if plugin_registry is not None and embedding_client is not None:
            from sirius_pulse.core.plugin_intent_matcher import PluginIntentMatcher

            self._engine._plugin_intent_matcher = PluginIntentMatcher(
                embedding_client=embedding_client,
                plugin_registry=plugin_registry,
            )
        else:
            self._engine._plugin_intent_matcher = None

        # 初始化插件意图验证器（轻量 LLM，用于向量匹配后的二次确认）
        brain = getattr(self._engine, "brain", None)
        model_router = getattr(self._engine, "model_router", None)
        if plugin_registry is not None and brain is not None and model_router is not None:
            from sirius_pulse.core.plugin_intent_verifier import PluginIntentVerifier

            self._engine._plugin_intent_verifier = PluginIntentVerifier(
                brain=brain,
                model_router=model_router,
                plugin_registry=plugin_registry,
            )
        else:
            self._engine._plugin_intent_verifier = None

    async def execute_plugin_command(
        self,
        decision: Any,
        message: Any,
        group_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Execute a Plugin command and produce the reply.

        Called from _execution() when decision.strategy == PLUGIN.
        Returns the same dict shape as the normal _execution() path so the
        bridge can handle plugin replies identically to normal replies.
        """
        engine = self._engine
        plugin_name = decision.plugin_intent
        if not plugin_name:
            return {"reply": None, "strategy": "plugin", "error": "no_plugin_name"}

        if not hasattr(engine, "_plugin_registry") or engine._plugin_registry is None:
            return {"reply": None, "strategy": "plugin", "error": "no_registry"}

        definition = engine._plugin_registry.get(plugin_name)
        if definition is None:
            return {"reply": f"[Plugin '{plugin_name}' 未找到]", "strategy": "plugin"}

        logger.info(
            "插件 %s 开始执行: raw_text=%r, slots=%s",
            plugin_name,
            getattr(message, "content", "")[:120],
            {k: (v, type(v).__name__) for k, v in getattr(decision, "plugin_slots", {}).items()},
        )

        # 解析指令
        from sirius_pulse.plugins.lexer import parse_command
        from sirius_pulse.plugins.models import CommandAST

        cmd = parse_command(message.content, definition)
        if cmd is None:
            from sirius_pulse.plugins.models import ArgNode

            # 回退：拿 definition 中的第一个 command name，而不是 plugin_name
            # 因为 plugin_name 和 @command 注册名可能不同
            # （如 plugin_name="chat_analyzer" 但 @command("ca_analyze")）
            fallback_command = plugin_name
            if definition.commands:
                fallback_command = definition.commands[0].name

            cmd = CommandAST(
                command=fallback_command,
                raw_text=message.content,
                kwargs={
                    k: ArgNode(value=v, raw=str(v), type_hint="str")
                    for k, v in decision.plugin_slots.items()
                },
            )
            logger.info(
                "插件 %s 指令解析（自然语言回退）：command=%s, slots=%s",
                plugin_name,
                cmd.command,
                {k: (v.value, type(v.value).__name__) for k, v in cmd.kwargs.items()},
            )
        else:
            logger.info(
                "插件 %s 指令解析（精确匹配）：command=%s, kwargs=%s, args=[%s]",
                plugin_name,
                cmd.command,
                {k: (v.value, type(v.value).__name__) for k, v in cmd.kwargs.items()},
                ", ".join(str(a.value) for a in cmd.args),
            )

        # 确定调用者是否为开发者
        caller_is_developer = False
        if hasattr(engine, "identity_resolver") and hasattr(engine, "user_manager"):
            try:
                platform = getattr(message, "channel", "")
                ext_uid = getattr(message, "channel_user_id", "")
                if platform and ext_uid:
                    # 使用 IdentityResolver 统一解析
                    ctx = IdentityContext(
                        speaker_name=getattr(message, "speaker", "") or "",
                        platform_uid=ext_uid,
                        platform=platform,
                    )
                    resolution = engine.identity_resolver.resolve_with_alias(
                        ctx,
                        engine.user_manager,
                        group_id,
                        profile_manager=getattr(engine, "profile_manager", None),
                    )
                    if resolution.user_id:
                        caller_profile = engine.user_manager.get_user(resolution.user_id, group_id)
                        caller_is_developer = bool(
                            caller_profile and getattr(caller_profile, "is_developer", False)
                        )
            except Exception:
                logger.warning("SKILL 执行上下文组装失败", exc_info=True)
                pass

        # 构建消息上下文
        from sirius_pulse.plugins.context import MessageContext

        msg_ctx = MessageContext(
            group_id=group_id,
            user_id=user_id,
            channel=getattr(message, "channel", ""),
            channel_user_id=getattr(message, "channel_user_id", ""),
            message_id=getattr(message, "message_id", ""),
            content=getattr(message, "content", ""),
            speaker_name=getattr(message, "speaker", ""),
        )

        if engine._plugin_executor is None:
            logger.debug("Plugin 执行器未加载，跳过 _execute_plugin_command")
            return {}

        # 执行 Plugin → list[PluginResponse]
        results = await engine._plugin_executor.execute(
            plugin_name,
            cmd,
            group_id=group_id,
            user_id=user_id,
            caller_is_developer=caller_is_developer,
            adapter=self._get_platform_adapter(),
            engine=engine,
            message_context=msg_ctx,
        )

        # 遍历结果，调度输出（每个 PluginResponse → 框架标准格式）
        partial_replies: list[str] = []
        final_reply: str | None = None
        final_message_group: Any = None
        is_last = False  # 防御性初始化，避免空 results 时变量未定义
        any_success = False  # 是否有任何成功的输出
        last_error: str | None = None  # 最后一个失败的 result.error
        for i, result in enumerate(results):
            is_last = i == len(results) - 1
            if not result.success:
                last_error = result.error or "未知错误"
                if is_last:
                    final_reply = (
                        f"[{definition.display_name or plugin_name}] 执行失败: {last_error}"
                    )
                continue

            any_success = True

            if engine._plugin_dispatcher is not None:
                dispatch_output = await engine._plugin_dispatcher.dispatch(
                    result,
                    definition,
                    engine=engine,
                    group_id=group_id,
                    user_id=user_id,
                )
                if dispatch_output.text is not None:
                    rendered = dispatch_output.text
                else:
                    rendered = ""
                if is_last and dispatch_output.message_group is not None:
                    final_message_group = dispatch_output.message_group
            else:
                rendered = result.text or ""

            if not rendered and not (is_last and final_message_group):
                continue

            if is_last:
                final_reply = rendered
            else:
                partial_replies.append(rendered)

        # 将最终回复录入记忆链（与正常 Pipeline 回复一致，仅成功时记录）
        if final_reply and any_success:
            try:
                _entry = engine.basic_memory.add_entry(
                    group_id=group_id,
                    user_id="assistant",
                    speaker_name=engine.persona.name,
                    role="assistant",
                    content=final_reply,
                )
                engine.basic_store.append(_entry)
            except Exception as exc:
                logger.debug("Plugin 回复录入记忆失败: %s", exc)

        return {
            "reply": final_reply,
            "partial_replies": partial_replies,
            "strategy": "plugin",
            "message_group": final_message_group,
            "error": (
                None
                if any_success
                else (last_error or ("plugin_failed" if results else "no_results"))
            ),
        }

    def _register_passive_skills(self) -> None:
        """Discover passive SKILLs and instantiate their background tasks / triggers."""
        engine = self._engine
        if engine._skill_registry is None:
            return
        from sirius_pulse.core.skill_engine_context import SkillEngineContextImpl

        ctx = SkillEngineContextImpl(engine)
        for skill in engine._skill_registry.passive_skills():
            try:
                # 生命周期：on_load（通过 asyncio.create_task 调度，与后台任务生命周期一致）
                if skill._on_load_factory is not None:
                    try:
                        on_load_coro = skill._on_load_factory(ctx)
                        if on_load_coro is not None and asyncio.iscoroutine(on_load_coro):
                            task = asyncio.create_task(
                                on_load_coro,
                                name=f"passive_skill_on_load_{skill.name}",
                            )
                            engine._bg_tasks.add(task)
                            task.add_done_callback(engine._bg_tasks.discard)
                            logger.info("被动SKILL on_load 已调度: %s", skill.name)
                    except Exception as exc:
                        logger.warning("被动SKILL on_load 失败 (%s): %s", skill.name, exc)

                # 生命周期：注册 on_unload
                if skill._on_unload_factory is not None:
                    engine._passive_skill_unloaders.append((ctx, skill._on_unload_factory))

                if skill._background_task_factory is not None:
                    specs = skill._background_task_factory(ctx)
                    if specs is None:
                        continue
                    if not isinstance(specs, list):
                        specs = [specs]
                    for spec in specs:
                        task = asyncio.create_task(
                            spec.run_loop(lambda: engine._bg_running),
                            name=f"passive_skill_{spec.name}",
                        )
                        engine._passive_skill_tasks[spec.name] = task
                        engine._bg_tasks.add(task)
                        task.add_done_callback(engine._bg_tasks.discard)
                        logger.info(
                            "被动SKILL后台任务已注册: %s (间隔 %.1fs)",
                            spec.name,
                            spec.interval_seconds,
                        )

                if skill._trigger_factory is not None:
                    trigger_specs = skill._trigger_factory(ctx)
                    if trigger_specs is None:
                        continue
                    if not isinstance(trigger_specs, list):
                        trigger_specs = [trigger_specs]
                    for spec in trigger_specs:
                        engine._passive_skill_triggers.setdefault(spec.event_type, []).append(spec)
                        logger.info(
                            "被动SKILL触发器已注册: %s (事件: %s)", spec.name, spec.event_type
                        )
            except Exception as exc:
                logger.warning("注册被动SKILL失败 (%s): %s", skill.name, exc)

        if engine._passive_skill_triggers:
            self._wrap_event_bus_for_triggers()

    def _wrap_event_bus_for_triggers(self) -> None:
        """Wrap event_bus.emit so passive SKILL triggers fire on matching events."""
        engine = self._engine
        original_emit = engine.event_bus.emit
        dispatch = self._dispatch_passive_triggers

        async def _dispatching_emit(event: Any) -> None:
            await original_emit(event)
            try:
                await dispatch(event.type.value, event.data)
            except Exception as exc:
                logger.warning("被动SKILL触发分发失败: %s", exc)

        engine.event_bus.emit = _dispatching_emit  # type: ignore[assignment]

    async def _dispatch_passive_triggers(self, event_type: str, data: dict[str, Any]) -> None:
        """Dispatch registered passive SKILL triggers for the given event type."""
        engine = self._engine
        triggers = engine._passive_skill_triggers.get(event_type)
        if not triggers:
            return
        for spec in triggers:
            try:
                await spec.trigger_func(data)
            except Exception as exc:
                logger.warning("被动SKILL触发器执行失败 (%s): %s", spec.name, exc)

    def get_recent_messages(self, group_id: str, n: int = 10) -> list[dict[str, Any]]:
        """获取最近n条消息。"""
        entries = self._engine.basic_memory.get_all(group_id)[-n:]
        return [
            {
                "user_id": e.user_id,
                "content": e.content,
                "timestamp": e.timestamp,
                "speaker": e.speaker_name or e.user_id,
                "platform_message_id": e.platform_message_id,
            }
            for e in entries
        ]

    def _get_platform_adapter(self) -> Any:
        """获取平台适配器实例。引擎在 add_skill_bridge() 时直接持有。"""
        return getattr(self._engine, "_adapter", None)

    def enhance_topic_relevance(
        self,
        base_score: float,
        message: str,
        group_id: str,
        user_id: str,
    ) -> float:
        """Enhance topic relevance using semantic memory (group + user) + topic window.

        v1.3+: 新增短期话题窗口增强。即使当前消息关键词与 AI 兴趣不重叠，
        但如果与近 N 轮群聊话题的关键词重叠 >= 2 个，也视为话题相关，
        修复"用户B说'评分怎么样'"等跨轮次关联场景的话题跟踪盲区。
        """
        engine = self._engine
        text_lower = (message or "").lower()
        if not text_lower:
            return base_score
        boost = 0.0

        # v1.3+: 短期话题窗口增强 —— 跨轮次话题跟踪
        try:
            msg_kw = extract_keywords(message)
            window = getattr(engine, "_topic_window", {}).get(group_id, [])
            for prev_kw in reversed(window):
                overlap = len(msg_kw & prev_kw)
                if overlap >= 2:
                    boost += 0.12
                    break
                elif overlap == 1:
                    boost += 0.05
                    break
        except Exception:
            pass

        return min(1.0, base_score + boost)

    @staticmethod
    def message_rate_per_minute(recent_msgs: list[dict[str, Any]]) -> float:
        """Estimate messages per minute from recent message timestamps."""
        if len(recent_msgs) < 2:
            return 0.0
        try:
            from datetime import datetime

            timestamps = []
            for m in recent_msgs:
                ts = m.get("timestamp")
                if isinstance(ts, str):
                    timestamps.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
                elif hasattr(ts, "isoformat"):
                    timestamps.append(ts)  # type: ignore[arg-type]
            if len(timestamps) < 2:
                return 0.0
            span_minutes = (max(timestamps) - min(timestamps)).total_seconds() / 60.0
            if span_minutes <= 0:
                return 0.0
            return round((len(timestamps) - 1) / span_minutes, 2)
        except Exception:
            logger.warning("获取情感分数失败", exc_info=True)
            return 0.0

    @staticmethod
    def is_pure_image_message(content: str) -> bool:
        """Check if content contains only image/sticker placeholders with no substantive text.

        Matches: [图片: filename.png], [图片描述：...],
                 [动画表情：...], [动画表情："xxx.jpg"]
        Also matches legacy 【】 format for backward compatibility.
        """
        if not content:
            return False
        cleaned = re.sub(r"[【\[](图片\d*|动画表情)[：:]\s*[^\]】]+[】\]]", "", content).strip()
        return not cleaned

    @staticmethod
    def inject_multimodal_into_user_message(
        messages: list[dict[str, Any]],
        multimodal_inputs: list[dict[str, str]] | None,
    ) -> list[dict[str, Any]]:
        """Convert the last user message's string content into OpenAI multimodal list.

        Supports image URLs (local paths are later converted to base64 data URLs
        by the transport layer in ``prepare_openai_compatible_messages``).
        """
        if not multimodal_inputs:
            return messages
        if not messages:
            return messages

        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                user_msg = dict(messages[i])
                content: list[dict[str, Any]] = [
                    {"type": "text", "text": str(user_msg.get("content", ""))}
                ]
                for item in multimodal_inputs:
                    if item.get("type") == "image":
                        content.append(
                            {"type": "image_url", "image_url": {"url": str(item["value"])}}
                        )
                user_msg["content"] = content
                messages[i] = user_msg
                break
        return messages

    # ==================================================================
    # Token recording & exception classification
    # ==================================================================

    def record_subtask_tokens(
        self,
        task_name: str,
        model_name: str,
        group_id: str,
        request: Any | None = None,
        duration_ms: float = 0.0,
        token_breakdown: dict[str, int] | None = None,
    ) -> None:
        """Record token usage for a sub-task (cognition, diary, etc.)."""
        engine = self._engine
        from sirius_pulse.config import TokenUsageRecord
        from sirius_pulse.providers.base import (
            estimate_generation_request_input_tokens,
            get_last_generation_usage,
        )
        from sirius_pulse.token.utils import PromptTokenBreakdown, estimate_tokens

        real_usage = get_last_generation_usage()
        if real_usage and isinstance(real_usage, dict):
            prompt_tokens = int(real_usage.get("prompt_tokens", 0))
            completion_tokens = int(real_usage.get("completion_tokens", 0))
            total_tokens = int(real_usage.get("total_tokens", prompt_tokens + completion_tokens))
            estimation_method = "provider_real"
        else:
            if request is not None:
                prompt_tokens = estimate_generation_request_input_tokens(request)
            else:
                prompt_tokens = 0
            completion_tokens = 0
            total_tokens = prompt_tokens
            estimation_method = "unknown_subtask"

        # Build breakdown JSON from request if available
        breakdown_json = ""
        if token_breakdown:
            bd = PromptTokenBreakdown(**token_breakdown)
            breakdown_json = bd.to_json()
        elif request is not None:
            system_prompt = getattr(request, "system_prompt", "") or ""
            messages = getattr(request, "messages", []) or []
            sp_total = estimate_tokens(system_prompt)
            um_total = sum(estimate_tokens(str(m.get("content", ""))) for m in messages)
            reply_text = getattr(request, "reply", "") or ""
            out_total = estimate_tokens(reply_text) if reply_text else 0
            breakdown_json = json.dumps(
                {
                    "system_prompt_total": sp_total,
                    "user_message": um_total,
                    "output_total": out_total,
                    "total": sp_total + um_total + out_total,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )

        record = TokenUsageRecord(
            actor_id="assistant",
            task_name=task_name,
            model=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimation_method=estimation_method,
            persona_name=engine.persona.name if engine.persona else "",
            group_id=group_id,
            provider_name=getattr(engine.provider_async, "_provider_name", "unknown"),
            breakdown_json=breakdown_json,
            duration_ms=duration_ms,
        )
        engine.token_usage_records.append(record)
        if engine.token_store is not None:
            try:
                engine.token_store.add(record)
            except Exception:
                pass

    def classify_exception(self, exc: Exception) -> str:
        """Classify an LLM provider exception into a structured error type."""
        msg = str(exc).lower()

        # 优先匹配中文 provider 包装异常
        if "提供商请求异常" in str(exc) or "提供商 http 错误" in str(exc):
            return "provider_error"

        if "timeout" in msg or "timed out" in msg or "socket" in msg:
            return "network_timeout"
        if "rate limit" in msg or "too many requests" in msg or "429" in msg:
            return "rate_limit"
        if (
            "authentication" in msg
            or "api key" in msg
            or "unauthorized" in msg
            or "401" in msg
            or "403" in msg
        ):
            return "auth_error"
        if "context length" in msg or "maximum context" in msg or "too long" in msg:
            return "context_exceeded"
        if "content filter" in msg or "moderation" in msg or "safety" in msg or "blocked" in msg:
            return "content_filter"
        if "500" in msg or "502" in msg or "503" in msg or "504" in msg or "server error" in msg:
            return "server_error"
        if "empty" in msg or "no choices" in msg or "no content" in msg:
            return "empty_response"
        if "connection" in msg or "refused" in msg or "reset" in msg:
            return "network_timeout"
        return "unknown"
