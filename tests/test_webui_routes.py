from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from sirius_pulse.utils.json_io import atomic_write_json
from sirius_pulse.webui import persona_manager_api as persona_manager
from sirius_pulse.webui.routes import WEBUI_ROUTES
from sirius_pulse.webui.server import DELEGATED_HANDLERS, WebUIServer


def _route_snapshot(app: web.Application) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for route in app.router.routes():
        resource = route.resource
        path = getattr(resource, "canonical", None)
        if path is None:
            continue
        routes.add((route.method, path))
    return routes


def test_webui_routes_when_server_is_created_then_all_declared_routes_are_registered(tmp_path):
    server = WebUIServer(data_dir=tmp_path)

    registered = _route_snapshot(server.app)

    for spec in WEBUI_ROUTES:
        assert (spec.method, spec.path) in registered
    assert ("GET", "/") in registered
    assert ("GET", "/ws/events") in registered


def test_webui_routes_when_declared_then_handler_names_are_available(tmp_path):
    server = WebUIServer(data_dir=tmp_path)

    for spec in WEBUI_ROUTES:
        handler = getattr(server, spec.handler_name)
        assert callable(handler)


@pytest.mark.asyncio
async def test_webui_delegated_handler_when_called_then_injects_data_dir(tmp_path, monkeypatch):
    calls = []

    async def fake_handler(request, data_dir):
        calls.append((request, data_dir))
        return web.json_response({"ok": True})

    monkeypatch.setitem(DELEGATED_HANDLERS, "api_persona_get", fake_handler)
    server = WebUIServer(data_dir=tmp_path)
    request = SimpleNamespace()

    response = await server.api_persona_get(request)

    assert response.status == 200
    assert calls == [(request, server.data_dir)]


@pytest.mark.asyncio
async def test_webui_persona_stop_when_worker_is_injected_then_shutdown_is_requested(tmp_path):
    persona_dir = tmp_path / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    atomic_write_json(tmp_path / "global_config.json", {"active_persona": "sirius"})

    class Worker:
        def __init__(self) -> None:
            self.persona_dir = persona_dir
            self.shutdown_called = False

        def shutdown(self) -> None:
            self.shutdown_called = True

    worker = Worker()
    server = WebUIServer(data_dir=tmp_path, persona_manager=worker)

    response = await server.api_persona_stop(SimpleNamespace())
    saved = json.loads((tmp_path / "global_config.json").read_text(encoding="utf-8"))

    assert response.status == 200
    assert saved["active_persona"] == ""
    assert worker.shutdown_called is True


@pytest.mark.asyncio
async def test_webui_persona_stop_when_worker_targets_other_persona_then_shutdown_is_skipped(tmp_path):
    active_dir = tmp_path / "personas" / "sirius"
    other_dir = tmp_path / "personas" / "other"
    active_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    atomic_write_json(tmp_path / "global_config.json", {"active_persona": "sirius"})

    class Worker:
        def __init__(self) -> None:
            self.persona_dir = other_dir
            self.shutdown_called = False

        def shutdown(self) -> None:
            self.shutdown_called = True

    worker = Worker()
    server = WebUIServer(data_dir=tmp_path, persona_manager=worker)

    response = await server.api_persona_stop(SimpleNamespace())

    assert response.status == 200
    assert worker.shutdown_called is False


@pytest.mark.asyncio
async def test_webui_persona_activate_when_called_then_updates_server_active_persona(tmp_path):
    sirius_dir = tmp_path / "personas" / "sirius"
    other_dir = tmp_path / "personas" / "other"
    sirius_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    atomic_write_json(sirius_dir / "persona.json", {"name": "sirius"})
    atomic_write_json(other_dir / "persona.json", {"name": "other"})
    atomic_write_json(tmp_path / "global_config.json", {"active_persona": "other"})
    server = WebUIServer(data_dir=tmp_path)
    request = SimpleNamespace(match_info={"name": "sirius"})

    response = await server.api_persona_activate(request)

    assert response.status == 200
    assert server.persona_dir == sirius_dir


@pytest.mark.asyncio
async def test_persona_status_when_worker_pid_is_stale_then_not_running(tmp_path, monkeypatch):
    persona_dir = tmp_path / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    atomic_write_json(tmp_path / "global_config.json", {"active_persona": "sirius"})
    atomic_write_json(
        persona_dir / "engine_state" / "worker_status.json",
        {"status": "running", "pid": 12345},
    )
    monkeypatch.setattr(persona_manager, "_pid_exists", lambda pid: False)

    response = await persona_manager.api_persona_status(SimpleNamespace(), persona_dir)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["running"] is False
    assert payload["pid"] == 12345


@pytest.mark.asyncio
async def test_persona_start_when_not_running_then_spawns_worker_process(tmp_path, monkeypatch):
    persona_dir = tmp_path / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    atomic_write_json(persona_dir / "persona.json", {"name": "sirius"})
    atomic_write_json(tmp_path / "global_config.json", {"active_persona": "", "log_level": "debug"})
    monkeypatch.setattr(persona_manager, "_pid_exists", lambda pid: False)
    calls = []

    class FakeProcess:
        pid = 24680

    def fake_popen(command, stdout, stderr, **kwargs):
        calls.append({"command": command, "stdout": stdout, "stderr": stderr, "kwargs": kwargs})
        return FakeProcess()

    monkeypatch.setattr(persona_manager.subprocess, "Popen", fake_popen)

    response = await persona_manager.api_persona_start(SimpleNamespace(), persona_dir)
    payload = json.loads(response.text)
    saved = json.loads((tmp_path / "global_config.json").read_text(encoding="utf-8"))

    assert response.status == 200
    assert payload["success"] is True
    assert payload["started"] is True
    assert payload["pid"] == 24680
    assert saved["active_persona"] == "sirius"
    assert calls[0]["command"][1:5] == ["-m", "sirius_pulse.persona_worker", "--config", str(persona_dir)]
    assert calls[0]["command"][-2:] == ["--log-level", "DEBUG"]
    assert calls[0]["kwargs"]["cwd"] == str(tmp_path.parent)


@pytest.mark.asyncio
async def test_persona_stop_when_external_worker_is_running_then_sends_sigterm(tmp_path, monkeypatch):
    persona_dir = tmp_path / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    atomic_write_json(tmp_path / "global_config.json", {"active_persona": "sirius"})
    atomic_write_json(
        persona_dir / "engine_state" / "worker_status.json",
        {"status": "running", "pid": 24680},
    )
    kills = []

    monkeypatch.setattr(persona_manager, "_pid_exists", lambda pid: len(kills) == 0)
    monkeypatch.setattr(persona_manager.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    response = await persona_manager.api_persona_stop(SimpleNamespace(), persona_dir)
    payload = json.loads(response.text)
    saved = json.loads((tmp_path / "global_config.json").read_text(encoding="utf-8"))

    assert response.status == 200
    assert payload["success"] is True
    assert payload["stopped"] is True
    assert saved["active_persona"] == ""
    assert kills == [(24680, persona_manager.signal.SIGTERM)]


@pytest.mark.asyncio
async def test_webui_providers_get_when_registry_exists_then_returns_masked_providers(tmp_path):
    atomic_write_json(
        tmp_path / "providers" / "provider_keys.json",
        {
            "providers": {
                "aliyun-bailian": {
                    "type": "aliyun-bailian",
                    "api_key": "sk-secret",
                    "base_url": "https://dashscope.example",
                    "enabled": True,
                    "models": ["qwen-plus"],
                    "healthcheck_model": "qwen-plus",
                }
            }
        },
    )
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_providers_get(SimpleNamespace())
    payload = json.loads(response.text)

    assert payload["providers"] == [
        {
            "name": "aliyun-bailian",
            "type": "aliyun-bailian",
            "platform_type": "aliyun-bailian",
            "api_key": "sk-s****",
            "base_url": "https://dashscope.example",
            "enabled": True,
            "models": ["qwen-plus"],
            "healthcheck_model": "qwen-plus",
        }
    ]


@pytest.mark.asyncio
async def test_webui_providers_get_when_registry_uses_legacy_list_then_returns_providers(tmp_path):
    atomic_write_json(
        tmp_path / "providers" / "provider_keys.json",
        {
            "providers": [
                {
                    "name": "deepseek-main",
                    "platform_type": "deepseek",
                    "api_key": "sk-deepseek",
                    "enabled": True,
                }
            ]
        },
    )
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_providers_get(SimpleNamespace())
    payload = json.loads(response.text)

    assert payload["providers"] == [
        {
            "name": "deepseek-main",
            "type": "deepseek",
            "platform_type": "deepseek",
            "api_key": "sk-d****",
            "enabled": True,
        }
    ]


@pytest.mark.asyncio
async def test_webui_providers_post_when_key_is_masked_then_preserves_secret_and_reloads(
    tmp_path,
):
    atomic_write_json(
        tmp_path / "providers" / "provider_keys.json",
        {
            "providers": {
                "aliyun-bailian": {
                    "type": "aliyun-bailian",
                    "api_key": "sk-original",
                    "base_url": "https://old.example",
                    "models_url": "https://models.example",
                    "enabled": True,
                    "models": ["old-model"],
                    "healthcheck_model": "old-model",
                },
                "deepseek": {
                    "type": "deepseek",
                    "api_key": "sk-deleted",
                },
            }
        },
    )
    server = WebUIServer(data_dir=tmp_path)

    async def json_body():
        return {
            "providers": [
                {
                    "name": "aliyun-bailian",
                    "platform_type": "aliyun-bailian",
                    "api_key": "sk-o****",
                    "base_url": "https://new.example",
                    "enabled": False,
                    "models": ["qwen-plus"],
                    "healthcheck_model": "qwen-plus",
                }
            ]
        }

    response = await server.api_providers_post(SimpleNamespace(json=json_body))
    saved = json.loads((tmp_path / "providers" / "provider_keys.json").read_text(encoding="utf-8"))

    assert response.status == 200
    assert saved == {
        "providers": {
            "aliyun-bailian": {
                "type": "aliyun-bailian",
                "api_key": "sk-original",
                "base_url": "https://new.example",
                "models_url": "https://models.example",
                "enabled": False,
                "models": ["qwen-plus"],
                "healthcheck_model": "qwen-plus",
            }
        }
    }
    assert (tmp_path / "engine_state" / "reload_requested").read_text(encoding="utf-8") == "provider"
