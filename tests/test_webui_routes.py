from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web

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
