"""后台生图的端到端业务链路（从用户要图到图片落进群聊）。

这不是工具内部的单元测试，而是把真实组件接到一起跑一遍：真实的
``ToolRegistry``（含内置工具）、真实的 ``ToolExecutor``（真实数据存储与 bridge）、
真实的 ``ToolEngineContextImpl``（真实工作模式轨迹与工具循环），只把模型、平台
适配器和引擎的主动消息投递换成记录器。

业务视角：
- 用户要一张图 → 模型调 ``image_generate`` 立刻拿到"已提交"，不阻塞回复；
- 后台轮询真的向 AMKR 的 ``image_generate`` 任务要到图，落成人格目录里的文件；
- "后台任务完成"回合把绝对路径交给模型，模型用 ``group_file_exec`` 把图发回原群，
  再经主动消息通道补一句自然的话；
- 画失败要如实说，失败通知重试三次后降级只发图；
- 参考图按原始字节送进生图请求；重启残留任务恢复后仍会补通知；生成期间新入队的
  任务不会被写回覆盖。
"""

from __future__ import annotations

import ast
import base64
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sirius_pulse.core.model_router import _DEFAULT_TASK_REGISTRY, ModelRouter
from sirius_pulse.core.tool_engine_context import ToolEngineContextImpl
from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.providers.base import (
    ImageGenerationRequest,
    ImageGenerationResult,
    ToolCall,
)
from sirius_pulse.tools.executor import ToolExecutor
from sirius_pulse.tools.models import (
    ToolEngineContext,
    ToolInvocationContext,
    ToolPassiveType,
)
from sirius_pulse.tools.registry import ToolRegistry
from sirius_pulse.webui.model_catalog import TASK_LABELS, build_model_catalog

GROUP_ID = "123456789"
USER_ID = "u-1001"
USER_NAME = "阿临"
ADAPTER_TYPE = "napcat"

#: 真实可解码的 1×1 PNG。落盘与 base64 图片段都按原始字节断言，所以用真图。
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKm"
    "MIQAAAABJRU5ErkJggg=="
)

_NOTIFICATION_TEXT = "画好啦，快看～"
_HONEST_FAILURE_TEXT = "抱歉，这次没画成。"
_TOOL_SOURCE = (
    Path(__file__).resolve().parent.parent / "sirius_pulse/tools/builtin/image_generate.py"
)


# ── 真实组件之外的三处替身：模型、平台适配器、引擎主动投递 ─────────────────


def _reply(text: str) -> SimpleNamespace:
    """模型的一轮纯文本回复（工具循环据此收尾）。"""
    return SimpleNamespace(
        raw_text=text,
        clean_text=text,
        tool_calls=[],
        reply_references=[],
        sticker_names=[],
        poke_user_ids=[],
    )


def _tool_call(name: str, arguments: dict[str, Any]) -> SimpleNamespace:
    """模型的一轮工具调用回合。"""
    return SimpleNamespace(
        raw_text="",
        clean_text="",
        tool_calls=[
            ToolCall(
                id="call-1",
                function_name=name,
                function_arguments=json.dumps(arguments, ensure_ascii=False),
            )
        ],
        reply_references=[],
        sticker_names=[],
        poke_user_ids=[],
    )


class _RecordingBrain:
    """模型替身：生图走 ``generate_image_async``，通知回合走 ``chat``。

    通知回合的行为按真实模型的用法模拟：成功时第一轮按系统提示里给出的绝对路径
    调 ``group_file_exec`` 发图，第二轮补一句正文；失败时直接如实说明没画成。
    """

    def __init__(self) -> None:
        self.image_requests: list[ImageGenerationRequest] = []
        self.chat_requests: list[Any] = []
        self.image_bytes = PNG_BYTES
        self.image_error: Exception | None = None
        self.notification_error: Exception | None = None
        self.forget_to_send_image = False
        self.on_generate: Any = None

    async def generate_image_async(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        self.image_requests.append(request)
        if self.on_generate is not None:
            await self.on_generate()
        if self.image_error is not None:
            raise self.image_error
        return ImageGenerationResult(image_bytes=self.image_bytes, mime_type="image/png")

    async def chat(self, request: Any) -> SimpleNamespace:
        self.chat_requests.append(request)
        if self.notification_error is not None:
            raise self.notification_error
        if self.forget_to_send_image:
            # 模型偷懒：只写了正文，没按提示调用 group_file_exec 发图。
            return _reply(_NOTIFICATION_TEXT)
        if any(message.get("role") == "tool" for message in request.messages):
            return _reply(_NOTIFICATION_TEXT)
        match = re.search(r"^生成的文件：(.+)$", request.system_prompt, re.MULTILINE)
        if match is None:
            return _reply(_HONEST_FAILURE_TEXT)
        return _tool_call(
            "group_file_exec",
            {"action": "image", "image_path": match.group(1).strip()},
        )


class _RecordingAdapter:
    """平台适配器替身：记录真正发出去的消息段。"""

    def __init__(self) -> None:
        self.group_messages: list[tuple[str, Any]] = []
        self.private_messages: list[tuple[str, Any]] = []

    async def send_group_msg(self, group_id: str, message: Any) -> dict[str, Any]:
        self.group_messages.append((str(group_id), message))
        return {"data": {"message_id": 1001}}

    async def send_private_msg(self, user_id: str, message: Any) -> dict[str, Any]:
        self.private_messages.append((str(user_id), message))
        return {"data": {"message_id": 1002}}


class _FakeEngine:
    """引擎替身：提供工具上下文所需的属性与主动消息投递入口。"""

    def __init__(
        self,
        work_path: Path,
        registry: ToolRegistry,
        executor: ToolExecutor,
        brain: _RecordingBrain,
    ) -> None:
        self.identity_resolver = None
        self.user_manager = None
        self.work_path = work_path
        self._tool_registry = registry
        self._tool_executor = executor
        self.brain = brain
        self.config = {"max_tool_rounds": 4, "tool_execution_timeout": 5}
        self.persona = None
        self._current_adapter_type = ADAPTER_TYPE
        self._pending_reminders: dict[str, list[dict[str, Any]]] = {}
        self._group_last_message_at = {GROUP_ID: "2026-10-19T12:00:00+00:00"}
        self.dispatched: list[dict[str, Any]] = []

    async def dispatch_proactive_message(self, **kwargs: Any) -> bool:
        self.dispatched.append(kwargs)
        return bool(kwargs.get("text") or kwargs.get("image_path"))


class _Harness:
    """把真实注册表 / 执行器 / 工具上下文接到一起，并保留记录用替身。"""

    def __init__(self, work_path: Path, registry: ToolRegistry) -> None:
        self.work_path = work_path
        self.executor = ToolExecutor(work_path)
        self.adapter = _RecordingAdapter()
        self.executor.set_bridge(ADAPTER_TYPE, SimpleNamespace(adapter=self.adapter))
        self.brain = _RecordingBrain()
        self.engine = _FakeEngine(work_path, registry, self.executor, self.brain)
        self.ctx = ToolEngineContextImpl(self.engine)
        self.tool = registry.get("image_generate")
        assert self.tool is not None, "真实注册表里没有加载到 image_generate"
        self.tasks = self.tool._background_task_factory(self.ctx)
        self.spec = self.tasks[0]

    async def submit(self, prompt: str, reference_images: list[str] | None = None) -> Any:
        """模拟模型按用户请求调用 image_generate 工具。"""
        params: dict[str, Any] = {"prompt": prompt}
        if reference_images is not None:
            params["reference_images"] = reference_images
        invocation = ToolInvocationContext(
            caller=UnifiedUser(user_id=USER_ID, name=USER_NAME),
            group_id=GROUP_ID,
            adapter_type=ADAPTER_TYPE,
        )
        return await self.executor.execute_async(
            self.tool,
            params,
            timeout=5,
            invocation_context=invocation,
        )

    async def tick(self) -> None:
        """跑一次后台轮询（等价于 ``BackgroundTaskSpec.run_loop`` 的一个周期）。"""
        await self.spec.task_func()

    def jobs(self) -> list[dict[str, Any]]:
        store = self.executor.get_data_store("image_generate")
        return list(store.get("jobs") or [])

    def job(self, index: int = 0) -> dict[str, Any]:
        return self.jobs()[index]


@pytest.fixture(scope="module")
def registry(tmp_path_factory: pytest.TempPathFactory) -> ToolRegistry:
    """真实注册表：扫描空的 tools 目录，同时加载包内置工具。"""
    tools_dir = tmp_path_factory.mktemp("image-flow-registry") / "tools"
    loaded = ToolRegistry()
    loaded.load_from_directory(tools_dir, auto_install_deps=False, include_builtin=True)
    return loaded


@pytest.fixture
def harness(tmp_path: Path, registry: ToolRegistry) -> _Harness:
    return _Harness(tmp_path, registry)


# ── 主链路：用户要图 → 后台画好 → 图与话一起回到原群 ─────────────────────


@pytest.mark.asyncio
async def test_a_user_asking_for_an_image_gets_it_delivered_to_the_group(harness: _Harness):
    prompt = "画一只戴墨镜的橘猫"

    # 后台轮询任务本身也要按契约注册：名字固定、每 2 秒跑一次。
    assert [spec.name for spec in harness.tasks] == ["image_generate_jobs"]
    assert harness.spec.interval_seconds == 2.0

    submitted = await harness.submit(prompt)

    # 模型侧立刻拿到"已提交"，并被告知之后用 group_file_exec 交付。
    assert submitted.success is True
    submitted_text = submitted.to_model_text()
    assert "已提交" in submitted_text
    assert "group_file_exec" in submitted_text
    assert 'action="image"' in submitted_text
    assert harness.jobs()[0]["status"] == "pending"
    assert harness.brain.image_requests == [], "提交阶段不该同步去画图"

    # 第一次 tick：后台真的向 AMKR 的生图任务要图，并落成人格目录里的文件。
    await harness.tick()

    job = harness.job()
    assert job["status"] == "done"
    assert job["notified"] is True
    image_path = Path(job["image_path"])
    assert image_path.is_absolute()
    assert image_path == harness.work_path / "tool_data/artifacts/image_generate/images" / (
        f"{job['id']}.png"
    )
    assert image_path.read_bytes() == PNG_BYTES

    image_request = harness.brain.image_requests[0]
    assert image_request.model == "image_generate", "发给 AMKR 的必须是任务名"
    assert image_request.prompt == prompt
    assert image_request.reference_images == []
    assert image_request.timeout_seconds == 300.0

    # 通知回合：这是"后台任务结果回来了"，不是用户的新消息。
    assert len(harness.brain.chat_requests) == 2
    first_round = harness.brain.chat_requests[0]
    assert first_round.work_mode is True
    assert first_round.enable_tools is True
    assert first_round.task_name == "proactive_generate"
    system_prompt = first_round.system_prompt
    assert "【后台任务完成】" in system_prompt
    assert "这不是用户的新消息" in system_prompt
    assert "group_file_exec" in system_prompt
    assert f"生成的文件：{image_path}" in system_prompt
    assert prompt in system_prompt
    # 这一轮不能把最近聊天记录当成"用户刚说的话"塞进来：循环开始时只有一条合成
    # 占位消息（之后模型自己的工具回合才追加进去）。
    assert first_round.messages[0] == {"role": "user", "content": "（后台任务结果已返回）"}

    # 模型按提示用 group_file_exec 发图：真实执行器 + 真实适配器把它变成图片段。
    assert len(harness.adapter.group_messages) == 1
    sent_group, message = harness.adapter.group_messages[0]
    assert sent_group == GROUP_ID
    image_segment = message[0]
    assert image_segment["type"] == "image"
    encoded = image_segment["data"]["file"]
    assert encoded.startswith("base64://")
    assert base64.b64decode(encoded.removeprefix("base64://")) == PNG_BYTES

    # 通知回合的正文经主动消息通道发回原群，这就是用户最后看到的那句话。
    assert len(harness.engine.dispatched) == 1
    dispatched = harness.engine.dispatched[0]
    assert dispatched["group_id"] == GROUP_ID
    assert dispatched["text"] == _NOTIFICATION_TEXT
    assert dispatched["adapter_type"] == ADAPTER_TYPE
    assert dispatched["event_id"] == f"image_generate:{job['id']}"
    assert dispatched["image_path"] == "", "正常路径由 group_file_exec 发图，不再重复带图"


@pytest.mark.asyncio
async def test_a_model_that_forgets_to_send_the_image_still_gets_it_delivered(harness: _Harness):
    """回执回合只回了话没发图：框架补发图片和文案，用户不能只收到一句"画好啦"。"""
    harness.brain.forget_to_send_image = True

    await harness.submit("画一只橘猫")
    await harness.tick()

    job = harness.job()
    assert job["status"] == "done"
    assert job["notified"] is True
    assert job["fallback"] is True
    # 模型确实没调 group_file_exec，所以平台桥接上没有任何图片消息。
    assert harness.adapter.group_messages == []

    assert len(harness.engine.dispatched) == 1
    dispatched = harness.engine.dispatched[0]
    assert dispatched["text"] == _NOTIFICATION_TEXT
    assert dispatched["image_path"] == job["image_path"]


# ── 失败路径：画不出来就如实说，不假发图 ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_generation_is_reported_honestly_without_a_fake_image(harness: _Harness):
    harness.brain.image_error = RuntimeError("provider 502")

    await harness.submit("画一张不存在的图")
    await harness.tick()

    job = harness.job()
    assert job["status"] == "failed"
    assert "provider 502" in job["error"]
    assert not job.get("image_path")
    assert job["notified"] is True

    # 通知回合明确告诉模型"失败"，并带上真实失败原因。
    assert len(harness.brain.chat_requests) == 1
    system_prompt = harness.brain.chat_requests[0].system_prompt
    assert "结果：失败" in system_prompt
    assert "provider 502" in system_prompt
    assert "生成的文件" not in system_prompt

    assert harness.adapter.group_messages == [], "失败时不能假发一张图"
    assert len(harness.engine.dispatched) == 1
    assert harness.engine.dispatched[0]["text"] == _HONEST_FAILURE_TEXT
    assert harness.engine.dispatched[0]["image_path"] == ""


# ── 参考图：本地文件按原始字节送进生图请求 ───────────────────────────────


@pytest.mark.asyncio
async def test_a_local_reference_image_reaches_the_image_request(harness: _Harness):
    reference = harness.work_path / "cat_ref.png"
    reference.write_bytes(PNG_BYTES)

    await harness.submit("照这张图再画一张", reference_images=[str(reference)])
    await harness.tick()

    image_request = harness.brain.image_requests[0]
    assert image_request.reference_images == [("01_cat_ref.png", PNG_BYTES)]
    materialized = (
        harness.work_path
        / "tool_data/artifacts/image_generate/references"
        / harness.job()["id"]
        / "01_cat_ref.png"
    )
    assert materialized.read_bytes() == PNG_BYTES
    assert harness.job()["status"] == "done"


# ── 降级：通知回合连续失败三次后，至少把图送到原群 ──────────────────────


@pytest.mark.asyncio
async def test_three_failed_notification_turns_degrade_to_image_only_delivery(harness: _Harness):
    harness.brain.notification_error = RuntimeError("模型服务不可用")

    await harness.submit("画一只柴犬")
    await harness.tick()
    await harness.tick()
    await harness.tick()

    job = harness.job()
    assert job["status"] == "done"
    assert job["notified"] is False
    assert job["notify_attempts"] == 3
    assert harness.adapter.group_messages == []
    assert harness.engine.dispatched == []

    # 第四次 tick 不再尝试模型回合，直接把已经落盘的图投递出去。
    harness.brain.notification_error = None
    await harness.tick()

    job = harness.job()
    assert job["notified"] is True
    assert job["fallback"] is True
    assert harness.adapter.group_messages == [], "降级路径不再跑模型回合"
    assert len(harness.engine.dispatched) == 1
    fallback = harness.engine.dispatched[0]
    assert fallback["text"] == ""
    assert fallback["image_path"] == job["image_path"]


# ── 生成期间新入队：慢的那张画完不能把新任务覆盖掉 ──────────────────────


@pytest.mark.asyncio
async def test_a_job_submitted_while_the_worker_is_generating_is_not_lost(harness: _Harness):
    async def submit_another() -> None:
        harness.brain.on_generate = None
        await harness.submit("再画一张猫")

    await harness.submit("画第一张图")
    harness.brain.on_generate = submit_another

    await harness.tick()
    await harness.tick()

    jobs = harness.jobs()
    assert len(jobs) == 2
    first = next(job for job in jobs if job["prompt"] == "画第一张图")
    second = next(job for job in jobs if job["prompt"] == "再画一张猫")
    assert first["status"] == "done"
    assert second["status"] == "done"
    assert first["notified"] is True
    assert second["notified"] is True
    assert [item["event_id"] for item in harness.engine.dispatched] == [
        f"image_generate:{first['id']}",
        f"image_generate:{second['id']}",
    ]
    assert "第一张图" in harness.brain.image_requests[0].prompt
    assert "再画一张猫" in harness.brain.image_requests[1].prompt


# ── 重启恢复：进程留下 running，下一次启动标失败并补通知 ────────────────


@pytest.mark.asyncio
async def test_a_job_interrupted_by_a_restart_is_recovered_and_notified(harness: _Harness):
    store = harness.executor.get_data_store("image_generate")
    store.set(
        "jobs",
        [
            {
                "id": "img_deadbeef",
                "status": "running",
                "prompt": "画一只睡着的狗",
                "references": [],
                "group_id": GROUP_ID,
                "user_id": USER_ID,
                "user_name": USER_NAME,
                "adapter_type": ADAPTER_TYPE,
                "caller_is_developer": False,
                "created_at": "2026-01-01T00:00:00+00:00",
                "notified": False,
                "notify_attempts": 0,
            }
        ],
    )
    store.save()

    await harness.tick()

    job = harness.job()
    assert job["status"] == "failed"
    assert job["error"] == "进程重启，任务中断"
    assert harness.brain.image_requests == [], "中断的任务不能悄悄重跑一次生图"
    assert job["notified"] is True
    assert harness.brain.chat_requests[0].system_prompt.count("【后台任务完成】") == 1
    assert harness.engine.dispatched[0]["text"] == _HONEST_FAILURE_TEXT
    assert harness.engine.dispatched[0]["group_id"] == GROUP_ID


# ── 集成登记：注册表 / 模型路由 / WebUI 目录都能看到这个新工具与新任务 ──


def test_the_real_registry_exposes_a_model_visible_periodic_tool(registry: ToolRegistry):
    tool = registry.get("image_generate")

    assert tool is not None
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
    assert "image_generate" in by_name
    assert by_name["image_generate"]["parameters"]["required"] == ["prompt"]


def test_the_model_router_and_webui_catalog_register_the_drawing_task():
    task = ModelRouter().resolve("image_generate")

    assert task.model_name == "image_generate"
    assert task.timeout == 300.0
    assert task.retries == 0

    catalog = build_model_catalog()
    assert catalog["available_models"] == list(_DEFAULT_TASK_REGISTRY)
    assert len(catalog["available_models"]) == 10
    assert "image_generate" in catalog["available_models"]
    labels = {choice["value"]: choice["label"] for choice in catalog["model_choices"]}
    assert labels["image_generate"] == f"{TASK_LABELS['image_generate']}（image_generate）"


def test_engine_context_signatures_bind_the_keyword_arguments_the_tool_actually_sends():
    """工具调用现场传的关键字参数，必须能被引擎上下文的两个新方法原样绑定。

    这是跨模块契约：工具在 ``image_generate.py`` 里写死 kwargs，方法签名在
    ``tool_engine_context.py`` 里；任何一边改名都会在运行时变成 TypeError。
    这里直接从工具源码 AST 里取实际发送的关键字，而不是在测试里再抄一份。
    """

    def _keyword_names(method_name: str) -> list[set[str]]:
        tree = ast.parse(_TOOL_SOURCE.read_text(encoding="utf-8"))
        found: list[set[str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called == method_name:
                found.append({kw.arg for kw in node.keywords if kw.arg})
        return found

    for method_name in ("generate_image", "generate_background_result_message"):
        method = getattr(ToolEngineContextImpl, method_name)
        calls = _keyword_names(method_name)
        assert calls, f"工具源码里已经没有对 {method_name} 的调用，契约检查会失效"
        for keywords in calls:
            method_signature = inspect.signature(method)
            # 类上的未绑定方法仍需要 self 占位，其余关键字按工具实际发送的来。
            method_signature.bind(None, **{name: None for name in keywords})


def test_the_image_generate_protocol_methods_match_the_engine_implementation():
    """工具依赖的是 Protocol；引擎实现必须真的提供这两个能力。"""
    for method_name in ("generate_image", "generate_background_result_message"):
        assert callable(
            getattr(ToolEngineContext, method_name, None)
        ), f"ToolEngineContext 协议没有声明 {method_name}，被动工具将无法依赖它"
        assert callable(
            getattr(ToolEngineContextImpl, method_name, None)
        ), f"ToolEngineContextImpl 没有实现 {method_name}"
