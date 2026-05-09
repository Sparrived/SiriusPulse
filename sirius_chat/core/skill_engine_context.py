"""Concrete SkillEngineContext implementation that adapts the engine to the Protocol."""

from __future__ import annotations

import logging
from typing import Any

from sirius_chat.core.events import SessionEvent, SessionEventType

logger = logging.getLogger(__name__)

_EVENT_TYPE_MAP: dict[str, SessionEventType] = {v.value: v for v in SessionEventType}


class SkillEngineContextImpl:
    """Adapts EmotionalGroupChatEngine to the SkillEngineContext Protocol.

    Passive skills receive this context when their create_background_tasks /
    create_triggers factories are invoked, giving them access to engine
    capabilities without a direct dependency on the engine class.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def skill_registry(self) -> Any:
        return self._engine._skill_registry

    @property
    def skill_executor(self) -> Any:
        return self._engine._skill_executor

    def get_data_store(self, skill_name: str) -> Any:
        executor = self._engine._skill_executor
        if executor is None:
            raise RuntimeError("SkillExecutor 未初始化")
        return executor.get_data_store(skill_name)

    async def generate_text(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        group_id: str,
        task_name: str = "passive_skill",
        **kwargs: Any,
    ) -> str:
        return await self._engine._generate(
            system_prompt, messages, group_id,
            task_name=task_name,
            **kwargs,
        )

    def queue_pending_message(
        self, group_id: str, text: str, adapter_type: str = ""
    ) -> None:
        self._engine._pending_reminders.setdefault(group_id, []).append(
            {"text": text, "adapter_type": adapter_type}
        )

    async def emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        mapped = _EVENT_TYPE_MAP.get(event_type)
        if mapped is None:
            logger.warning("未知事件类型: %s", event_type)
            return
        await self._engine.event_bus.emit(
            SessionEvent(type=mapped, data=data)
        )

    def get_active_groups(self) -> list[str]:
        return list(self._engine._group_last_message_at.keys())

    def get_config_value(self, key: str, default: Any = None) -> Any:
        return self._engine.config.get(key, default)

    def get_persona(self) -> Any:
        return self._engine.persona

    def log_inner_thought(self, text: str) -> None:
        self._engine._log_inner_thought(text)

    def add_memory_entry(
        self, group_id: str, user_id: str, role: str, content: str, speaker_name: str = ""
    ) -> None:
        self._engine.basic_memory.add_entry(
            group_id=group_id,
            user_id=user_id,
            role=role,
            content=content,
            speaker_name=speaker_name,
        )

    def record_reply_timestamp(self, group_id: str) -> None:
        from datetime import datetime, timezone
        self._engine._last_reply_at[group_id] = datetime.now(timezone.utc).timestamp()

    def persist_group_state(self, group_id: str) -> None:
        self._engine._persist_group_state(group_id)

    def get_user_communication_style(self, group_id: str, user_id: str) -> str:
        profile = self._engine.semantic_memory.get_global_user_profile(user_id)
        return getattr(profile, "communication_style", "") if profile else ""

    def get_skill_descriptions(self, caller_is_developer: bool = False) -> str:
        from sirius_chat.core.prompt_factory import PromptFactory
        return PromptFactory.build_skill_descriptions(
            skill_registry=self._engine._skill_registry,
            caller_is_developer=caller_is_developer,
            adapter_type=self._engine._current_adapter_type or None,
        )

    def get_current_adapter_type(self) -> str:
        return self._engine._current_adapter_type

    def activate_private_group(self, group_id: str) -> None:
        self._engine._active_private_groups.add(group_id)
