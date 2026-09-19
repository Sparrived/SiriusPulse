"""Relay persona engine events to WebUI WebSocket clients.

The WebUI WebSocket previously carried only coarse file-change notifications:
nothing ever subscribed to the engine's ``SessionEventBus``.  Events such as
``agent_turn_updated`` were therefore emitted into an empty bus, and
``ToolEngineContextImpl.emit_event`` returned early whenever the subscriber
count was zero — so a self-initiated turn left no real-time trace at all.

This bridge is that missing subscriber.  Under ``sirius-pulse run`` the personas
live in the same process as the WebUI, so it can attach to each engine's bus
directly; no IPC is involved.  Engines may be rebuilt at runtime, which replaces
the bus object, so a supervisor re-attaches whenever the engine identity changes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

LOG = logging.getLogger("sirius.webui.events")

# How often to look for newly started, stopped, or rebuilt persona engines.
# Rebuilds also close the old bus, which ends that listener on its own; this
# poll only bounds how long the gap lasts.
_ATTACH_POLL_SECONDS = 2.0

# Events carry engine-internal payloads.  The WebUI shows a trace, not the whole
# body, so oversized string fields are trimmed instead of shipped to browsers.
_MAX_DATA_CHARS = 4000


def _bounded(data: dict[str, Any]) -> dict[str, Any]:
    """Trim oversized string values from a relayed event payload."""
    bounded: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str) and len(value) > _MAX_DATA_CHARS:
            bounded[key] = value[:_MAX_DATA_CHARS] + "…"
        else:
            bounded[key] = value
    return bounded


def _engine_of(worker: Any) -> Any:
    """Return a worker's live engine, if it has one."""
    runtime = getattr(worker, "_runtime", None)
    if runtime is None:
        return None
    return getattr(runtime, "engine", None)


class EngineEventBridge:
    """Subscribe to every persona engine's event bus and fan out to WebSocket.

    The persona set is read through a provider callable rather than captured at
    construction: under ``sirius-pulse run`` the workers are assigned to the
    WebUI *after* it has started, and engines can be replaced later.
    """

    def __init__(
        self,
        ws_manager: Any,
        persona_provider: Callable[[], Any],
        *,
        poll_seconds: float = _ATTACH_POLL_SECONDS,
    ) -> None:
        self._ws_manager = ws_manager
        self._persona_provider = persona_provider
        self._poll_seconds = poll_seconds
        self._listeners: dict[str, asyncio.Task[None]] = {}
        self._engines: dict[str, Any] = {}
        self._supervisor: asyncio.Task[None] | None = None
        self._running = False

    def start(self) -> None:
        """Begin watching for persona engines.  Idempotent."""
        if self._running:
            return
        self._running = True
        self._supervisor = asyncio.create_task(self._supervise(), name="webui_engine_event_bridge")
        LOG.info("引擎事件桥已启动")

    async def stop(self) -> None:
        """Cancel the supervisor and every attached listener."""
        self._running = False
        tasks: list[asyncio.Task[Any]] = [t for t in self._listeners.values()]
        if self._supervisor is not None:
            tasks.append(self._supervisor)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._supervisor = None
        self._listeners.clear()
        self._engines.clear()
        LOG.info("引擎事件桥已停止")

    # ─── 内部实现 ──────────────────────────────────────────

    async def _supervise(self) -> None:
        while self._running:
            try:
                await self._sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.warning("同步引擎事件订阅失败", exc_info=True)
            await asyncio.sleep(self._poll_seconds)

    async def _sync(self) -> None:
        """Attach to live engines, drop gone ones, and follow rebuilds."""
        workers = self._resolve_workers()
        for name, worker in workers.items():
            engine = _engine_of(worker)
            task = self._listeners.get(name)
            if engine is None:
                if task is not None:
                    self._detach(name)
                continue
            # A rebuild swaps the engine (and its bus) while the old listener is
            # still parked on the retired bus; re-attach rather than wait for it.
            if task is not None and not task.done() and self._engines.get(name) is engine:
                continue
            self._detach(name)
            self._engines[name] = engine
            self._listeners[name] = asyncio.create_task(
                self._listen(name, engine), name=f"webui_engine_events_{name}"
            )
            LOG.info("已订阅人格引擎事件: %s", name)
        for name in list(self._listeners):
            if name not in workers:
                self._detach(name)

    def _detach(self, name: str) -> None:
        task = self._listeners.pop(name, None)
        self._engines.pop(name, None)
        if task is not None and not task.done():
            task.cancel()
        LOG.debug("已取消人格引擎事件订阅: %s", name)

    def _resolve_workers(self) -> dict[str, Any]:
        manager = (
            self._persona_provider() if callable(self._persona_provider) else self._persona_provider
        )
        if not manager:
            return {}
        items = getattr(manager, "items", None)
        if not callable(items):
            return {}
        try:
            return {str(key): value for key, value in items() if value is not None}
        except Exception:
            LOG.debug("读取人格 worker 列表失败", exc_info=True)
            return {}

    async def _listen(self, persona: str, engine: Any) -> None:
        bus = getattr(engine, "event_bus", None)
        if bus is None:
            return
        try:
            async for event in bus.subscribe():
                payload = self._payload(persona, event)
                if payload is not None:
                    await self._ws_manager.broadcast_to_persona(persona, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.warning("人格 %s 的引擎事件订阅中断", persona, exc_info=True)

    @staticmethod
    def _payload(persona: str, event: Any) -> dict[str, Any] | None:
        event_type = str(getattr(getattr(event, "type", None), "value", "") or "")
        if not event_type:
            return None
        data = getattr(event, "data", None)
        return {
            "type": event_type,
            "persona": persona,
            "timestamp": float(getattr(event, "timestamp", 0.0) or 0.0),
            "data": _bounded(data if isinstance(data, dict) else {}),
        }
