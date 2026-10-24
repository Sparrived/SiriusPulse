"""真实 Agent 生图回调：模型/图生成和 WebSocket 为替身，其余核心组件真实。

批量产物默认只内部返回；Agent 可静默、继续工作或显式选择一个产物外发。
回调工具副作用在持久化 fence 后发生，平台回执不能被非空内容冒充。
"""

from __future__ import annotations

import ast
import asyncio
import base64
import inspect
import json
import socket
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from sirius_pulse.core.emotional_engine import EmotionalGroupChatEngine
from sirius_pulse.core.model_router import _DEFAULT_TASK_REGISTRY, ModelRouter
from sirius_pulse.core.persona_store import PersonaStore
from sirius_pulse.core.tool_engine_context import ToolEngineContextImpl
from sirius_pulse.core.work_mode import WorkModeStore
from sirius_pulse.exceptions import ProviderAuthError
from sirius_pulse.memory.basic.store import BasicMemoryFileStore
from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision
from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter
from sirius_pulse.platforms.runtime import EngineRuntime
from sirius_pulse.providers.base import GenerationResult, ImageGenerationResult, ToolCall
from sirius_pulse.providers.mock import MockProvider
from sirius_pulse.tools.data_store import ToolDataStore
from sirius_pulse.tools.executor import ToolExecutor
from sirius_pulse.tools.models import ToolEngineContext, ToolInvocationContext, ToolPassiveType
from sirius_pulse.tools.registry import ToolRegistry
from sirius_pulse.webui.model_catalog import TASK_LABELS, build_model_catalog

GROUP_ID = "123456789"
USER_ID = "u-1001"
USER_NAME = "阿临"
ADAPTER_TYPE = "napcat"
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKm"
    "MIQAAAABJRU5ErkJggg=="
)
_TOOL_SOURCE = (
    Path(__file__).resolve().parent.parent / "sirius_pulse/tools/builtin/image_generate.py"
)


def _call(name, arguments):
    return GenerationResult(
        content="",
        tool_calls=[
            ToolCall(
                id="agent-selected-action",
                function_name=name,
                function_arguments=json.dumps(arguments, ensure_ascii=False),
            )
        ],
    )


class _AgentProvider(MockProvider):
    def __init__(self):
        super().__init__([])
        self.steps = []
        self.image_requests = []
        self.image_results = []
        self.image_error = None
        self.on_generate = None

    async def generate_async(self, request, return_reasoning=False):
        self.requests.append(request)
        result = self.steps.pop(0) if self.steps else ""
        if callable(result):
            result = result(request)
        if isinstance(result, BaseException):
            raise result
        return result if isinstance(result, GenerationResult) else GenerationResult(content=result)

    async def generate_image_async(self, request):
        self.image_requests.append(request)
        if self.on_generate is not None:
            await self.on_generate()
        if self.image_error is not None:
            raise self.image_error
        image = PNG_BYTES + str(len(self.image_requests)).encode()
        self.image_results.append(image)
        return ImageGenerationResult(image_bytes=image, mime_type="image/png")


class _FakePlatform:
    def __init__(self):
        self.outcomes = []
        self.requests = []
        self.accepted = []
        self.adapter = None
        self.closed = False
        self.before_send = None

    async def send(self, payload):
        request = json.loads(payload)
        if self.before_send is not None:
            self.before_send()
        self.requests.append(request)
        assert request["action"] == "send_group_msg"
        assert request["params"]["group_id"] == int(GROUP_ID)
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome is None:
            return
        if outcome == "ok":
            self.accepted.append(request)
        await self.adapter._dispatch(
            {
                "echo": request["echo"],
                "status": outcome,
                "retcode": 1200 if outcome == "failed" else 0,
                "wording": "rejected" if outcome == "failed" else "",
                "data": {"message_id": len(self.accepted)} if outcome == "ok" else {},
            }
        )


class _Harness:
    def __init__(self, work_path, registry):
        self.work_path = work_path
        self.registry = registry
        self.provider = _AgentProvider()
        self.platform = _FakePlatform()
        self.consumer = None
        self.chat_requests = []
        PersonaStore.save(work_path, PersonaProfile(name="image-verifier"))

    async def start(self):
        self.runtime = EngineRuntime(self.work_path)
        self.engine = EmotionalGroupChatEngine(
            work_path=self.work_path,
            provider_async=self.provider,
            config={
                "pending_message_threshold": 0,
                "max_tool_rounds": 4,
                "tool_execution_timeout": 5,
                "task_enabled": {"memory_extract": False, "event_extract": False},
            },
            persona_db_conn=self.runtime.persona_db.conn,
        )
        self.runtime._engine = self.engine
        original_chat = self.engine.brain.chat

        async def observe_chat(request):
            self.chat_requests.append(request)
            return await original_chat(request)

        self.engine.brain.chat = observe_chat
        self.executor = ToolExecutor(self.work_path)
        self.engine.set_tool_runtime(tool_registry=self.registry, tool_executor=self.executor)
        self.ctx = ToolEngineContextImpl(self.engine)
        self.tool = self.registry.get("image_generate")
        self.spec = self.tool._background_task_factory(self.ctx)[0]
        self.adapter = NapCatAdapter(
            "ws://example.invalid",
            api_timeout=0.01,
            work_path=self.work_path / "napcat",
            config={
                "allowed_group_ids": [GROUP_ID],
                "persona_name": "image-verifier",
                "qq_number": "10001",
                "dispatch_db_path": str(self.work_path / "dispatcher.db"),
                "human_reply_delay_enabled": False,
            },
        )
        self.adapter._engine = self.engine
        self.adapter._running = True
        self.adapter.ws = self.platform
        self.platform.adapter = self.adapter
        # Skip rate-limit time only, retaining actual I/O fence and ACK classification.
        self.adapter.call_api = self.adapter._call_api_inner
        self.executor.set_bridge(ADAPTER_TYPE, SimpleNamespace(adapter=self.adapter))
        self.engine.register_adapter(self.adapter, group_ids=[GROUP_ID], private_user_ids=[])

        async def consume():
            async for event in self.engine.event_bus.subscribe():
                await self.adapter._handle_event(event)

        self.consumer = asyncio.create_task(consume())
        await asyncio.sleep(0)
        assert self.engine.event_bus.subscriber_count == 1

    async def close(self):
        await self.engine.event_bus.close()
        if self.consumer is not None:
            await self.consumer
            self.consumer = None
        if self.adapter._dispatcher is not None:
            self.adapter._dispatcher.close()
        self.adapter._running = False
        assert not self.adapter._pending
        await self.runtime.stop()

    async def restart(self):
        await self.close()
        await self.start()

    async def submit(self, prompt, reference_images=None):
        params = {"prompt": prompt}
        if reference_images is not None:
            params["reference_images"] = reference_images
        return await self.executor.execute_async(
            self.tool,
            params,
            timeout=5,
            invocation_context=(
                ToolInvocationContext(
                    caller=UnifiedUser(user_id=USER_ID, name=USER_NAME),
                    group_id=GROUP_ID,
                    adapter_type=ADAPTER_TYPE,
                )
            ),
        )

    async def send_selected(self, path, callback_id, *, self_initiated=False):
        return await self.executor.execute_async(
            self.registry.get("group_file_exec"),
            {"action": "image", "image_path": str(path)},
            invocation_context=ToolInvocationContext(
                caller=UnifiedUser(user_id=USER_ID, name=USER_NAME),
                group_id=GROUP_ID,
                adapter_type=ADAPTER_TYPE,
                background_event_id=callback_id,
                self_initiated=self_initiated,
            ),
        )

    async def tick(self):
        await self.spec.task_func()

    def jobs(self):
        return ToolDataStore(self.executor.get_data_store("image_generate").store_path).get(
            "jobs", []
        )

    def job(self, index=0):
        return self.jobs()[index]

    def archive(self):
        return BasicMemoryFileStore(self.work_path).read_all(GROUP_ID)

    def receipts(self):
        with sqlite3.connect(self.work_path / "dispatcher.db") as conn:
            return conn.execute(
                "SELECT event_id, status FROM dispatcher_delivery_receipts"
            ).fetchall()


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    loaded = ToolRegistry()
    loaded.load_from_directory(
        tmp_path_factory.mktemp("image-flow-registry") / "tools",
        auto_install_deps=False,
        include_builtin=True,
    )
    return loaded


@pytest_asyncio.fixture
async def harness(tmp_path, registry, monkeypatch):
    def no_network(*_args, **_kwargs):
        raise AssertionError("image callback acceptance must not open a real network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    value = _Harness(tmp_path, registry)
    await value.start()
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", ["", "三个方案先留作内部草稿。"])
async def test_batch_generation_returns_to_real_agent_without_publishing(harness, summary):
    harness.provider.steps = [summary] * 3
    for number in range(3):
        submitted = await harness.submit(f"猫的方案{number}")
        assert submitted.success is True
        assert "已提交" in submitted.to_model_text()
    assert harness.provider.image_requests == []
    for _ in range(4):
        await harness.tick()

    jobs = harness.jobs()
    assert len(jobs) == len(harness.provider.image_requests) == 3
    assert all(job["callback_state"] == "completed" and job["notified"] for job in jobs)
    for index, job in enumerate(jobs):
        assert Path(job["image_path"]).read_bytes() == harness.provider.image_results[index]
    assert harness.platform.requests == []
    assert harness.archive() == []
    assert GROUP_ID not in harness.engine._last_reply_at
    assert len(harness.chat_requests) == 3
    assert all(request.work_mode and not request.post_process for request in harness.chat_requests)
    assert all(request.enable_tools for request in harness.chat_requests)
    assert len(WorkModeStore(harness.work_path).load()) == 3
    await harness.restart()
    await harness.tick()
    assert len(harness.provider.image_requests) == len(harness.provider.requests) == 3
    assert harness.platform.requests == []


@pytest.mark.asyncio
async def test_agent_can_continue_generating_without_automatic_group_reply(harness):
    harness.provider.steps = [
        _call("image_generate", {"prompt": "把第一张改成水彩"}),
        "继续下一步，暂不发群。",
        "",
    ]
    await harness.submit("画一只猫")
    await harness.tick()
    assert len(harness.jobs()) == 2
    assert harness.job(1)["status"] == "pending"
    await harness.tick()
    assert [request.prompt for request in harness.provider.image_requests] == [
        "画一只猫",
        "把第一张改成水彩",
    ]
    assert all(job["callback_state"] == "completed" for job in harness.jobs())
    assert harness.platform.requests == []
    assert harness.archive() == []


@pytest.mark.asyncio
async def test_agent_selects_one_of_three_artifacts_and_does_not_publish_internal_summary(harness):
    harness.provider.steps = [
        "",
        "",
        lambda _request: _call(
            "group_file_exec", {"action": "image", "image_path": harness.job(1)["image_path"]}
        ),
        "我只选择了第二张，剩下的暂存。",
    ]
    for number in range(3):
        await harness.submit(f"猫的方案{number}")

    def before_platform_io():
        assert harness.job(2)["callback_state"] == "running"
        assert harness.job(2)["callback_unsafe"] is True

    harness.platform.before_send = before_platform_io
    for _ in range(3):
        await harness.tick()
    assert len(harness.platform.requests) == len(harness.platform.accepted) == 1
    segments = harness.platform.accepted[0]["params"]["message"]
    assert [segment["type"] for segment in segments] == ["image"]
    encoded = segments[0]["data"]["file"].removeprefix("base64://")
    assert base64.b64decode(encoded) == Path(harness.job(1)["image_path"]).read_bytes()
    assert harness.job(2)["callback_state"] == "completed"
    assert harness.receipts()[0][1] == "sent"
    assert not any("剩下的暂存" in entry.content for entry in harness.archive())
    await harness.restart()
    await harness.tick()
    assert len(harness.platform.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ack", ["ok", "failed", None, "async"], ids=["sent", "rejected", "lost", "async"]
)
async def test_explicit_selected_image_uses_real_platform_receipt_and_stable_replay(harness, ack):
    await harness.submit("画一只猫")
    await harness.tick()
    path = harness.job()["image_path"]
    callback_id = f"image_generate:{harness.job()['id']}"
    harness.platform.outcomes = [ack]
    first = await harness.send_selected(path, callback_id)
    assert first.success is (ack == "ok")
    assert len(harness.platform.requests) == 1
    await harness.restart()
    second = await harness.send_selected(path, callback_id)
    if ack == "failed":
        assert second.success is True
        assert len(harness.platform.requests) == 2
    else:
        assert second.success is (ack == "ok")
        assert len(harness.platform.requests) == 1
    rows = harness.receipts()
    assert len(rows) == 1
    assert rows[0][1] == ("uncertain" if ack in (None, "async") else "sent")


@pytest.mark.asyncio
async def test_post_tool_model_failure_does_not_reexecute_selected_image_after_restart(harness):
    harness.provider.steps = [
        lambda _request: _call(
            "group_file_exec", {"action": "image", "image_path": harness.job()["image_path"]}
        ),
        ProviderAuthError("mock", "模型在工具后认证失败", http_status=401),
    ]
    await harness.submit("画一只猫")
    await harness.tick()
    assert len(harness.platform.accepted) == 1
    assert harness.job()["callback_state"] == "uncertain"
    assert harness.job()["notified"] is False
    count = len(harness.provider.requests)
    await harness.restart()
    await harness.tick()
    assert len(harness.platform.requests) == 1
    assert len(harness.provider.requests) == count


@pytest.mark.asyncio
async def test_completed_state_write_failure_never_replays_callback_side_effects(
    harness, monkeypatch
):
    harness.provider.steps = [
        lambda _request: _call(
            "group_file_exec", {"action": "image", "image_path": harness.job()["image_path"]}
        ),
        "仅发选中的图，不自动发此总结。",
    ]
    await harness.submit("画一只猫")
    store = harness.executor.get_data_store("image_generate")
    save = store.save

    def fail_completion_write():
        if any(job.get("callback_state") == "completed" for job in store.get("jobs", [])):
            raise OSError("disk unavailable at callback commit")
        save()

    monkeypatch.setattr(store, "save", fail_completion_write)
    await harness.tick()
    assert harness.job()["callback_state"] == "running"
    assert harness.job()["callback_unsafe"] is True
    assert len(harness.platform.accepted) == 1
    count = len(harness.provider.requests)
    await harness.tick()
    assert len(harness.platform.requests) == 1
    assert len(harness.provider.requests) == count
    monkeypatch.setattr(store, "save", save)
    await harness.restart()
    await harness.tick()
    assert harness.job()["callback_state"] == "uncertain"
    assert harness.job()["notified"] is False
    assert len(harness.platform.requests) == 1
    assert len(harness.provider.requests) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("busy_kind", ["turn_lock", "queued_user", "tool_chain"])
async def test_current_user_work_defers_callback_until_group_is_idle(harness, busy_kind):
    await harness.submit("先画一只猫")
    if busy_kind == "turn_lock":
        lock = harness.engine.get_agent_turn_lock(GROUP_ID)
        await lock.acquire()
    elif busy_kind == "queued_user":
        harness.engine.delayed_queue.enqueue(
            group_id=GROUP_ID,
            user_id=USER_ID,
            message_content="先回答我的问题",
            strategy_decision=StrategyDecision(strategy=ResponseStrategy.IMMEDIATE),
        )
    else:
        harness.engine.begin_tool_chain(GROUP_ID)
    try:
        await asyncio.wait_for(harness.tick(), timeout=1)
        assert harness.job()["status"] == "done"
        assert harness.job()["callback_state"] == "pending"
        assert harness.job()["callback_attempts"] == 0
        assert harness.provider.requests == []
        assert harness.platform.requests == []
    finally:
        if busy_kind == "turn_lock":
            lock.release()
        elif busy_kind == "queued_user":
            harness.engine.delayed_queue.cancel_all_for_user(GROUP_ID, USER_ID)
        else:
            harness.engine.end_tool_chain(GROUP_ID)
    await harness.tick()
    assert harness.job()["callback_state"] == "completed"
    assert len(harness.provider.image_requests) == len(harness.provider.requests) == 1


@pytest.mark.asyncio
async def test_self_initiated_callback_does_not_gain_delivery_permission(harness):
    await harness.submit("我想自己画一只猫")
    store = harness.executor.get_data_store("image_generate")
    jobs = harness.jobs()
    jobs[0]["self_initiated"] = True
    store.set("jobs", jobs)
    store.save()
    harness.provider.steps = [
        lambda _request: _call(
            "group_file_exec", {"action": "image", "image_path": harness.job()["image_path"]}
        ),
        "保留在本地，不外发。",
    ]
    await harness.tick()
    assert harness.job()["callback_state"] == "completed"
    assert harness.platform.requests == []
    followup = harness.provider.requests[1].messages
    assert any(
        message.get("role") == "tool" and "自主产出的内容先留作" in str(message.get("content"))
        for message in followup
    )
    result = await harness.send_selected(
        harness.job()["image_path"],
        "self-initiated-check",
        self_initiated=True,
    )
    assert result.success is False
    assert harness.platform.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("old_ack", ["ok", None], ids=["legacy_sent", "legacy_unknown"])
@pytest.mark.parametrize("path_prefix", ["", "file://"], ids=["plain_path", "file_uri"])
async def test_legacy_callback_cannot_resend_original_artifact_but_may_send_new_work(
    harness, old_ack, path_prefix
):
    await harness.submit("旧版本画的猫")
    await harness.tick()
    job = harness.job()
    harness.platform.outcomes = [old_ack]
    accepted = await harness.ctx.dispatch_proactive_message(
        group_id=GROUP_ID,
        adapter_type=ADAPTER_TYPE,
        event_id=f"image_generate:{job['id']}:image",
        text="",
        image_path=job["image_path"],
    )
    assert accepted is (old_ack == "ok")
    assert len(harness.platform.requests) == 1
    store = harness.executor.get_data_store("image_generate")
    for key in list(job):
        if key.startswith("callback_"):
            job.pop(key)
    job.update(
        delivery_version=2,
        image_sent=old_ack == "ok",
        notified=False,
        notify_turn_done=True,
        result_text="旧通知绝不能补发",
    )
    store.set("jobs", [job])
    store.save()
    harness.provider.steps = [
        _call(
            "group_file_exec", {"action": "image", "image_path": path_prefix + job["image_path"]}
        ),
        "回调内部总结也不能自动群发",
    ]
    await harness.restart()
    await harness.tick()
    assert harness.job()["callback_state"] == "completed"
    assert len(harness.platform.requests) == 1, "旧图即使模型显式再次选择也不得重复或盲发"
    processed = harness.work_path / "processed-cat.png"
    processed.write_bytes(PNG_BYTES)
    result = await harness.send_selected(processed, f"image_generate:{job['id']}")
    assert result.success is True
    assert len(harness.platform.requests) == 2, "新加工产物仍由Agent自行决定发送"


@pytest.mark.asyncio
async def test_generation_error_only_reaches_internal_callback(harness):
    harness.provider.image_error = RuntimeError("provider 502")
    await harness.submit("画一张不存在的图")
    await harness.tick()
    assert harness.job()["status"] == "failed"
    assert harness.job()["callback_state"] == "completed"
    assert "provider 502" in harness.provider.requests[0].system_prompt
    assert harness.platform.requests == []
    assert harness.archive() == []


@pytest.mark.asyncio
async def test_reference_bytes_and_submission_during_generation_remain_intact(harness):
    reference = harness.work_path / "cat_ref.png"
    reference.write_bytes(PNG_BYTES)

    async def submit_another():
        harness.provider.on_generate = None
        await harness.submit("再画一张猫")

    await harness.submit("照这张图再画", reference_images=[str(reference)])
    harness.provider.on_generate = submit_another
    await harness.tick()
    await harness.tick()
    assert harness.provider.image_requests[0].reference_images == [("01_cat_ref.png", PNG_BYTES)]
    assert len(harness.jobs()) == 2
    assert all(job["callback_state"] == "completed" for job in harness.jobs())
    assert harness.platform.requests == []


def test_the_real_registry_exposes_a_model_visible_periodic_tool(registry):
    tool = registry.get("image_generate")
    assert tool.model_visible is True
    assert tool.is_passive is True
    assert tool.passive_type == ToolPassiveType.PERIODIC
    assert tool._background_task_factory is not None
    assert tool in registry.passive_tools()
    parameters = {param.name: param for param in tool.parameters}
    assert parameters["prompt"].required is True
    assert parameters["reference_images"].required is False
    assert "最多 4 张" in parameters["reference_images"].description
    config = {param.name: param for param in tool.config_parameters}
    assert config["timeout_seconds"].default == 300
    assert config["max_pending_jobs"].default == 3
    schemas = registry.build_tools_list(
        invocation_context=ToolInvocationContext(caller=UnifiedUser(user_id=USER_ID)),
        adapter_type=ADAPTER_TYPE,
    )
    by_name = {schema["function"]["name"]: schema["function"] for schema in schemas}
    assert by_name["image_generate"]["parameters"]["required"] == ["prompt"]


def test_the_model_router_and_webui_catalog_register_the_drawing_task():
    task = ModelRouter().resolve("image_generate")
    assert task.model_name == "image_generate"
    assert task.timeout == 300.0
    assert task.retries == 0
    catalog = build_model_catalog()
    assert catalog["available_models"] == list(_DEFAULT_TASK_REGISTRY)
    assert "image_generate" in catalog["available_models"]
    labels = {choice["value"]: choice["label"] for choice in catalog["model_choices"]}
    assert labels["image_generate"] == f"{TASK_LABELS['image_generate']}（image_generate）"


def test_engine_context_signatures_bind_actual_image_tool_keyword_arguments():
    tree = ast.parse(_TOOL_SOURCE.read_text(encoding="utf-8"))
    for name in ("generate_image", "handle_background_tool_result"):
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == name
        ]
        assert calls
        for call in calls:
            inspect.signature(getattr(ToolEngineContextImpl, name)).bind(
                None,
                **{keyword.arg: None for keyword in call.keywords if keyword.arg},
            )
        assert callable(getattr(ToolEngineContext, name, None))
