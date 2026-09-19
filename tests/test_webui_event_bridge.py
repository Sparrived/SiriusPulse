"""WebUI 引擎事件桥：人格自主过程要能被浏览器看见。

此前 WebUI 的 WebSocket 只传文件变更通知，事件总线没有任何订阅者，
``agent_turn_updated`` 因此成了死信——她自主行动时页面上什么都看不到。
这里从使用者的角度验证：浏览器最终收到了什么。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sirius_pulse.core.events import SessionEvent, SessionEventBus, SessionEventType
from sirius_pulse.webui.event_bridge import EngineEventBridge


class _RecordingWsManager:
    """记录发给浏览器的广播。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def broadcast_to_persona(self, persona_name: str, event_data: dict) -> None:
        self.sent.append((persona_name, event_data))


class _FakeEngine:
    def __init__(self) -> None:
        self.event_bus = SessionEventBus()


class _FakeWorker:
    def __init__(self, engine: _FakeEngine | None) -> None:
        self._runtime = SimpleNamespace(engine=engine)


async def _settle(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


@pytest.mark.asyncio
async def test_agent_turn_reaches_browser_after_engine_starts_later():
    """WebUI 先起来、人格引擎后到，自主回合仍要能被看见。

    run 模式下 workers 是在 WebUI 启动之后才挂上去的，所以桥必须能等到它们。
    """
    ws = _RecordingWsManager()
    workers: dict[str, object] = {}
    bridge = EngineEventBridge(ws, lambda: workers, poll_seconds=0.01)

    bridge.start()
    await _settle()
    assert ws.sent == []

    engine = _FakeEngine()
    workers["sirius"] = _FakeWorker(engine)
    await _settle()

    await engine.event_bus.emit(
        SessionEvent(
            type=SessionEventType.AGENT_TURN_UPDATED,
            data={"origin": "self_initiated", "phase": "complete", "episode": {"kind": "reading"}},
        )
    )
    await _settle()

    assert len(ws.sent) == 1
    persona, payload = ws.sent[0]
    assert persona == "sirius"
    assert payload["type"] == "agent_turn_updated"
    assert payload["persona"] == "sirius"
    assert payload["data"]["origin"] == "self_initiated"

    await bridge.stop()


@pytest.mark.asyncio
async def test_rebuilt_engine_is_resubscribed_even_if_the_old_bus_never_closed():
    """引擎重建会换掉事件总线，桥必须跟过去，否则自主过程又变得不可见。

    重建路径会尝试关闭旧总线，但关闭失败只记一条 warning 就继续
    （platforms/runtime.py 的 _reload_engine_locked）。此时旧订阅还活着，
    所以桥不能靠"订阅自然结束"来发现换栈，必须比对引擎身份。
    """
    ws = _RecordingWsManager()
    first = _FakeEngine()
    workers: dict[str, object] = {"sirius": _FakeWorker(first)}
    bridge = EngineEventBridge(ws, lambda: workers, poll_seconds=0.01)

    bridge.start()
    await _settle()

    await first.event_bus.emit(SessionEvent(type=SessionEventType.AGENT_TURN_UPDATED, data={}))
    await _settle()
    assert len(ws.sent) == 1

    # 旧总线故意不关闭：模拟关闭旧引擎事件总线失败。
    rebuilt = _FakeEngine()
    workers["sirius"] = _FakeWorker(rebuilt)
    await _settle()

    await rebuilt.event_bus.emit(
        SessionEvent(type=SessionEventType.AGENT_TURN_UPDATED, data={"phase": "started"})
    )
    await _settle()

    assert len(ws.sent) == 2
    assert ws.sent[1][1]["data"]["phase"] == "started"

    await bridge.stop()


@pytest.mark.asyncio
async def test_stopped_persona_stops_being_bridged_and_start_stop_are_clean():
    """人格停掉后不该再往浏览器推它的事件；启停也不该留下多余任务。"""
    ws = _RecordingWsManager()
    engine = _FakeEngine()
    workers: dict[str, object] = {"sirius": _FakeWorker(engine)}
    bridge = EngineEventBridge(ws, lambda: workers, poll_seconds=0.01)

    bridge.start()
    await _settle()

    workers.clear()
    await _settle()

    await engine.event_bus.emit(SessionEvent(type=SessionEventType.AGENT_TURN_UPDATED, data={}))
    await _settle()
    assert ws.sent == []

    await bridge.stop()
    bridge.start()
    bridge.start()  # 重复启动不应叠加监管任务
    await _settle()
    await bridge.stop()


@pytest.mark.asyncio
async def test_oversized_event_payload_is_bounded_before_broadcast():
    """一个超长产物不能把整包塞给浏览器。"""
    ws = _RecordingWsManager()
    engine = _FakeEngine()
    bridge = EngineEventBridge(ws, lambda: {"sirius": _FakeWorker(engine)}, poll_seconds=0.01)

    bridge.start()
    await _settle()

    await engine.event_bus.emit(
        SessionEvent(
            type=SessionEventType.AGENT_TURN_UPDATED,
            data={"outcome": "很" * 5000, "kind": "writing"},
        )
    )
    await _settle()

    data = ws.sent[0][1]["data"]
    assert data["kind"] == "writing"
    assert len(data["outcome"]) < 5000

    await bridge.stop()
