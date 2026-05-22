"""Session-level event bus for real-time message delivery.

Provides a pub/sub mechanism so external consumers can subscribe to session
events (new messages, SKILL execution status, errors) without being blocked
by the request-response cycle of ``run_live_message``.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from sirius_pulse.models import Message

logger = logging.getLogger(__name__)


class SessionEventType(enum.Enum):
    """Categories of events emitted during session processing."""

    PERCEPTION_COMPLETED = "perception_completed"
    COGNITION_COMPLETED = "cognition_completed"
    DECISION_COMPLETED = "decision_completed"
    EXECUTION_COMPLETED = "execution_completed"
    DELAYED_RESPONSE_TRIGGERED = "delayed_response_triggered"
    PROACTIVE_RESPONSE_TRIGGERED = "proactive_response_triggered"
    DEVELOPER_CHAT_TRIGGERED = "developer_chat_triggered"
    REMINDER_TRIGGERED = "reminder_triggered"


@dataclass(slots=True)
class SessionEvent:
    """A single event emitted by the engine during session processing.

    Attributes:
        type: The category of the event.
        message: The ``Message`` object, present for message-related events.
        data: Arbitrary metadata (e.g. skill name, error details).
        timestamp: Unix timestamp when the event was created.
    """

    type: SessionEventType
    message: Message | None = None
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class SessionEventBus:
    """Per-session event bus supporting multiple concurrent subscribers.

    Usage::

        bus = SessionEventBus()
        # Subscribe
        async for event in bus.subscribe():
            handle(event)

        # Publish (from engine internals)
        await bus.emit(SessionEvent(type=SessionEventType.PERCEPTION_COMPLETED, data={"message": msg}))

        # Close when the session ends
        await bus.close()
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[SessionEvent | None]] = []
        self._closed = False

    async def emit(self, event: SessionEvent) -> None:
        """Publish an event to all current subscribers."""
        if self._closed:
            return
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("事件总线订阅者队列已满，丢弃事件: %s", event.type.value)

    async def subscribe(self, *, max_queue_size: int = 256) -> AsyncIterator[SessionEvent]:
        """Return an async iterator that yields events as they arrive.

        The iterator terminates when :meth:`close` is called.
        """
        queue: asyncio.Queue[SessionEvent | None] = asyncio.Queue(maxsize=max_queue_size)
        self._subscribers.append(queue)
        try:
            while True:
                event = await queue.get()
                if event is None:
                    # Sentinel — bus has been closed.
                    break
                yield event
        finally:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass  # Already removed by close()

    async def close(self) -> None:
        """Signal all subscribers to stop and clear the subscriber list."""
        self._closed = True
        for queue in self._subscribers:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._subscribers.clear()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def closed(self) -> bool:
        return self._closed
