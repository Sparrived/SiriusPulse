from __future__ import annotations

import asyncio

import pytest

from sirius_pulse.adapters.models import ParsedEvent
from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter


class _Engine:
    def __init__(self) -> None:
        self._bot_platform_uids = {}
        self.processed: list[str] = []
        self.processed_messages = []
        self.processed_participants = []
        self.observed: list[str] = []

    async def process_message(self, *, message, participants, group_id):
        self.processed.append(message.speaker or "")
        self.processed_messages.append(message)
        self.processed_participants.append(participants)
        return {"strategy": "silent", "reply": None}

    async def observe_message(self, message, participants, group_id):
        self.observed.append(message.speaker or "")


class _PreviewEngine(_Engine):
    def __init__(self, candidate: dict[str, object]) -> None:
        super().__init__()
        self.candidate = candidate

    def preview_dispatch(self, message, participants, group_id):
        return self.candidate


class _EventBus:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def subscribe(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.closed.set()
        if False:
            yield None


class _EventEngine(_Engine):
    def __init__(self) -> None:
        super().__init__()
        self.event_bus = _EventBus()


def _adapter(tmp_path, persona: str, account: str, engine: _Engine) -> NapCatAdapter:
    adapter = NapCatAdapter(
        "ws://example.invalid",
        work_path=tmp_path / persona,
        config={
            "persona_name": persona,
            "qq_number": account,
            "dispatch_db_path": str(tmp_path / "dispatcher.db"),
            "dispatch_min_reply_interval_seconds": 30,
        },
    )
    adapter._engine = engine
    adapter._publish_group_metadata = lambda group_id, self_id: _async_noop()  # type: ignore[method-assign]
    adapter._get_dispatcher()
    return adapter


async def _async_noop():
    return None


@pytest.mark.asyncio
async def test_stop_handling_cancels_tracked_raw_event_handlers(tmp_path):
    adapter = _adapter(tmp_path, "alpha", "100", _Engine())
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def block_handler(_event):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    adapter.on_event(block_handler)
    await adapter._dispatch({"post_type": "notice"})
    await started.wait()

    await adapter.stop_handling()

    assert cancelled.is_set()
    assert adapter._event_handler_tasks == set()


@pytest.mark.asyncio
async def test_rebind_cancels_and_drains_tracked_event_bus_handlers(tmp_path):
    first_engine = _EventEngine()
    second_engine = _EventEngine()
    adapter = _adapter(tmp_path, "alpha", "100", first_engine)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def block_handler(_event, *, engine=None):
        assert engine is first_engine
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    adapter._handle_event = block_handler  # type: ignore[method-assign]
    adapter._track_event_handler(
        SessionEvent(type=SessionEventType.CUSTOM),
        first_engine,
    )
    await started.wait()

    await adapter.rebind_engine(second_engine)

    assert cancelled.is_set()
    assert adapter._event_handler_tasks == set()
    assert adapter._engine is second_engine


@pytest.mark.asyncio
async def test_stale_dispatch_retry_does_not_process_rebound_engine(tmp_path):
    old_engine = _Engine()
    new_engine = _Engine()
    adapter = _adapter(tmp_path, "alpha", "100", new_engine)
    adapter._running = True
    adapter._engine_generation = 2
    processed = []

    async def fake_process_event(event):
        processed.append(event)
        return True

    adapter._process_event = fake_process_event  # type: ignore[method-assign]
    await adapter._retry_dispatch_event(
        {"post_type": "message"},
        "event-1",
        60.0,
        engine=old_engine,
        generation=1,
    )

    assert processed == []


@pytest.mark.asyncio
async def test_rebind_engine_switches_event_bus_listener(tmp_path):
    first_engine = _EventEngine()
    second_engine = _EventEngine()
    adapter = _adapter(tmp_path, "alpha", "100", first_engine)
    adapter._running = True
    adapter._event_bus_task = asyncio.create_task(adapter._event_bus_listener())

    await first_engine.event_bus.started.wait()
    await adapter.rebind_engine(second_engine)
    await second_engine.event_bus.started.wait()

    assert adapter._engine is second_engine
    assert first_engine.event_bus.closed.is_set()

    await adapter.stop_handling()


def _set_parsed(adapter: NapCatAdapter, parsed: ParsedEvent) -> None:
    async def fake_parse(event):
        return parsed

    adapter.parse_event = fake_parse  # type: ignore[method-assign]


def _parsed(
    account: str,
    message_id: str,
    *,
    at: tuple[str, ...] = (),
    user_id: str = "300",
    prompt: str = "群里说一句",
    event_time: int = 0,
) -> ParsedEvent:
    return ParsedEvent(
        group_id="g1",
        user_id=user_id,
        self_id=account,
        message_type="group",
        prompt=prompt,
        nickname="Alice",
        message_id=message_id,
        event_time=event_time,
        at_user_ids=list(at),
    )


@pytest.mark.asyncio
async def test_parse_poke_notice_as_model_message(tmp_path):
    adapter = NapCatAdapter(
        "ws://example.invalid",
        work_path=tmp_path,
        config={"persona_name": "alpha", "qq_number": "100", "allowed_group_ids": ["g1"]},
    )
    adapter.set_persona_name("alpha")

    parsed = await adapter.parse_event(
        {
            "time": 1720000000,
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "group_id": "g1",
            "user_id": "300",
            "self_id": "100",
            "target_id": "100",
            "sender": {"nickname": "Alice", "card": ""},
        }
    )

    assert parsed is not None
    assert parsed.message_type == "group"
    assert parsed.poke_target_id == "100"
    assert parsed.message_id == "poke-1720000000-300-100"
    assert parsed.event_time == 1720000000
    assert "戳了一下 alpha" in parsed.prompt


@pytest.mark.asyncio
async def test_only_one_persona_processes_a_group_event(tmp_path):
    first_engine = _Engine()
    second_engine = _Engine()
    first = _adapter(tmp_path, "alpha", "100", first_engine)
    second = _adapter(tmp_path, "beta", "200", second_engine)
    _set_parsed(first, _parsed("100", "m1", event_time=1720000000))
    _set_parsed(second, _parsed("200", "m2", event_time=1720000000))

    await asyncio.gather(
        first._process_event_impl({"self_id": "100"}),
        second._process_event_impl({"self_id": "200"}),
    )

    assert first_engine.processed == ["Alice"]
    assert second_engine.processed == []
    assert second_engine.observed == ["Alice"]


@pytest.mark.asyncio
async def test_peer_event_is_recorded_before_dispatch_admission(tmp_path):
    engine = _Engine()
    adapter = _adapter(tmp_path, "beta", "200", engine)
    adapter.plugin_config["peer_ai_ids"] = ["100"]
    _set_parsed(adapter, _parsed("200", "peer-1", user_id="100", event_time=1720000000))
    event = {"self_id": "200"}

    await adapter._process_event_impl(event)

    assert engine.observed == ["Alice"]
    assert engine.processed == []
    assert event["_sirius_peer_transcript_recorded"] is True


def test_dispatch_event_id_aligns_per_account_message_ids_within_time_bucket():
    first = _parsed("100", "m1", event_time=1720000000)
    second = _parsed("200", "m2", event_time=1720000002)
    later = _parsed("200", "m3", event_time=1720000006)

    assert NapCatAdapter._dispatch_event_id(first) == NapCatAdapter._dispatch_event_id(second)
    assert NapCatAdapter._dispatch_event_id(first) != NapCatAdapter._dispatch_event_id(later)


@pytest.mark.asyncio
async def test_targeted_persona_gets_the_next_group_event(tmp_path):
    first_engine = _Engine()
    second_engine = _Engine()
    first = _adapter(tmp_path, "alpha", "100", first_engine)
    second = _adapter(tmp_path, "beta", "200", second_engine)

    _set_parsed(first, _parsed("100", "m1"))
    _set_parsed(second, _parsed("200", "m1"))
    await asyncio.gather(
        first._process_event_impl({"self_id": "100"}),
        second._process_event_impl({"self_id": "200"}),
    )

    _set_parsed(first, _parsed("100", "m2", at=("200",)))
    _set_parsed(second, _parsed("200", "m2", at=("200",)))
    await asyncio.gather(
        first._process_event_impl({"self_id": "100"}),
        second._process_event_impl({"self_id": "200"}),
    )

    assert first_engine.processed == ["Alice"]
    assert second_engine.processed == ["Alice"]


@pytest.mark.asyncio
async def test_text_named_persona_gets_a_soft_dispatch_bonus(tmp_path):
    first_engine = _PreviewEngine(
        {
            "should_reply": True,
            "score": 1.2,
            "reason": "reply_needed",
            "is_mentioned": False,
            "strategy": "delayed",
            "delay_seconds": 12.0,
        }
    )
    second_engine = _PreviewEngine(
        {
            "should_reply": True,
            "score": 1.0,
            "reason": "addressed",
            "is_mentioned": True,
            "strategy": "delayed",
            "delay_seconds": 12.0,
        }
    )
    first = _adapter(tmp_path, "alpha", "100", first_engine)
    second = _adapter(tmp_path, "beta", "200", second_engine)
    _set_parsed(first, _parsed("100", "named-1", prompt="beta，帮我看一下"))
    _set_parsed(second, _parsed("200", "named-1", prompt="beta，帮我看一下"))

    await asyncio.gather(
        first._process_event_impl({"self_id": "100"}),
        second._process_event_impl({"self_id": "200"}),
    )

    assert first_engine.processed == []
    assert first_engine.observed == ["Alice"]
    assert second_engine.processed == ["Alice"]
    assert second_engine.processed_messages[0].dispatch_response_strategy == "delayed"
    assert second_engine.processed_messages[0].dispatch_response_delay_seconds == 12.0


@pytest.mark.asyncio
async def test_registered_peer_account_is_marked_as_other_ai(tmp_path):
    first_engine = _Engine()
    second_engine = _Engine()
    first = _adapter(tmp_path, "alpha", "100", first_engine)
    _adapter(tmp_path, "beta", "200", second_engine)

    _set_parsed(first, _parsed("100", "peer-1", user_id="200", at=("100",)))
    await first._process_event_impl({"self_id": "100"})

    assert first_engine.processed_messages[0].sender_type == "other_ai"
    assert first_engine.processed_participants[0][0].metadata["is_ai"] is True


@pytest.mark.asyncio
async def test_poke_targeted_persona_receives_group_event(tmp_path):
    first_engine = _Engine()
    second_engine = _Engine()
    first = _adapter(tmp_path, "alpha", "100", first_engine)
    second = _adapter(tmp_path, "beta", "200", second_engine)

    _set_parsed(
        first,
        ParsedEvent(
            group_id="g1",
            user_id="300",
            self_id="100",
            message_type="group",
            prompt="戳了一下 beta",
            nickname="Alice",
            message_id="poke-1",
            poke_target_id="200",
        ),
    )
    _set_parsed(
        second,
        ParsedEvent(
            group_id="g1",
            user_id="300",
            self_id="200",
            message_type="group",
            prompt="戳了一下 beta",
            nickname="Alice",
            message_id="poke-1",
            poke_target_id="200",
        ),
    )

    await asyncio.gather(
        first._process_event_impl({"self_id": "100"}),
        second._process_event_impl({"self_id": "200"}),
    )

    assert first_engine.processed == []
    assert second_engine.processed == ["Alice"]
    assert second_engine.processed_messages[0].mentions_current_bot is True


class _DeliverySocket:
    closed = False

    def __init__(self, adapter, outcomes):
        self.adapter = adapter
        self.outcomes = iter(outcomes)
        self.requests = []

    async def send(self, raw):
        import json

        request = json.loads(raw)
        self.requests.append(request)
        outcome = next(self.outcomes)
        if outcome == "unknown":
            return
        await self.adapter._dispatch(
            {
                "echo": request["echo"],
                "status": outcome,
                "retcode": 0 if outcome == "ok" else 100,
                "data": {"message_id": str(len(self.requests))},
            }
        )


def _delivery_harness(tmp_path, *, gid="100", outcomes=("ok",), queue=None, enabled=True):
    from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
    from sirius_pulse.core.engine_persistence import EngineStateStore
    from tests.test_delayed_queue_persistence import _tasks_with_failing_chat

    queue = queue or DelayedResponseQueue()
    tasks, engine = _tasks_with_failing_chat(queue, failures=0)
    engine._state_store = EngineStateStore(tmp_path)
    engine.tick_delayed_queue = tasks.tick_delayed_queue
    engine._delayed_event_emitted = {}
    adapter = NapCatAdapter(
        "ws://example.invalid",
        work_path=tmp_path,
        api_timeout=0.01,
        config={
            "persona_name": "alpha",
            "qq_number": "900",
            "allowed_group_ids": [gid],
            "allowed_private_user_ids": ["200"],
            "group_dispatch_enabled": enabled,
            "human_reply_delay_enabled": False,
            "dispatch_db_path": str(tmp_path / "dispatcher.db"),
        },
    )
    adapter._engine = engine
    adapter._running = True
    adapter.call_api = adapter._call_api_inner  # Bypass only the one-second throttle.
    socket = _DeliverySocket(adapter, outcomes)
    adapter.ws = socket
    return queue, tasks, engine, adapter, socket


def _delivery_item(queue, gid, adapter, content="hello"):
    from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision

    return queue.enqueue(
        gid,
        "u1",
        content,
        StrategyDecision(strategy=ResponseStrategy.IMMEDIATE),
        adapter_type="napcat",
        adapter_route_id=adapter.adapter_route_id,
    )


def _delivery_event(item):
    return SessionEvent(
        type=SessionEventType.DELAYED_RESPONSE_TRIGGERED,
        data={
            "group_id": item.group_id,
            "item_id": item.item_id,
            "adapter_type": "napcat",
            "adapter_route_id": item.adapter_route_id,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("gid,enabled", [("100", True), ("100", False), ("private_200", False)])
async def test_delayed_ack_failure_retries_only_remaining_lines_after_restart(
    tmp_path, gid, enabled
):
    from types import SimpleNamespace

    from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue

    queue, tasks, engine, adapter, socket = _delivery_harness(
        tmp_path, gid=gid, outcomes=("ok", "failed"), enabled=enabled
    )
    engine.brain.chat.side_effect = [
        SimpleNamespace(
            raw_text="first\nsecond", clean_text="first\nsecond", tool_calls=[], reply_references=[]
        )
    ]
    item = _delivery_item(queue, gid, adapter)
    await adapter._handle_event(_delivery_event(item))
    assert len(socket.requests) == 2
    assert item.status == "pending"
    assert queue.snapshot()[0]["delivery"]["results"][0]["delivery_cursor"] == 1
    assert not any(
        call.args[0].data.get("phase") == "complete"
        for call in engine.event_bus.emit.await_args_list
    )
    restored = DelayedResponseQueue()
    restored.restore(engine._state_store.load_delayed_queue())
    _, _, next_engine, next_adapter, next_socket = _delivery_harness(
        tmp_path, gid=gid, queue=restored, enabled=enabled
    )
    await next_adapter._handle_event(_delivery_event(item))
    assert len(next_socket.requests) == 1
    assert "second" in str(next_socket.requests[0])
    next_engine.brain.chat.assert_not_awaited()
    assert restored.snapshot() == []
    assert any(
        call.args[0].data.get("phase") == "complete"
        for call in next_engine.event_bus.emit.await_args_list
    )


@pytest.mark.asyncio
async def test_delayed_unknown_ack_survives_restart_without_replay(tmp_path):
    from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue

    queue, _, engine, adapter, socket = _delivery_harness(tmp_path, outcomes=("unknown",))
    item = _delivery_item(queue, "100", adapter)
    await adapter._handle_event(_delivery_event(item))
    assert item.status == "uncertain"
    assert len(socket.requests) == 1
    restored = DelayedResponseQueue()
    restored.restore(engine._state_store.load_delayed_queue())
    _, _, next_engine, next_adapter, next_socket = _delivery_harness(tmp_path, queue=restored)
    await next_adapter._handle_event(_delivery_event(item))
    assert next_socket.requests == []
    next_engine.brain.chat.assert_not_awaited()
    assert restored.snapshot()[0]["status"] == "uncertain"


@pytest.mark.asyncio
async def test_delayed_busy_defers_then_generation_failure_can_retry_same_id(tmp_path):
    from types import SimpleNamespace

    queue, _, engine, adapter, socket = _delivery_harness(tmp_path)
    item = _delivery_item(queue, "100", adapter)
    dispatcher = adapter._get_dispatcher()
    busy = dispatcher.admit(event_id="busy", group_id="100")
    await adapter._handle_event(_delivery_event(item))
    assert queue.get_pending("100") == [item]
    engine.brain.chat.assert_not_awaited()
    dispatcher.finish(busy.lease_id, sent=False)
    engine.brain.chat.side_effect = [
        RuntimeError("LLM unavailable"),
        SimpleNamespace(raw_text="done", clean_text="done", tool_calls=[], reply_references=[]),
    ]
    await adapter._handle_event(_delivery_event(item))
    assert item.status == "pending"
    await adapter._handle_event(_delivery_event(item))
    assert item.status == "sent"
    assert len(socket.requests) == 1


@pytest.mark.asyncio
async def test_new_input_cannot_claim_cached_payload_or_receipt_of_previous_item(tmp_path):
    from types import SimpleNamespace

    queue, _, engine, adapter, socket = _delivery_harness(tmp_path, outcomes=("failed", "ok", "ok"))
    first = _delivery_item(queue, "100", adapter, "request A")
    engine.brain.chat.side_effect = [
        SimpleNamespace(raw_text=word, clean_text=word, tool_calls=[], reply_references=[])
        for word in ("answer A", "answer B")
    ]
    await adapter._handle_event(_delivery_event(first))
    second = _delivery_item(queue, "100", adapter, "request B")
    assert second.item_id != first.item_id
    await adapter._handle_event(_delivery_event(second))
    await adapter._handle_event(_delivery_event(first))
    assert [request["params"]["message"][0]["data"]["text"] for request in socket.requests] == [
        "answer A",
        "answer B",
        "answer A",
    ]
    assert first.status == second.status == "sent"
    assert engine.brain.chat.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("render_outcome", ["render_failure", "rejected", "unknown", "ok"])
async def test_rich_payload_is_cached_before_io_and_fallback_retries_text_only(
    tmp_path, monkeypatch, render_outcome
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from sirius_pulse.adapters.base import DeliveryUncertainError

    queue, _, engine, adapter, socket = _delivery_harness(tmp_path, outcomes=("failed", "ok", "ok"))
    item = _delivery_item(queue, "100", adapter)
    engine.brain.chat.side_effect = [
        SimpleNamespace(
            raw_text="# title\nbody", clean_text="# title\nbody", tool_calls=[], reply_references=[]
        )
    ]
    engine._record_assistant_message = Mock()

    async def render(*args, **kwargs):
        assert (
            engine._state_store.load_delayed_queue()[0]["delivery"]["results"][0]["reply"]
            == "# title\nbody"
        )
        if render_outcome == "unknown":
            raise DeliveryUncertainError("lost ACK")
        if render_outcome != "ok":
            raise ValueError(
                "render failed"
            ) if render_outcome == "render_failure" else RuntimeError("rejected")
        return {"image_message_id": "42"}

    renderer = AsyncMock(side_effect=render)
    monkeypatch.setattr(
        "sirius_pulse.platforms.onebot_v11.napcat.adapter._markdown_image.render_and_send_rich_reply",
        renderer,
    )
    await adapter._handle_event(_delivery_event(item))
    if render_outcome == "unknown":
        assert item.status == "uncertain"
        assert socket.requests == []
    elif render_outcome == "ok":
        assert item.status == "sent"
        assert socket.requests == []
        engine._record_assistant_message.assert_called_once()
    else:
        assert item.status == "pending"
        engine._record_assistant_message.assert_not_called()
        await adapter._handle_event(_delivery_event(item))
        assert item.status == "sent"
        assert len(socket.requests) == 3
        engine._record_assistant_message.assert_called_once()
    renderer.assert_awaited_once()
    engine.brain.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_delayed_event_does_not_take_a_lease_while_callback_selects_an_image(
    tmp_path, monkeypatch
):
    import socket

    from sirius_pulse.tools.registry import ToolRegistry
    from tests.test_image_generate_flow import GROUP_ID, PNG_BYTES, _call, _Harness

    def no_network(*_args, **_kwargs):
        raise AssertionError("callback lease test must not connect to the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    registry = ToolRegistry()
    registry.load_from_directory(tmp_path / "tools", auto_install_deps=False, include_builtin=True)
    harness = _Harness(tmp_path, registry)
    await harness.start()
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    callback = None
    try:
        image_path = tmp_path / "selected.png"
        image_path.write_bytes(PNG_BYTES)
        harness.provider.steps = [
            _call("group_file_exec", {"action": "image", "image_path": str(image_path)}),
            "仅内部保留：已分享选中的图片。",
            "收到，接着处理你的新消息。",
        ]
        original_chat = harness.engine.brain.chat

        async def paused_chat(request):
            if not callback_started.is_set():
                callback_started.set()
                await release_callback.wait()
            return await original_chat(request)

        harness.engine.brain.chat = paused_chat
        callback = asyncio.create_task(
            harness.ctx.handle_background_tool_result(
                callback_id="image_generate:lease-callback",
                tool_name="image_generate",
                outcome="success",
                artifact_path=str(image_path),
                group_id=GROUP_ID,
                user_id="u1",
                adapter_type="napcat",
            )
        )
        await asyncio.wait_for(callback_started.wait(), timeout=1)
        assert harness.engine.get_agent_turn_lock(GROUP_ID).locked()
        item = _delivery_item(harness.engine.delayed_queue, GROUP_ID, harness.adapter)
        harness.engine._delayed_event_emitted[GROUP_ID] = {item.item_id}
        dispatcher = harness.adapter._get_delivery_dispatcher()
        # Inbound admission retains its lease until the queued reply takes over.
        inbound = dispatcher.admit(event_id="new-user-message", group_id=GROUP_ID)
        assert inbound.granted
        harness.adapter._dispatch_leases[GROUP_ID] = inbound.lease_id
        await asyncio.wait_for(harness.adapter._handle_event(_delivery_event(item)), timeout=0.5)
        assert harness.engine.delayed_queue.get_pending(GROUP_ID) == [item]
        assert harness.engine.delayed_queue.in_flight_ids(GROUP_ID) == []
        assert item.item_id not in harness.engine._delayed_event_emitted[GROUP_ID]
        assert not dispatcher.active_lease(GROUP_ID)
        assert GROUP_ID not in harness.adapter._dispatch_leases
        assert harness.platform.requests == []

        release_callback.set()
        result = await asyncio.wait_for(callback, timeout=2)
        assert result["status"] == "completed"
        assert len(harness.platform.accepted) == 1
        assert harness.platform.accepted[0]["params"]["message"][0]["type"] == "image"
        assert any(
            "image_generate:lease-callback:image:" in event_id and status == "sent"
            for event_id, status in harness.receipts()
        )

        await asyncio.wait_for(harness.adapter._handle_event(_delivery_event(item)), timeout=2)
        assert item.status == "sent"
        assert harness.engine.delayed_queue.snapshot() == []
        assert len(harness.platform.accepted) == 2
        assert harness.platform.accepted[1]["params"]["message"][0]["type"] == "text"
        assert "新消息" in harness.platform.accepted[1]["params"]["message"][0]["data"]["text"]
    finally:
        release_callback.set()
        if callback is not None:
            if not callback.done():
                callback.cancel()
            await asyncio.gather(callback, return_exceptions=True)
        await harness.close()
