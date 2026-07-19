from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sirius_pulse.webui.memory_api import (
    api_persona_diary_delete,
    api_persona_diary_get,
    api_persona_diary_post,
    api_persona_diary_put,
    api_persona_glossary_delete,
    api_persona_glossary_get,
    api_persona_glossary_post,
    api_persona_glossary_put,
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
    assert _payload(await api_persona_memory_dedupe_status(_request(), tmp_path))["worker_running"] is True
    assert (await api_persona_memory_dedupe_apply(_request({"job_id": payload["job_id"]}), tmp_path)).status == 409


@pytest.mark.asyncio
async def test_persona_diary_crud_roundtrip(tmp_path):
    response = await api_persona_diary_post(
        _request(
            {
                "group_id": "group-a",
                "summary": "一次重要讨论",
                "content": "大家确认了周末活动安排。",
                "keywords": ["活动", "周末"],
            }
        ),
        tmp_path,
    )
    created = _payload(response)["entry"]

    response = await api_persona_diary_put(
        _request(
            {"summary": "更新后的讨论", "keywords": "活动,更新"},
            match={"entry_id": created["entry_id"]},
        ),
        tmp_path,
    )
    assert _payload(response)["entry"]["summary"] == "更新后的讨论"

    response = await api_persona_diary_get(_request(query={"limit": "20", "offset": "0"}), tmp_path)
    payload = _payload(response)
    assert payload["total"] == 1
    assert payload["entries"][0]["keywords"] == ["活动", "更新"]

    response = await api_persona_diary_delete(
        _request(match={"entry_id": created["entry_id"]}),
        tmp_path,
    )
    assert _payload(response)["success"] is True

    response = await api_persona_diary_get(_request(query={"limit": "20", "offset": "0"}), tmp_path)
    assert _payload(response)["total"] == 0


@pytest.mark.asyncio
async def test_persona_glossary_crud_roundtrip(tmp_path):
    response = await api_persona_glossary_post(
        _request({"term": "月白", "definition": "人格的称呼", "confidence": 0.9}),
        tmp_path,
    )
    assert response.status == 201

    response = await api_persona_glossary_put(
        _request(
            {"term": "月白酱", "definition": "更亲昵的人格称呼"},
            match={"term": "月白"},
        ),
        tmp_path,
    )
    assert _payload(response)["term"]["term"] == "月白酱"

    response = await api_persona_glossary_get(
        _request(query={"limit": "20", "offset": "0"}), tmp_path
    )
    payload = _payload(response)
    assert payload["total"] == 1
    assert payload["terms"][0]["definition"] == "更亲昵的人格称呼"

    response = await api_persona_glossary_delete(
        _request(match={"term": "月白酱"}),
        tmp_path,
    )
    assert _payload(response)["success"] is True
