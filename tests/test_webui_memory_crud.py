from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sirius_pulse.webui.memory_api import (
    api_persona_memory_dedupe_apply,
    api_persona_memory_dedupe_scan,
    api_persona_memory_dedupe_status,
)


def _request(body: dict | None = None, *, query: dict | None = None, match: dict | None = None):
    async def json_body():
        return body or {}

    return SimpleNamespace(json=json_body, query=query or {}, match_info=match or {})


def _payload(response):
    return json.loads(response.text)


@pytest.mark.asyncio
async def test_memory_dedupe_scan_lifecycle(tmp_path, monkeypatch):
    import sirius_pulse.webui.memory_api as memory_api

    monkeypatch.setattr(memory_api, "_is_persona_running", lambda _: False)
    assert (await api_persona_memory_dedupe_scan(_request(), tmp_path)).status == 409

    monkeypatch.setattr(memory_api, "_is_persona_running", lambda _: True)
    response = await api_persona_memory_dedupe_scan(_request(), tmp_path)
    payload = _payload(response)
    assert response.status == 202
    request_data = json.loads(
        (tmp_path / "engine_state" / "memory_dedupe" / "request.json").read_text("utf-8")
    )
    assert request_data == {"action": "scan", "job_id": payload["job_id"]}
    assert (await api_persona_memory_dedupe_scan(_request(), tmp_path)).status == 409
    assert (
        _payload(await api_persona_memory_dedupe_status(_request(), tmp_path))["worker_running"]
        is True
    )
    assert (
        await api_persona_memory_dedupe_apply(_request({"job_id": payload["job_id"]}), tmp_path)
    ).status == 409
