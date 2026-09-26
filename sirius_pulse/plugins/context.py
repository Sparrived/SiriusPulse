"""Plugin 运行时上下文 —— PluginContext、EngineProxy。

Plugin 通过 PluginContext 安全地访问引擎和平台能力。
引擎能力通过 EngineProxy 代理，平台能力通过 adapter 直接访问
（adapter 是 BaseAdapter 实例，PluginExecutor 在运行时时注入）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
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
        self._engine: Any = None  # EmotionalGroupChatEngine 引用
        self._plugin_name: str = ""
        self._user_lookup: Any = None  # UserLookupService 实例

    def _bind(self, engine: Any, plugin_name: str) -> None:
        """绑定到实际的引擎实例。"""
        self._engine = engine
        self._plugin_name = plugin_name
        # 延迟初始化 UserLookupService
        if engine is not None:
            from sirius_pulse.core.user_lookup import UserLookupService

            self._user_lookup = UserLookupService(
                identity_resolver=getattr(engine, "identity_resolver", None),
                user_manager=getattr(engine, "user_manager", None),
                engine=engine,
            )

    async def generate_text(
        self,
        prompt: str,
        *,
        group_id: str = "",
        messages: list[dict[str, Any]] | None = None,
        task_name: str = "plugin_generate",
        **kwargs: Any,
    ) -> str:
        """调用 Brain.generate_text() 生成人格化文本。

        走完整的框架生成链路：模型路由、token 记录、人格注入、语气对齐。
        ``messages`` 和 ``task_name`` 可供需要保留上下文或专用路由的插件使用，
        但默认值保持旧版 Plugin API 行为。
        """
        return await self._engine.brain.generate_text(
            system_prompt=prompt,
            messages=list(messages or []),
            group_id=group_id,
            task_name=task_name,
        )

    async def generate_text_analysis(
        self, prompt: str, *, group_id: str = "", **kwargs: Any
    ) -> str:
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

    async def dispatch_proactive_message(
        self,
        *,
        group_id: str,
        text: str,
        adapter_type: str = "",
        event_id: str = "",
        image_path: str = "",
        reply_references: list[dict[str, Any]] | None = None,
        sticker_names: list[str] | None = None,
        poke_user_ids: list[str] | None = None,
    ) -> bool:
        """通过框架的主动消息管线发送文本和可选附件。"""
        if self._engine is None:
            return False
        handler = getattr(self._engine, "dispatch_proactive_message", None)
        if callable(handler):
            result = await handler(
                group_id=group_id,
                text=text,
                adapter_type=adapter_type,
                event_id=event_id,
                image_path=image_path,
                reply_references=reply_references,
                sticker_names=sticker_names,
                poke_user_ids=poke_user_ids,
            )
            return result is True
        return False

    def get_active_groups(self) -> list[str]:
        """获取引擎当前已观测到的活跃群组。"""
        if self._engine is None:
            return []
        handler = getattr(self._engine, "get_active_groups", None)
        if callable(handler):
            return list(handler())
        return list(getattr(self._engine, "_group_last_message_at", {}).keys())

    def get_current_adapter_type(self) -> str:
        """获取引擎当前适配器类型；后台任务可能返回空字符串。"""
        if self._engine is None:
            return ""
        return str(getattr(self._engine, "_current_adapter_type", "") or "")

    def resolve_adapter_types(self, group_id: str) -> list[str]:
        """返回配置为处理目标群组的适配器类型。"""
        if self._engine is None:
            return []
        resolver = getattr(self._engine, "resolve_adapter_types", None)
        if not callable(resolver):
            return []
        try:
            resolved = resolver(str(group_id or ""))
        except Exception:
            logger.debug("解析插件主动消息 adapter 路由失败", exc_info=True)
            return []
        if isinstance(resolved, str):
            resolved = [resolved]
        return list(
            dict.fromkeys(str(value).strip() for value in (resolved or []) if str(value).strip())
        )

    async def emit_event(self, event_type: str, data: dict[str, Any]) -> bool:
        """异步发射一个引擎事件，并返回是否成功交给事件总线。"""
        if self._engine is None:
            return False
        event_bus = getattr(self._engine, "event_bus", None)
        if event_bus is None or bool(getattr(event_bus, "closed", False)):
            return False
        subscriber_count = getattr(event_bus, "subscriber_count", None)
        if isinstance(subscriber_count, int) and subscriber_count <= 0:
            return False
        from sirius_pulse.core.events import SessionEvent, SessionEventType

        try:
            mapped_type = SessionEventType(event_type)
        except ValueError:
            mapped_type = SessionEventType.CUSTOM
        result = await event_bus.emit(SessionEvent(type=mapped_type, data=dict(data)))
        return result is not False

    async def generate_raw(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        messages: list[dict] | None = None,
        inject_persona: bool = False,
        task_name: str = "plugin_raw",
        model: str | None = None,
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

        采样参数不在此接口：它们由 AMKR 的任务定义持有，需要调整时改 AMKR 侧。
        """
        if self._engine is None:
            return "[引擎未绑定]"
        provider = getattr(self._engine, "provider_async", None)
        if provider is None:
            return "[未配置 provider]"

        from sirius_pulse.providers.base import (
            GenerationRequest,
            estimate_generation_request_input_tokens,
        )

        # 1. 人格注入（自动拼装到 system_prompt 开头）
        if inject_persona:
            persona = getattr(self._engine, "persona", None)
            if persona:
                persona_prompt = getattr(persona, "full_system_prompt", "").strip()
                name = getattr(persona, "name", "")
                persona_block = "\n".join(
                    item for item in (f"你当前的角色身份是「{name}」。" if name else "", persona_prompt) if item
                )
                if persona_block:
                    system_prompt = (
                        f"{persona_block}\n\n{system_prompt}" if system_prompt else persona_block
                    )

        # 2. 模型解析：model 参数 > task_name 路由 > 默认
        resolved_model = model
        if resolved_model is None:
            model_router = getattr(self._engine, "model_router", None)
            if model_router is not None:
                resolved_model = model_router.resolve(task_name).model_name

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
                reasoning, gen_result = result
                reply = gen_result.content or ""
            else:
                reply = result.content or ""
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
                completion_tokens = int(
                    real_usage.get("completion_tokens", estimated_output_tokens)
                )
                total_tokens = int(
                    real_usage.get("total_tokens", prompt_tokens + completion_tokens)
                )
                estimation_method = "provider_real"
            else:
                prompt_tokens = estimated_input_tokens
                completion_tokens = estimated_output_tokens
                total_tokens = estimated_input_tokens + estimated_output_tokens
                estimation_method = "tiktoken" if estimated_output_tokens > 0 else "char_div4"

            persona_name = getattr(getattr(self._engine, "persona", None), "name", "")
            provider_name = getattr(
                provider, "_last_provider_name", getattr(provider, "_provider_name", "unknown")
            )

            record = TokenUsageRecord(
                actor_id="assistant",
                task_name=task_name,
                model=resolved_model or "",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                input_chars=len(system_prompt)
                + len(prompt)
                + sum(len(str(m.get("content", ""))) for m in (messages or [])),
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

            token_store = getattr(self._engine, "token_store", None)
            if token_store is not None:
                try:
                    token_store.add(record)
                except Exception:
                    logger.warning("token_store.add() 失败", exc_info=True)
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
            "full_system_prompt": getattr(persona, "full_system_prompt", ""),
        }

    def get_engine(self) -> Any:
        """获取原始引擎引用（高级用法，谨慎使用）。"""
        return self._engine

    def get_config_value(self, key: str, default: Any = None) -> Any:
        """读取宿主引擎的只读配置项。"""
        if self._engine is None:
            return default
        return getattr(self._engine, "config", {}).get(key, default)

    # ── 用户查找 API（委托给 UserLookupService）──────────────

    @property
    def user_lookup(self) -> Any:
        """获取用户查找服务。"""
        return self._user_lookup

    def find_user_by_platform_uid(
        self,
        platform: str,
        platform_uid: str,
        group_id: str = "",
    ) -> dict[str, Any] | None:
        """通过平台 UID 查找用户。"""
        if self._user_lookup is None:
            return None
        return self._user_lookup.find_by_platform_uid(platform, platform_uid, group_id)

    def find_user_by_name(
        self,
        name: str,
        group_id: str = "",
        *,
        fuzzy: bool = True,
    ) -> dict[str, Any] | None:
        """通过显示名或别名查找用户。"""
        if self._user_lookup is None:
            return None
        return self._user_lookup.find_by_name(name, group_id, fuzzy=fuzzy)

    def get_user_info(self, user_id: str, group_id: str = "") -> dict[str, Any] | None:
        """获取用户详细信息。"""
        if self._user_lookup is None:
            return None
        return self._user_lookup.get_info(user_id, group_id)

    def list_users(self, group_id: str = "") -> list[dict[str, Any]]:
        """列出群组中的所有用户。"""
        if self._user_lookup is None:
            return []
        return self._user_lookup.list_users(group_id)

    def get_bot_id(self) -> str:
        """获取 Bot 自身的 user_id。"""
        if self._user_lookup is None:
            return "assistant"
        return self._user_lookup.get_self_id()

    def get_bot_info(self, group_id: str = "") -> dict[str, Any] | None:
        """获取 Bot 自身的详细信息。"""
        if self._user_lookup is None:
            return None
        return self._user_lookup.get_self_info(group_id)

    def get_bot_platform_uid(self, platform: str = "") -> str | None:
        """获取 Bot 在指定平台的 UID（如 QQ 号）。

        Args:
            platform: 平台标识（如 "qq_native_sirius_pulse"）。
                      为空时返回当前活跃平台的 UID。
        """
        if self._user_lookup is None:
            return None
        return self._user_lookup.get_bot_platform_uid(platform)

    def get_bot_platform_uids(self) -> dict[str, str]:
        """获取 Bot 在所有平台的 UID。"""
        if self._user_lookup is None:
            return {}
        return self._user_lookup.get_bot_platform_uids()


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
        self._load_error: str | None = None
        self._load()

    def _load(self) -> None:
        """从磁盘加载数据。"""
        import json as _json

        self._load_error = None
        if self._file.exists():
            try:
                loaded = _json.loads(self._file.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("根值不是对象")
                self._cache = loaded
            except Exception as exc:
                self._cache = {}
                self._load_error = f"插件数据文件损坏：{type(exc).__name__}"

    def _save(self, values: dict[str, Any] | None = None) -> None:
        """Atomically persist a candidate cache and commit it in memory."""
        from sirius_pulse.config.file_io import atomic_json_save

        candidate = dict(self._cache if values is None else values)
        atomic_json_save(self._file, candidate)
        self._cache = candidate
        self._load_error = None

    def reload(self) -> None:
        """从磁盘重新加载数据，供配置变更后的插件任务使用。"""
        self._cache = {}
        self._load()

    @property
    def store_path(self) -> Path:
        """获取插件数据文件路径。"""
        return self._file

    @property
    def load_error(self) -> str | None:
        """Return a non-empty error when the persisted JSON was unreadable."""
        return self._load_error

    @property
    def artifact_dir(self) -> Path:
        """获取插件专用附件目录。"""
        path = self._data_dir / "artifacts" / self._plugin_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get(self, key: str, default: Any = None) -> Any:
        """读取数据。"""
        return self._cache.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """写入数据并持久化。"""
        candidate = dict(self._cache)
        candidate[key] = value
        self._save(candidate)

    def update(self, values: dict[str, Any]) -> None:
        """一次原子保存多个插件状态字段。"""
        candidate = dict(self._cache)
        candidate.update(values)
        self._save(candidate)

    def delete(self, key: str) -> None:
        """删除数据。"""
        candidate = dict(self._cache)
        candidate.pop(key, None)
        self._save(candidate)

    def delete_many(self, keys: Iterable[str]) -> None:
        """Delete several keys and persist the resulting state once."""
        candidate = dict(self._cache)
        for key in keys:
            candidate.pop(key, None)
        self._save(candidate)

    def clear(self) -> None:
        """Clear all plugin data, including a previously recorded load error."""
        self._save({})

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

    def get_artifact_dir(self) -> Path:
        """获取 Plugin 专用附件目录，并确保目录存在。"""
        if self.data_store is None:
            raise RuntimeError("PluginDataStore 不可用")
        return self.data_store.artifact_dir

    async def dispatch_proactive_message(
        self,
        *,
        group_id: str,
        text: str,
        adapter_type: str = "",
        event_id: str = "",
        image_path: str = "",
        reply_references: list[dict[str, Any]] | None = None,
        sticker_names: list[str] | None = None,
        poke_user_ids: list[str] | None = None,
    ) -> bool:
        """通过统一消息管线主动发送文本和可选图片。"""
        return await self.engine.dispatch_proactive_message(
            group_id=group_id,
            text=text,
            adapter_type=adapter_type,
            event_id=event_id,
            image_path=image_path,
            reply_references=reply_references,
            sticker_names=sticker_names,
            poke_user_ids=poke_user_ids,
        )

    def get_active_groups(self) -> list[str]:
        """获取当前引擎已观测到的活跃群组。"""
        return self.engine.get_active_groups()

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
