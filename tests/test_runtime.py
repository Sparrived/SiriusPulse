from __future__ import annotations

import asyncio
import os
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.core.brain import Brain, ChatRequest
from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase
from sirius_pulse.core.model_router import ModelRouter
from sirius_pulse.core.orchestration_store import OrchestrationStore
from sirius_pulse.persona_config import PersonaExperienceConfig
from sirius_pulse.persona_worker import PersonaWorker
from sirius_pulse.platforms.runtime import EngineRuntime, _wait_for_embedding_health
from sirius_pulse.plugins.models import PluginDefinition, PluginPermissionDef
from sirius_pulse.providers.amkr import load_inference_keys, load_panel_keys
from sirius_pulse.providers.mock import MockProvider
from sirius_pulse.utils.json_io import atomic_write_json


def test_engine_runtime_when_workspace_has_shared_plugins_then_uses_shared_directory(tmp_path):
    data_dir = tmp_path / "data"
    persona_dir = data_dir / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    shared_plugins = tmp_path / "plugins"
    shared_plugins.mkdir()

    runtime = EngineRuntime(persona_dir)

    assert runtime._plugins_dir() == shared_plugins


def test_engine_runtime_plugin_config_cannot_relax_manifest_security(tmp_path):
    data_dir = tmp_path / "data"
    persona_dir = data_dir / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    atomic_write_json(
        plugins_dir / "_config.json",
        {
            "locked": {
                "enabled": True,
                "permissions": {
                    "developer_only": False,
                    "hidden_from_intent": False,
                    "rate_limit_calls_per_minute": 0,
                },
                "settings": {},
            }
        },
    )
    runtime = EngineRuntime(persona_dir)
    definition = PluginDefinition(
        name="locked",
        permissions=PluginPermissionDef(
            developer_only=True,
            hidden_from_intent=True,
            rate_limit_calls_per_minute=10,
        ),
    )

    runtime._merge_plugin_config(definition)

    assert definition.permissions.developer_only is True
    assert definition.permissions.hidden_from_intent is True
    assert definition.permissions.rate_limit_calls_per_minute == 10


def test_engine_runtime_when_tool_bridge_has_routes_then_registers_proactive_destinations(tmp_path):
    registrations = []

    class Engine:
        _tool_executor = None

        def register_adapter(self, *args, **kwargs):
            registrations.append((args, kwargs))

    class Adapter:
        def get_configured_group_ids(self):
            return ["g1"]

        def get_configured_private_user_ids(self):
            return ["u1"]

    runtime = EngineRuntime(tmp_path)
    runtime._engine = Engine()
    adapter = Adapter()

    runtime.add_tool_bridge("custom", adapter)

    assert registrations == [
        (
            (adapter,),
            {
                "adapter_type": "custom",
                "group_ids": ["g1"],
                "private_user_ids": ["u1"],
            },
        )
    ]
    assert runtime._engine._adapter is adapter


def test_engine_runtime_when_work_path_is_persona_dir_then_loads_global_amkr_settings(tmp_path):
    data_dir = tmp_path / "data"
    persona_dir = data_dir / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    atomic_write_json(
        data_dir / "global_config.json",
        {
            "amkr_base_url": "http://amkr.internal:8000",
            "amkr_local_api_key": "sk-amkr-admin",
            "amkr_workspace": "sirius-pulse",
            "amkr_inference_keys": {"sirius-pulse/sirius": "amkr_ik_persona"},
        },
    )

    runtime = EngineRuntime(persona_dir)

    assert runtime.global_data_path == data_dir
    assert runtime.has_provider_config() is True

    provider = runtime._build_provider()
    assert provider is not None
    assert provider._base_url == "http://amkr.internal:8000"
    # 模型调用发出去的是该空间的**推理 key**，不是全局管理员 key：后者能增删
    # 供应商与 Key，放在每次对话补全的请求头上等于让推理路径随时可以升级成管理操作。
    assert provider._api_key == "amkr_ik_persona"
    assert provider._api_key != "sk-amkr-admin"
    # 必须指向本 persona 的子空间：任务注册写在 <base>/<persona>，请求带错空间
    # 会让 AMKR 查不到任务定义。
    assert provider._workspace == "sirius-pulse/sirius"


def test_engine_runtime_when_inference_key_missing_then_not_ready(tmp_path):
    """有管理员凭据但缺该空间的推理 key 时**不就绪**，且绝不回落到管理员 key。

    回落会让一个配置疏漏静默变成一次越权：请求照样成功，运维以为已经收窄了。
    """
    data_dir = tmp_path / "data"
    persona_dir = data_dir / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    atomic_write_json(
        data_dir / "global_config.json",
        {
            "amkr_base_url": "http://amkr.internal:8000",
            "amkr_local_api_key": "sk-amkr-admin",
            "amkr_workspace": "sirius-pulse",
        },
    )

    runtime = EngineRuntime(persona_dir)

    assert runtime._build_provider() is None


def test_engine_runtime_when_amkr_key_missing_then_not_ready(tmp_path):
    """没配授权 Key 时不应就绪，且不抛异常。"""
    persona_dir = tmp_path / "data" / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    atomic_write_json(
        persona_dir.parent.parent / "global_config.json",
        {"amkr_base_url": "http://127.0.0.1:8000"},
    )

    runtime = EngineRuntime(persona_dir)

    assert runtime.has_provider_config() is False


def test_engine_runtime_when_registering_tasks_then_requests_use_the_same_workspace(
    tmp_path, monkeypatch
):
    """注册与请求必须落在同一个工作空间。

    任务名只在工作空间内唯一：如果注册写进 ``<base>/<persona>`` 而请求只带
    ``<base>``，AMKR 就会把任务名当成普通模型名去查，从而永远命中不了任务定义。
    """
    import httpx

    data_dir = tmp_path / "data"
    persona_dir = data_dir / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    atomic_write_json(
        data_dir / "global_config.json",
        {
            "amkr_base_url": "http://amkr.internal:8000",
            "amkr_local_api_key": "sk-amkr-admin",
            "amkr_workspace": "sirius-pulse",
        },
    )

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-amkr-workspace", ""))
        if request.url.path == "/api/workspaces":
            return httpx.Response(
                201,
                json={
                    "name": "sirius-pulse/sirius",
                    "api_key": "amkr_ws_k",
                    "inference_key": "amkr_ik_k",
                },
            )
        if request.url.path == "/api/tasks":
            return httpx.Response(200, json={"tasks": [], "config_revision": "rev-1"})
        return httpx.Response(200, json={"config_revision": "rev-2"})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)),
    )

    runtime = EngineRuntime(persona_dir)
    result = asyncio.run(runtime.register_amkr_tasks())
    provider = runtime._build_provider()

    assert result.ok, result.failed
    assert seen, "注册应当真的发起请求"
    assert set(seen) == {"sirius-pulse/sirius"}
    assert provider is not None
    assert provider._workspace == "sirius-pulse/sirius"
    assert provider._api_key == "amkr_ik_k"
    # 两把 key 都只在建空间时返回一次，因此必须当场落到全局配置里。
    assert load_panel_keys(data_dir) == {"sirius-pulse/sirius": "amkr_ws_k"}
    assert load_inference_keys(data_dir) == {"sirius-pulse/sirius": "amkr_ik_k"}


def test_engine_runtime_when_building_engine_then_provisions_credentials_before_provider(
    tmp_path, monkeypatch
):
    """全新安装时，建引擎必须**先备齐空间凭据，再建 provider**。

    推理 key 只在建空间那一次返回，而 provider 要拿它才能构造。顺序反了的话，
    全新部署上 provider 永远拿不到凭据，引擎永远不就绪——而且报的是「未配置
    AMKR」，把人引向一个没问题的配置项。
    """
    import httpx

    data_dir = tmp_path / "data"
    persona_dir = data_dir / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    atomic_write_json(
        data_dir / "global_config.json",
        {
            "amkr_base_url": "http://amkr.internal:8000",
            "amkr_local_api_key": "sk-amkr-admin",
            "amkr_workspace": "sirius-pulse",
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/workspaces":
            return httpx.Response(
                201,
                json={
                    "name": "sirius-pulse/sirius",
                    "api_key": "amkr_ws_new",
                    "inference_key": "amkr_ik_new",
                },
            )
        if request.url.path == "/api/tasks":
            return httpx.Response(200, json={"tasks": [], "config_revision": "rev-1"})
        return httpx.Response(200, json={"config_revision": "rev-2"})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)),
    )

    runtime = EngineRuntime(persona_dir)
    # 尚未注册过任何东西：这时直接建 provider 必然是 None（这正是顺序问题的现场）。
    assert runtime._build_provider() is None

    asyncio.run(runtime.register_amkr_tasks())

    # 注册补齐了凭据，于是 provider 现在能建起来，且用的是推理 key。
    provider = runtime._build_provider()
    assert provider is not None
    assert provider._api_key == "amkr_ik_new"


def test_persona_worker_passes_main_model_reply_cooldown_to_runtime_config(tmp_path):
    worker = PersonaWorker(tmp_path)
    experience = PersonaExperienceConfig(
        main_model_reply_cooldown_seconds=7.5,
        memory_unit_top_k=4,
        memory_unit_token_budget=1500,
        group_reply_strategies={"group-keyword": "keyword"},
    )

    plugin_config = worker._build_plugin_config(experience)

    assert plugin_config["main_model_reply_cooldown_seconds"] == 7.5
    assert plugin_config["memory_unit_top_k"] == 4
    assert plugin_config["memory_unit_token_budget"] == 1500
    assert plugin_config["group_reply_strategies"] == {"group-keyword": "keyword"}


def test_persona_worker_experience_reload_updates_runtime_config_keys(tmp_path):
    worker = PersonaWorker(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    PersonaExperienceConfig(
        engagement_sensitivity=0.8,
        min_reply_interval_seconds=13,
        max_sentence_chars=31,
        memory_unit_top_k=2,
        memory_unit_token_budget=1300,
    ).save(tmp_path / "experience.json")

    class Brain:
        config = {}

    class Engine:
        config = {}
        brain = Brain()

    engine = Engine()

    worker._reload_experience(engine)

    assert engine.config["sensitivity"] == 0.8
    assert engine.config["reply_cooldown_seconds"] == 13
    assert engine.config["max_sentence_chars"] == 31
    assert engine.config["memory_unit_top_k"] == 2
    assert engine.config["memory_unit_token_budget"] == 1300
    assert "engagement_sensitivity" not in engine.config
    assert engine.brain.config["memory_unit_top_k"] == 2


def test_persona_worker_config_reload_consumes_experience_flag(tmp_path):
    worker = PersonaWorker(tmp_path)
    PersonaExperienceConfig(memory_unit_top_k=15).save(tmp_path / "experience.json")
    engine = SimpleNamespace(config={}, brain=SimpleNamespace(config={}))
    worker._runtime = SimpleNamespace(engine=engine)
    flag = tmp_path / "engine_state" / "reload_requested"
    flag.parent.mkdir()
    flag.write_text("experience", encoding="utf-8")
    os.utime(flag, (0, 0))

    worker._check_config_reload()

    assert not flag.exists()
    assert engine.config["memory_unit_top_k"] == 15
    assert engine.brain.config["memory_unit_top_k"] == 15


def _worker_with_orchestration(tmp_path, explicit_limit=None, provider=None):
    OrchestrationStore.save(
        tmp_path,
        {
            "task_timeout": {"response_generate": 7},
            "task_retries": {"memory_extract": 5},
            "max_concurrent_llm_calls": 3,
        },
    )
    engine = _EmotionalGroupChatEngineBase.__new__(_EmotionalGroupChatEngineBase)
    engine.work_path = tmp_path
    engine.config = {} if explicit_limit is None else {"max_concurrent_llm_calls": explicit_limit}
    engine._explicit_config_keys = set(engine.config)
    engine._init_orchestration_and_task_models()
    engine._init_model_router()
    engine.brain = Brain(
        provider_async=provider or MockProvider(),
        model_router=engine.model_router,
        persona=SimpleNamespace(name="月白", build_system_prompt=lambda: ""),
        config=engine.config,
    )
    worker = PersonaWorker(tmp_path)
    worker._runtime = SimpleNamespace(
        engine=engine, _build_provider=lambda: engine.brain.provider_async
    )
    return worker, engine


def _request_orchestration_reload(worker):
    flag = worker.paths.engine_state / "reload_requested"
    flag.write_text("orchestration", encoding="utf-8")
    os.utime(flag, (0, 0))
    worker._check_config_reload()
    assert not flag.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_limit", [None, 2])
async def test_persona_worker_empty_orchestration_restores_defaults(tmp_path, explicit_limit):
    """用户清空编排后，后续模型请求使用默认路由与并发；显式参数仍优先。"""

    class ConcurrentMockProvider(MockProvider):
        active = 0
        peak = 0

        async def generate_async(self, request, return_reasoning=False):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0)
                return await super().generate_async(request, return_reasoning)
            finally:
                self.active -= 1

    provider = ConcurrentMockProvider(["收到"] * 3)
    worker, engine = _worker_with_orchestration(tmp_path, explicit_limit, provider)
    OrchestrationStore.save(tmp_path, {})

    _request_orchestration_reload(worker)

    defaults = ModelRouter()
    assert engine.model_router.resolve("response_generate").timeout == (
        defaults.resolve("response_generate").timeout
    )
    assert engine.brain.router.resolve("memory_extract").retries == (
        defaults.resolve("memory_extract").retries
    )
    assert engine.config.get("max_concurrent_llm_calls") == explicit_limit
    assert engine.brain.config.get("max_concurrent_llm_calls") == explicit_limit
    results = await asyncio.gather(
        *(
            engine.brain.chat(
                ChatRequest(
                    group_id=f"group-{i}",
                    user_id="u1",
                    system_prompt="system",
                    messages=[{"role": "user", "content": "清空配置后的回复"}],
                )
            )
            for i in range(3)
        )
    )
    assert [result.clean_text for result in results] == ["收到"] * 3
    assert provider.peak == (explicit_limit or 1)


def test_persona_worker_all_reload_allows_empty_orchestration_before_rebuild(tmp_path, monkeypatch):
    worker, engine = _worker_with_orchestration(tmp_path)
    OrchestrationStore.save(tmp_path, {})
    actions = []
    for method in (
        "_reload_persona",
        "_reload_experience",
        "_reload_provider",
        "_reload_global_config",
        "_reload_memory_index",
    ):
        monkeypatch.setattr(worker, method, lambda _engine: None)
    monkeypatch.setattr(worker, "_schedule_engine_rebuild", lambda: actions.append("rebuild"))
    flag = worker.paths.engine_state / "reload_requested"
    flag.write_text("all", encoding="utf-8")
    os.utime(flag, (0, 0))

    worker._check_config_reload()

    assert actions == ["rebuild"]
    assert engine.config.get("max_concurrent_llm_calls") is None
    assert engine.brain.config.get("max_concurrent_llm_calls") is None
    assert engine.model_router.resolve("response_generate").timeout == (
        ModelRouter().resolve("response_generate").timeout
    )


@pytest.mark.parametrize("content", [None, "{broken", '{"max_concurrent_llm_calls": NaN}'])
def test_persona_worker_all_reload_aborts_before_other_changes_when_orchestration_fails(
    tmp_path, monkeypatch, content
):
    worker, engine = _worker_with_orchestration(tmp_path)
    previous_router = engine.model_router
    path = OrchestrationStore._path(tmp_path)
    if content is None:
        path.unlink()
    else:
        path.write_text(content, encoding="utf-8")
    actions = []
    for method in (
        "_reload_persona",
        "_reload_experience",
        "_reload_provider",
        "_reload_global_config",
        "_reload_memory_index",
    ):
        monkeypatch.setattr(worker, method, lambda _engine, name=method: actions.append(name))
    monkeypatch.setattr(worker, "_schedule_engine_rebuild", lambda: actions.append("rebuild"))
    flag = worker.paths.engine_state / "reload_requested"
    flag.write_text("all", encoding="utf-8")
    os.utime(flag, (0, 0))

    worker._check_config_reload()

    assert actions == []
    assert worker._runtime.engine is engine
    assert engine.model_router is previous_router
    assert engine.config["max_concurrent_llm_calls"] == 3
    assert engine.brain.config["max_concurrent_llm_calls"] == 3
    assert engine.model_router.resolve("response_generate").timeout == 7


@pytest.mark.parametrize("failure", ["missing", "broken-json", "non-object", "io", "encoding"])
def test_persona_worker_failed_orchestration_reload_preserves_live_settings(
    tmp_path, monkeypatch, caplog, failure
):
    worker, engine = _worker_with_orchestration(tmp_path)
    previous_router = engine.model_router
    path = OrchestrationStore._path(tmp_path)
    if failure == "missing":
        path.unlink()
    elif failure == "broken-json":
        path.write_text("{broken", encoding="utf-8")
    elif failure == "non-object":
        path.write_text("[]", encoding="utf-8")
    elif failure == "encoding":
        path.write_bytes(b"\xff")
    else:
        read_text = type(path).read_text

        def denied_read(self, *args, **kwargs):
            if self == path:
                raise PermissionError("orchestration read denied")
            return read_text(self, *args, **kwargs)

        monkeypatch.setattr(type(path), "read_text", denied_read)

    _request_orchestration_reload(worker)

    assert engine.model_router is previous_router
    assert engine.brain.router is previous_router
    assert engine.model_router.resolve("response_generate").timeout == 7
    assert engine.model_router.resolve("memory_extract").retries == 5
    assert engine.brain.config["max_concurrent_llm_calls"] == 3
    assert engine.config["max_concurrent_llm_calls"] == 3
    assert "保留旧配置" in caplog.text
    # Existing startup callers still get the default fallback, including missing files.
    assert OrchestrationStore.load(tmp_path) == {}


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e309", "-1e309"])
def test_persona_worker_nonfinite_orchestration_rejected_before_mutating_settings(
    tmp_path, caplog, number
):
    worker, engine = _worker_with_orchestration(tmp_path)
    previous_router = engine.model_router
    OrchestrationStore._path(tmp_path).write_text(
        '{"task_timeout": {"response_generate": 19}, "max_concurrent_llm_calls": ' + number + "}",
        encoding="utf-8",
    )

    _request_orchestration_reload(worker)

    assert engine.model_router is previous_router
    assert engine.brain.router is previous_router
    assert engine._orch_task_timeout == {"response_generate": 7}
    assert engine._orch_task_retries == {"memory_extract": 5}
    assert engine.config["max_concurrent_llm_calls"] == 3
    assert engine.brain.config["max_concurrent_llm_calls"] == 3
    assert "保留旧配置" in caplog.text


def test_persona_worker_orchestration_reload_uses_one_validated_snapshot(tmp_path, monkeypatch):
    worker, engine = _worker_with_orchestration(tmp_path)
    OrchestrationStore.save(tmp_path, {"task_timeout": {"response_generate": 12}})
    load = OrchestrationStore.load
    reads = []

    def load_then_remove(cls, work_path, *, strict=False):
        reads.append(strict)
        config = load(work_path, strict=strict)
        cls._path(work_path).unlink()
        return config

    monkeypatch.setattr(OrchestrationStore, "load", classmethod(load_then_remove))

    _request_orchestration_reload(worker)

    assert reads == [True]
    assert engine.brain.router.resolve("response_generate").timeout == 12
    assert engine.brain.config.get("max_concurrent_llm_calls") is None


@pytest.mark.asyncio
async def test_embedding_health_wait_does_not_block_persona_event_loop():
    entered = threading.Event()
    release = threading.Event()

    class BlockingClient:
        def check_health(self):
            entered.set()
            release.wait(timeout=1)
            return False

    ticker_ran = asyncio.Event()
    health_task = asyncio.create_task(
        _wait_for_embedding_health(
            BlockingClient(),
            total_timeout_seconds=0.05,
            per_attempt_timeout_seconds=0.02,
            retry_seconds=0,
        )
    )
    await asyncio.to_thread(entered.wait, 0.5)
    await asyncio.sleep(0)
    ticker_ran.set()

    assert ticker_ran.is_set()
    assert await health_task is False
    release.set()


@pytest.mark.asyncio
async def test_engine_runtime_warmup_failure_resets_running_for_retry(tmp_path, monkeypatch):
    runtime = EngineRuntime(tmp_path)
    monkeypatch.setattr(runtime, "has_provider_config", lambda: True)
    monkeypatch.setattr(runtime, "has_persona", lambda: True)

    async def failing_ensure():
        raise RuntimeError("embedding unavailable")

    runtime._ensure_engine_locked = failing_ensure  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="embedding unavailable"):
        await runtime.start()

    assert runtime._running is False
    assert runtime.engine is None


@pytest.mark.asyncio
async def test_engine_runtime_failed_build_preserves_embedding_backoff(tmp_path):
    runtime = EngineRuntime(tmp_path)
    runtime._embedding_build_failed = True
    runtime._embedding_fail_count = 2

    await runtime._reload_engine_locked(reset_embedding_backoff=False)

    assert runtime._embedding_build_failed is True
    assert runtime._embedding_fail_count == 2


@pytest.mark.asyncio
async def test_persona_worker_when_runtime_has_no_engine_then_does_not_start_adapters(tmp_path):
    started = []

    class Runtime:
        engine = None

        async def start(self):
            started.append("runtime")

    worker = PersonaWorker(tmp_path)
    worker._runtime = Runtime()
    worker._start_adapter = AsyncMock()  # type: ignore[method-assign]

    await worker._start_runtime_and_adapters(
        SimpleNamespace(adapters=[SimpleNamespace(enabled=True, type="napcat")]), {}
    )

    assert started == ["runtime"]
    worker._start_adapter.assert_not_awaited()


def test_persona_worker_all_reload_schedules_engine_rebuild(tmp_path):
    worker = PersonaWorker(tmp_path)
    engine = SimpleNamespace(config={}, brain=SimpleNamespace(config={}))
    worker._runtime = SimpleNamespace(engine=engine)
    flag = tmp_path / "engine_state" / "reload_requested"
    flag.parent.mkdir()
    flag.write_text("all", encoding="utf-8")
    os.utime(flag, (0, 0))
    scheduled = []
    worker._reload_persona = lambda _engine: None  # type: ignore[method-assign]
    worker._reload_orchestration = lambda _engine: None  # type: ignore[method-assign]
    worker._reload_experience = lambda _engine: None  # type: ignore[method-assign]
    worker._reload_provider = lambda _engine: None  # type: ignore[method-assign]
    worker._reload_global_config = lambda _engine: None  # type: ignore[method-assign]
    worker._schedule_engine_rebuild = lambda: scheduled.append(True)  # type: ignore[method-assign]

    worker._check_config_reload()

    assert scheduled == [True]


@pytest.mark.asyncio
async def test_persona_worker_rebuild_engine_rebinds_adapters(tmp_path):
    replacement = object()
    bridges = []

    class Runtime:
        async def rebuild_engine(self):
            return replacement

        def add_tool_bridge(self, adapter_type, adapter):
            bridges.append((adapter_type, adapter))

    class Adapter:
        def __init__(self):
            self.engine = None

        async def rebind_engine(self, engine):
            self.engine = engine

    adapter = Adapter()
    worker = PersonaWorker(tmp_path)
    worker._runtime = Runtime()
    worker._adapters = [adapter]

    await worker._rebuild_engine_and_rebind()

    assert adapter.engine is replacement
    assert bridges == [("napcat", adapter)]


def test_engine_runtime_includes_main_model_reply_cooldown_in_engine_config(tmp_path):
    runtime = EngineRuntime(
        tmp_path,
        plugin_config={"main_model_reply_cooldown_seconds": 7.5},
    )

    config = runtime._build_engine_runtime_config(PersonaExperienceConfig())

    assert config["main_model_reply_cooldown_seconds"] == 7.5


def test_engine_runtime_includes_group_reply_strategies_in_engine_config(tmp_path):
    runtime = EngineRuntime(tmp_path)

    config = runtime._build_engine_runtime_config(
        PersonaExperienceConfig(group_reply_strategies={"group-keyword": "keyword"})
    )

    assert config["group_reply_strategies"] == {"group-keyword": "keyword"}


@pytest.mark.asyncio
async def test_engine_runtime_concurrent_ensure_builds_one_engine(tmp_path):
    runtime = EngineRuntime(tmp_path)
    builds = []

    class Engine:
        def __init__(self):
            self._bg_running = False

        def start_background_tasks(self):
            self._bg_running = True

        def stop_background_tasks(self):
            self._bg_running = False

    async def build_engine():
        builds.append(object())
        await asyncio.sleep(0)
        return Engine()

    runtime._build_engine = build_engine  # type: ignore[method-assign]

    first, second = await asyncio.gather(runtime._ensure_engine(), runtime._ensure_engine())

    assert first is second
    assert len(builds) == 1
    assert runtime.engine is first


@pytest.mark.asyncio
async def test_engine_runtime_ensure_waits_for_in_progress_rebuild(tmp_path):
    runtime = EngineRuntime(tmp_path)
    builds = []
    build_started = asyncio.Event()
    allow_build = asyncio.Event()

    class Engine:
        def __init__(self, name):
            self.name = name
            self._bg_running = False
            self.stop_calls = 0

        def save_state(self):
            return None

        def start_background_tasks(self):
            self._bg_running = True

        def stop_background_tasks(self):
            self.stop_calls += 1
            self._bg_running = False

    old_engine = Engine("old")
    runtime._engine = old_engine

    async def build_engine():
        builds.append(object())
        build_started.set()
        await allow_build.wait()
        return Engine("replacement")

    runtime._build_engine = build_engine  # type: ignore[method-assign]
    rebuild_task = asyncio.create_task(runtime.rebuild_engine())
    await build_started.wait()
    ensure_task = asyncio.create_task(runtime._ensure_engine())
    await asyncio.sleep(0)

    assert ensure_task.done() is False
    assert len(builds) == 1

    allow_build.set()
    replacement = await rebuild_task
    ensured = await ensure_task

    assert replacement is ensured
    assert runtime.engine is replacement
    assert old_engine.stop_calls == 1
    assert len(builds) == 1


@pytest.mark.asyncio
async def test_engine_runtime_queued_ensure_is_fenced_after_stop(tmp_path):
    runtime = EngineRuntime(tmp_path)
    build_calls = []

    async def build_engine():
        build_calls.append(True)
        raise AssertionError("closed runtime must not build an engine")

    runtime._build_engine = build_engine  # type: ignore[method-assign]
    await runtime._engine_lock.acquire()
    stop_task = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0)
    ensure_task = asyncio.create_task(runtime._ensure_engine())
    await asyncio.sleep(0)
    runtime._engine_lock.release()

    await stop_task
    with pytest.raises(RuntimeError, match="已关闭"):
        await ensure_task
    assert build_calls == []


@pytest.mark.asyncio
async def test_engine_runtime_reload_retires_and_closes_old_event_bus(tmp_path):
    runtime = EngineRuntime(tmp_path)
    event_bus = SimpleNamespace(close=AsyncMock())
    stopped = []
    engine = SimpleNamespace(
        event_bus=event_bus,
        save_state=lambda: None,
        stop_background_tasks=lambda: stopped.append(True),
    )
    runtime._engine = engine

    await runtime.reload_engine()

    assert engine._runtime_retiring is True
    assert stopped == [True]
    event_bus.close.assert_awaited_once()
    assert runtime.engine is None


@pytest.mark.asyncio
async def test_persona_worker_startup_lock_serializes_initialization(tmp_path):
    lock = asyncio.Lock()
    workers = [PersonaWorker(tmp_path / name, startup_lock=lock) for name in ("sunspot", "sirius")]
    state = {"active": 0, "max_active": 0}

    async def fake_start(_adapters_cfg, _plugin_config):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(0)
        state["active"] -= 1

    for worker in workers:
        worker._start_runtime_and_adapters = fake_start

    await asyncio.gather(
        *(worker._start_with_lock(SimpleNamespace(adapters=[]), {}) for worker in workers)
    )

    assert state["max_active"] == 1


def _heartbeat_status_for(worker: PersonaWorker, adapter: object) -> dict:
    """跑一轮心跳循环并返回写入的 worker_status。"""
    worker._adapters = [adapter]  # type: ignore[list-item]
    worker._running = True
    written: dict = {}
    worker._write_status = lambda status: written.update(status)  # type: ignore[method-assign]
    worker._check_config_reload = lambda: None  # type: ignore[method-assign]

    async def _once() -> None:
        sleeps = 0

        async def _fake_sleep(_seconds: float) -> None:
            nonlocal sleeps
            assert _seconds == 10
            sleeps += 1
            if sleeps >= 1:
                worker._running = False

        original = asyncio.sleep
        asyncio.sleep = _fake_sleep  # type: ignore[assignment]
        try:
            await worker._heartbeat_loop()
        finally:
            asyncio.sleep = original  # type: ignore[assignment]

    asyncio.run(_once())
    assert datetime.fromisoformat(written["heartbeat_at"]).tzinfo is not None
    return written


def test_heartbeat_reports_adapter_offline_without_claiming_process_stopped():
    """适配器离线时心跳要如实暴露，但 status 必须仍是 running。

    §3.3 的故障是「AI 静默离线、心跳照写、UI 仍显示运行中」。修法是把连接
    健康写进心跳；但若顺手把 status 改成非 running，`_is_persona_running()`
    会判定进程已停，WebUI 就会重复拉起第二个 worker —— 那是更严重的故障。
    """

    class _OfflineAdapter:
        def connection_state(self) -> dict:
            return {"online": False, "reconnect_exhausted": False, "offline_seconds": 42.0}

    worker = PersonaWorker(os.path.join(os.getcwd(), "unused-persona-dir"))
    status = _heartbeat_status_for(worker, _OfflineAdapter())

    assert status["status"] == "running"
    assert status["adapter_online"] is False
    assert status["degraded"] is True
    assert status["adapter_offline_seconds"] == 42.0


def test_heartbeat_marks_adapter_online_when_connected():
    class _OnlineAdapter:
        def connection_state(self) -> dict:
            return {"online": True, "reconnect_exhausted": False, "offline_seconds": 0.0}

    worker = PersonaWorker(os.path.join(os.getcwd(), "unused-persona-dir"))
    status = _heartbeat_status_for(worker, _OnlineAdapter())

    assert status["status"] == "running"
    assert status["adapter_online"] is True
    assert status["degraded"] is False


def test_heartbeat_omits_connection_fields_for_legacy_adapters():
    """不支持连接状态的适配器不应让心跳出现假字段。"""

    class _LegacyAdapter:
        pass

    worker = PersonaWorker(os.path.join(os.getcwd(), "unused-persona-dir"))
    status = _heartbeat_status_for(worker, _LegacyAdapter())

    assert status["status"] == "running"
    assert "adapter_online" not in status
