"""Concrete ToolEngineContext implementation that adapts the engine to the Protocol."""

from __future__ import annotations

import json
import logging
from typing import Any

from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.core.prompt_factory import PromptFactory
from sirius_pulse.providers.base import ToolCall
from sirius_pulse.tools.models import ToolInvocationContext

logger = logging.getLogger(__name__)

_EVENT_TYPE_MAP: dict[str, SessionEventType] = {v.value: v for v in SessionEventType}


class ToolEngineContextImpl:
    """Adapts EmotionalGroupChatEngine to the ToolEngineContext Protocol.

    Passive tools receive this context when their create_background_tasks /
    create_triggers factories are invoked, giving them access to engine
    capabilities without a direct dependency on the engine class.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        # 组合 UserLookupService
        from sirius_pulse.core.user_lookup import UserLookupService

        self._user_lookup = UserLookupService(
            identity_resolver=engine.identity_resolver,
            user_manager=engine.user_manager,
            engine=engine,
        )

    @property
    def tool_registry(self) -> Any:
        return self._engine._tool_registry

    @property
    def tool_executor(self) -> Any:
        return self._engine._tool_executor

    def get_data_store(self, tool_name: str) -> Any:
        executor = self._engine._tool_executor
        if executor is None:
            raise RuntimeError("ToolExecutor 未初始化")
        return executor.get_data_store(tool_name)

    async def generate_text(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        group_id: str,
        task_name: str = "passive_tool",
        post_process: bool = False,
        **kwargs: Any,
    ) -> str:
        return await self._engine.brain.generate_text(
            system_prompt,
            messages,
            group_id,
            task_name=task_name,
            post_process=post_process,
        )

    async def generate_scheduled_message(
        self,
        *,
        job: dict[str, Any],
        command_output: str,
        group_id: str,
        user_id: str,
        user_name: str,
        adapter_type: str,
        caller_is_developer: bool = False,
    ) -> dict[str, Any]:
        """Run a scheduled message through the same tool-call loop as chat."""
        identity = self._engine.persona.build_system_prompt() if self._engine.persona else ""
        tool_desc = self.get_tool_descriptions(
            caller_is_developer=caller_is_developer, adapter_type=adapter_type
        )
        system_prompt, messages = PromptFactory.build_scheduled_task_sections(
            identity=identity,
            job=job,
            command_output=command_output,
            tool_desc=tool_desc,
        )
        self._engine._tool_executor.set_chat_context(
            group_id=group_id, user_id=user_id, adapter_type=adapter_type
        )
        caller = self._build_caller(user_id, user_name, caller_is_developer)
        invocation_context = ToolInvocationContext(
            caller=caller,
            developer_profiles=[caller] if caller_is_developer else [],
        )
        result = await self._run_tool_loop(
            system_prompt=system_prompt,
            messages=messages,
            group_id=group_id,
            user_id=user_id,
            task_name="proactive_generate",
            adapter_type=adapter_type,
            caller_is_developer=caller_is_developer,
            invocation_context=invocation_context,
        )
        return self._scheduled_result_payload(result) if result else {}

    async def _run_tool_loop(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        group_id: str,
        user_id: str,
        task_name: str,
        adapter_type: str,
        caller_is_developer: bool,
        invocation_context: ToolInvocationContext,
    ) -> Any:
        """Run the normal multi-round tool loop and return the final ChatResult."""
        max_rounds = max(1, int(self.get_config_value("max_tool_rounds", 8)))
        last_result: Any = None

        for _ in range(max_rounds):
            result = await self._engine.brain.chat(
                self._chat_request(
                    group_id=group_id,
                    user_id=user_id,
                    system_prompt=system_prompt,
                    messages=messages,
                    task_name=task_name,
                    adapter_type=adapter_type,
                    caller_is_developer=caller_is_developer,
                )
            )
            last_result = result
            tool_calls = list(getattr(result, "tool_calls", []) or [])
            if not tool_calls:
                break

            messages.append(self._assistant_tool_message(result.raw_text, tool_calls))
            tool_multimodal: list[dict[str, Any]] = []
            for tool_call in tool_calls:
                tool_name = tool_call.function_name
                tool = (
                    self._engine._tool_registry.get(tool_name)
                    if self._engine._tool_registry
                    else None
                )
                if tool is None:
                    tool_content = f"Tool '{tool_name}' not found"
                else:
                    try:
                        params = json.loads(tool_call.function_arguments or "{}")
                    except json.JSONDecodeError:
                        params = {}
                    if not isinstance(params, dict):
                        params = {}
                    try:
                        timeout = float(self.get_config_value("tool_execution_timeout", 30.0))
                    except (TypeError, ValueError):
                        timeout = 30.0
                    executed = await self._engine._tool_executor.execute_async(
                        tool,
                        params,
                        timeout=timeout,
                        invocation_context=invocation_context,
                        max_retries=2 if getattr(tool, "retry_safe", False) else 0,
                    )
                    tool_content = executed.to_model_text()
                    tool_multimodal.extend(
                        {"type": "image_url", "image_url": {"url": block.value}}
                        for block in executed.multimodal_blocks
                    )
                messages.append(
                    {"role": "tool", "tool_call_id": tool_call.id, "content": tool_content}
                )
            if tool_multimodal:
                messages.append({"role": "user", "content": tool_multimodal})

        return last_result

    async def run_autonomous_turn(
        self,
        *,
        kind: str,
        seed: str,
        group_id: str,
        task_name: str = "autonomy_generate",
    ) -> dict[str, Any]:
        """Run one self-initiated turn with no chat session context.

        The turn carries ``self_initiated=True``, which makes the tool executor
        refuse every delivery-capable tool for the whole turn.  Autonomy
        therefore *produces*; whether to *share* anything is a separate decision
        made elsewhere.  No shared executor state is mutated, so this is safe to
        run while a normal reply is in flight.
        """
        identity = self._engine.persona.build_system_prompt() if self._engine.persona else ""
        tool_desc = self.get_tool_descriptions()
        system_prompt, messages = PromptFactory.build_autonomous_turn_sections(
            identity=identity,
            kind=kind,
            seed=seed,
            tool_desc=tool_desc,
        )
        caller = self._build_caller("autonomy", "autonomy", False)
        invocation_context = ToolInvocationContext(caller=caller, self_initiated=True)
        result = await self._run_tool_loop(
            system_prompt=system_prompt,
            messages=messages,
            group_id=group_id,
            user_id="",
            task_name=task_name,
            adapter_type="",
            caller_is_developer=False,
            invocation_context=invocation_context,
        )
        return self._scheduled_result_payload(result) if result else {}

    @staticmethod
    def _scheduled_result_payload(result: Any) -> dict[str, Any]:
        return {
            "text": str(getattr(result, "clean_text", "") or "").strip(),
            "reply_references": list(getattr(result, "reply_references", []) or []),
            "sticker_names": list(getattr(result, "sticker_names", []) or []),
            "poke_user_ids": list(getattr(result, "poke_user_ids", []) or []),
        }

    def _chat_request(self, **kwargs: Any) -> Any:
        from sirius_pulse.core.brain import ChatRequest

        return ChatRequest(enable_tools=True, post_process=True, **kwargs)

    @staticmethod
    def _assistant_tool_message(reply: str, tool_calls: list[ToolCall]) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": reply or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": call.type or "function",
                    "function": {
                        "name": call.function_name,
                        "arguments": call.function_arguments,
                    },
                }
                for call in tool_calls
            ],
        }

    @staticmethod
    def _build_caller(user_id: str, user_name: str, caller_is_developer: bool) -> Any:
        from sirius_pulse.memory.user.unified_models import UnifiedUser

        return UnifiedUser(
            user_id=user_id or "scheduled-task",
            name=user_name or "scheduled-task",
            metadata={"is_developer": caller_is_developer},
        )

    def queue_pending_message(self, group_id: str, text: str, adapter_type: str = "") -> None:
        self._engine._pending_reminders.setdefault(group_id, []).append(
            {"text": text, "adapter_type": adapter_type}
        )

    async def emit_event(self, event_type: str, data: dict[str, Any]) -> bool:
        mapped = _EVENT_TYPE_MAP.get(event_type)
        event_bus = getattr(self._engine, "event_bus", None)
        if mapped is None:
            logger.warning("未知事件类型: %s", event_type)
            return False
        if event_bus is None or bool(getattr(event_bus, "closed", False)):
            return False
        subscriber_count = getattr(event_bus, "subscriber_count", None)
        if isinstance(subscriber_count, int) and subscriber_count <= 0:
            return False
        result = await event_bus.emit(SessionEvent(type=mapped, data=dict(data)))
        return result is not False

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

        # 兼容旧宿主：公共引擎方法不存在时保留原有实现。
        adapter = adapter_type or self._engine._current_adapter_type
        self.queue_pending_message(group_id, text, adapter)
        emitted = await self.emit_event(
            "reminder_triggered",
            {
                "group_id": group_id,
                "reply": text,
                "adapter_type": adapter,
                "reminder_id": event_id,
                "image_path": image_path,
                "reply_references": reply_references or [],
                "sticker_names": sticker_names or [],
                "poke_user_ids": poke_user_ids or [],
            },
        )
        if group_id.startswith("private_"):
            self.activate_private_group(group_id)
        return bool(emitted)

    def get_active_groups(self) -> list[str]:
        return list(self._engine._group_last_message_at.keys())

    def get_recent_messages(self, group_id: str, n: int = 10) -> list[dict[str, Any]]:
        """只读获取某群最近消息，用于为自主行为挑选素材。"""
        getter = getattr(self._engine, "_get_recent_messages", None)
        if not callable(getter):
            return []
        return list(getter(group_id, n))

    def add_memory_unit(self, unit: Any) -> bool:
        """把一条记忆单元写入人格记忆，供后续对话检索复用。"""
        manager = getattr(self._engine, "memory_unit_manager", None)
        if manager is None:
            return False
        group_id = str(getattr(unit, "group_id", "") or "")
        manager.add_units(group_id, [unit])
        return True

    def get_config_value(self, key: str, default: Any = None) -> Any:
        return self._engine.config.get(key, default)

    def get_work_path(self) -> str:
        return str(getattr(self._engine, "work_path", "") or "")

    def get_expressiveness(self) -> float:
        """返回人格表达性（0-1），配置缺失时取中性值。"""
        raw = getattr(self._engine, "expressiveness", None)
        value = getattr(raw, "expressiveness", raw)
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.5

    def get_persona(self) -> Any:
        return self._engine.persona

    def log_inner_thought(self, text: str) -> None:
        self._engine._log_inner_thought(text)

    def add_memory_entry(
        self, group_id: str, user_id: str, role: str, content: str, speaker_name: str = ""
    ) -> None:
        entry = self._engine.basic_memory.add_entry(
            group_id=group_id,
            user_id=user_id,
            role=role,
            content=content,
            speaker_name=speaker_name,
        )
        self._engine.basic_store.append(entry)

    def record_reply_timestamp(self, group_id: str) -> None:
        from datetime import datetime, timezone

        self._engine._last_reply_at[group_id] = datetime.now(timezone.utc).timestamp()

    def persist_group_state(self, group_id: str) -> None:
        self._engine._persist_group_state(group_id)

    def get_tool_descriptions(
        self, caller_is_developer: bool = False, adapter_type: str | None = None
    ) -> str:
        """获取工具描述文本（用于被动工具的 prompt 注入）。"""
        if self._engine._tool_registry is None:
            return ""
        from sirius_pulse.memory.user.unified_models import UnifiedUser
        from sirius_pulse.tools.models import ToolInvocationContext

        caller = UnifiedUser(
            user_id="caller",
            name="caller",
            metadata={"is_developer": caller_is_developer},
        )
        ctx = ToolInvocationContext(caller=caller)
        tools = self._engine._tool_registry.build_tools_list(
            invocation_context=ctx,
            adapter_type=adapter_type or self._engine._current_adapter_type or None,
        )
        if not tools:
            return ""
        # 将 tools 格式转换为文本描述
        lines = []
        for tool in tools:
            func = tool.get("function", {})
            name = func.get("name", "")
            desc = func.get("description", "")
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)

    def get_current_adapter_type(self) -> str:
        return self._engine._current_adapter_type

    def is_qq_bot_group_admin(self, group_id: str) -> bool:
        checker = getattr(self._engine, "is_qq_bot_group_admin", None)
        return bool(checker(group_id)) if callable(checker) else False

    def activate_private_group(self, group_id: str) -> None:
        self._engine._active_private_groups.add(group_id)

    async def send_sticker_by_names(
        self,
        group_id: str,
        names: list[str],
    ) -> dict[str, Any]:
        """Send one sticker chosen from the provided candidate names."""
        return await self._engine._send_stickers_by_names(group_id, names)

    def list_sticker_names(self) -> list[str]:
        """Return sticker names available to the current persona."""
        return list(getattr(self._engine, "_sticker_names", []) or [])

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
        return self._user_lookup.find_by_platform_uid(platform, platform_uid, group_id)

    def find_user_by_name(
        self,
        name: str,
        group_id: str = "",
        *,
        fuzzy: bool = True,
    ) -> dict[str, Any] | None:
        """通过显示名或别名查找用户。"""
        return self._user_lookup.find_by_name(name, group_id, fuzzy=fuzzy)

    def get_user_info(self, user_id: str, group_id: str = "") -> dict[str, Any] | None:
        """获取用户详细信息。"""
        return self._user_lookup.get_info(user_id, group_id)

    def list_users(self, group_id: str = "") -> list[dict[str, Any]]:
        """列出群组中的所有用户。"""
        return self._user_lookup.list_users(group_id)

    def get_bot_id(self) -> str:
        """获取 Bot 自身的 user_id。"""
        return self._user_lookup.get_self_id()

    def get_bot_info(self, group_id: str = "") -> dict[str, Any] | None:
        """获取 Bot 自身的详细信息。"""
        return self._user_lookup.get_self_info(group_id)

    def get_bot_platform_uid(self, platform: str = "") -> str | None:
        """获取 Bot 在指定平台的 UID（如 QQ 号）。

        Args:
            platform: 平台标识（如 "qq_native_sirius_pulse"）。
                      为空时返回当前活跃平台的 UID。
        """
        return self._user_lookup.get_bot_platform_uid(platform)

    def get_bot_platform_uids(self) -> dict[str, str]:
        """获取 Bot 在所有平台的 UID。"""
        return self._user_lookup.get_bot_platform_uids()
