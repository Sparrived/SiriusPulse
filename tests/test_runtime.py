from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.persona_config import PersonaExperienceConfig
from sirius_pulse.persona_worker import PersonaWorker
from sirius_pulse.platforms.runtime import EngineRuntime, _wait_for_embedding_health
from sirius_pulse.plugins.models import PluginDefinition, PluginPermissionDef
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
        },
    )

    runtime = EngineRuntime(persona_dir)

    assert runtime.global_data_path == data_dir
    assert runtime.has_provider_config() is True

    provider = runtime._build_provider()
    assert provider is not None
    assert provider._base_url == "http://amkr.internal:8000"
    assert provider._api_key == "sk-amkr-admin"
    assert provider._workspace == "sirius-pulse"


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


def test_persona_worker_passes_main_model_reply_cooldown_to_runtime_config(tmp_path):
    worker = PersonaWorker(tmp_path)
    experience = PersonaExperienceConfig(
        main_model_reply_cooldown_seconds=7.5,
        diary_top_k=7,
        diary_token_budget=900,
        memory_unit_top_k=4,
        group_reply_strategies={"group-keyword": "keyword"},
    )

    plugin_config = worker._build_plugin_config(experience)

    assert plugin_config["main_model_reply_cooldown_seconds"] == 7.5
    assert plugin_config["diary_top_k"] == 7
    assert plugin_config["diary_token_budget"] == 900
    assert plugin_config["memory_unit_top_k"] == 4
    assert plugin_config["group_reply_strategies"] == {"group-keyword": "keyword"}


def test_persona_worker_experience_reload_updates_runtime_config_keys(tmp_path):
    worker = PersonaWorker(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    PersonaExperienceConfig(
        engagement_sensitivity=0.8,
        min_reply_interval_seconds=13,
        max_sentence_chars=31,
        diary_top_k=6,
        diary_token_budget=700,
        memory_unit_top_k=2,
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
    assert engine.config["diary_top_k"] == 6
    assert engine.config["diary_token_budget"] == 700
    assert engine.config["memory_unit_top_k"] == 2
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
