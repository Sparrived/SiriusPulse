"""Behavior of the quiet, chat-local notebook maintenance worker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sirius_pulse.core.brain import Brain
from sirius_pulse.core.model_router import ModelRouter
from sirius_pulse.core.notebook_maintenance import NotebookMaintenance
from sirius_pulse.memory.persona_notebook import PersonaNotebookStore
from sirius_pulse.providers.mock import MockProvider
from sirius_pulse.tools.executor import ToolExecutor
from sirius_pulse.tools.models import ToolInvocationContext
from sirius_pulse.tools.registry import ToolRegistry

GROUP = "group-a"
OTHER = "private_other"


def _worker(tmp_path: Path, provider: MockProvider) -> tuple[NotebookMaintenance, Brain]:
    brain = Brain(
        provider_async=provider,
        model_router=ModelRouter(),
        persona=SimpleNamespace(
            name="月白", build_system_prompt=lambda: "角色资料", full_system_prompt="角色资料"
        ),
        config={},
    )
    engine = SimpleNamespace(
        work_path=tmp_path,
        brain=brain,
        config={"persona_notebook_token_budget": 2048},
        is_notebook_maintenance_busy=lambda group_id: False,
        persona=brain.persona,
    )
    worker = NotebookMaintenance(engine)
    worker._last_message_at = -100
    return worker, brain


def _plan(provider: MockProvider) -> None:
    provider.responses.append(json.dumps({"operations": [], "apply_pending": True}))
    provider._queue.append(provider.responses[-1])


def test_organize_is_only_a_request_and_idle_worker_uses_exact_chat(tmp_path: Path):
    registry = ToolRegistry()
    registry.load_from_directory(tmp_path / "tools", include_builtin=True)
    tool = registry.get("persona_notebook")
    assert tool is not None
    executor = ToolExecutor(tmp_path)
    executor.set_engine_context(
        SimpleNamespace(
            get_work_path=lambda: str(tmp_path),
            get_config_value=lambda key, default=None: default,
        )
    )
    invocation = ToolInvocationContext(group_id=GROUP)
    before = PersonaNotebookStore.for_chat(tmp_path, GROUP)
    before.add("待整理的线索")
    PersonaNotebookStore.for_chat(tmp_path, OTHER).add("另一聊天的私密线索")

    result = executor.execute(tool, {"action": "organize"}, invocation_context=invocation)
    assert result.success and result.data["status"] == "maintenance_requested"
    assert result.data["saved"] is False
    assert before.read() == [{"text": "待整理的线索", "id": "entry-000001"}]

    async def run():
        provider = MockProvider([json.dumps({"operations": [], "apply_pending": False})])
        worker, _ = _worker(tmp_path, provider)
        # No pending: an ordinary request must not invent an autonomous cleanup.
        assert (await worker.run(GROUP)).status == "no_pending"
        assert provider.requests == []
        assert (await worker.run(GROUP, force=True)).status == "unchanged"
        request = provider.requests[0]
        assert request.model == "memory_extract"
        assert "待整理的线索" in str(request.messages)
        assert "另一聊天的私密线索" not in str(request.messages)
        assert before.snapshot()["revision"] == 1

    asyncio.run(run())


def test_pending_worker_conflict_does_not_overwrite_new_chat_text(tmp_path: Path):
    store = PersonaNotebookStore.for_chat(tmp_path, GROUP)
    filler = "界" * 2000
    store.add(filler, entry_id="full")
    queued = store.apply_or_enqueue(
        [{"op": "add", "entry_id": "new", "entry": {"text": "待保存的新约定"}}]
    )
    assert queued["status"] == "pending"

    class PausedProvider(MockProvider):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.resume = asyncio.Event()

        async def generate_async(self, request, return_reasoning=False):
            self.started.set()
            await self.resume.wait()
            return await super().generate_async(request, return_reasoning)

    async def run():
        provider = PausedProvider()
        _plan(provider)
        worker, _ = _worker(tmp_path, provider)
        task = asyncio.create_task(worker.run(GROUP))
        await asyncio.wait_for(provider.started.wait(), 1)
        # A foreground edit supersedes the model's snapshot while it is thinking.
        store.operations([{"op": "replace", "entries": [{"text": "前台的新内容", "id": "current"}]}])
        provider.resume.set()
        assert (await asyncio.wait_for(task, 2)).status == "conflict"
        assert store.read() == [{"text": "前台的新内容", "id": "current"}]
        assert store.snapshot()["pending"][0]["request_id"] == queued["request_id"]

    asyncio.run(run())


def test_incoming_chat_cancels_model_without_waiting_for_reply_or_deleting_notes(tmp_path: Path):
    store = PersonaNotebookStore.for_chat(tmp_path, GROUP)
    store.add("旧线索", entry_id="old")

    class SlowProvider(MockProvider):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.finished = asyncio.Event()

        async def generate_async(self, request, return_reasoning=False):
            self.started.set()
            try:
                await asyncio.sleep(60)
            finally:
                self.finished.set()
            return await super().generate_async(request, return_reasoning)

    async def run():
        provider = SlowProvider()
        worker, _ = _worker(tmp_path, provider)
        task = asyncio.create_task(worker.run(GROUP, force=True))
        await asyncio.wait_for(provider.started.wait(), 1)
        worker.on_message(GROUP)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        await asyncio.wait_for(provider.finished.wait(), 1)
        await asyncio.wait_for(worker.wait_closed(), 1)
        assert store.read() == [{"text": "旧线索", "id": "old"}]
        # It never emits a chat reply or reuses the normal response task.
        assert worker._scheduled == {}

    asyncio.run(run())


def test_forced_noop_is_not_an_endless_self_restarting_loop(tmp_path: Path):
    store = PersonaNotebookStore.for_chat(tmp_path, GROUP)
    store.add("保留这条", entry_id="keep")

    async def run():
        provider = MockProvider()
        provider.responses.append(json.dumps({"operations": [], "apply_pending": False}))
        provider._queue.append(provider.responses[-1])
        worker, _ = _worker(tmp_path, provider)
        assert worker.schedule(GROUP, force=True)
        assert worker.schedule(GROUP, force=True)
        task = worker._scheduled[GROUP]
        await asyncio.wait_for(task, 2)
        await asyncio.sleep(0)
        assert len(provider.requests) == 1
        assert worker._forced == set()
        assert store.read() == [{"text": "保留这条", "id": "keep"}]

    asyncio.run(run())
