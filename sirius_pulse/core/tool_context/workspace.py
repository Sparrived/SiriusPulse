"""她自己的工作区：人格目录、配置、数据存储、内心日志与事件。

实现 :class:`sirius_pulse.tools.engine_context.WorkspaceAccess`。
"""

from __future__ import annotations

import logging
from typing import Any

from sirius_pulse.core.events import SessionEvent, SessionEventType

logger = logging.getLogger(__name__)

_EVENT_TYPE_MAP: dict[str, SessionEventType] = {v.value: v for v in SessionEventType}


class WorkspaceMixin:
    """她自己的工作区：人格目录、配置、数据存储、内心日志与事件。"""

    _engine: Any

    def get_data_store(self, tool_name: str) -> Any:
        executor = self._engine._tool_executor
        if executor is None:
            raise RuntimeError("ToolExecutor 未初始化")
        return executor.get_data_store(tool_name)

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
