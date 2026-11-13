"""会话与记忆：活跃的聊天、最近的消息、写入记忆与群状态。

实现 :class:`sirius_pulse.tools.engine_context.ConversationAccess`。
"""

from __future__ import annotations

from typing import Any


class ConversationMixin:
    """会话与记忆：活跃的聊天、最近的消息、写入记忆与群状态。"""

    _engine: Any

    def get_active_groups(self) -> list[str]:
        return list(self._engine._group_last_message_at.keys())

    def get_recent_messages(self, group_id: str, n: int = 10) -> list[dict[str, Any]]:
        """只读获取某群最近消息，用于为自主行为挑选素材。"""
        getter = getattr(self._engine, "_get_recent_messages", None)
        if not callable(getter):
            return []
        return list(getter(group_id, n))

    async def add_memory_unit(self, unit: Any) -> bool:
        """把一条记忆单元写入人格记忆，供后续对话检索复用。"""
        manager = getattr(self._engine, "memory_unit_manager", None)
        if manager is None:
            return False
        group_id = str(getattr(unit, "group_id", "") or "")
        await manager.add_units(group_id, [unit])
        return True

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

    def schedule_notebook_maintenance(self, group_id: str, *, force: bool = False) -> None:
        manager = getattr(self._engine, "_bg_tasks_mgr", None)
        maintenance = getattr(manager, "notebook_maintenance", None)
        if maintenance is not None:
            maintenance.schedule(group_id, force=force)
