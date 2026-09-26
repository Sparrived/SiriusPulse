from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sirius_pulse.memory.units import MemoryUnit, MemoryUnitFileStore
from sirius_pulse.webui.memory_api import (
    api_persona_memory_dedupe_apply,
    api_persona_memory_dedupe_scan,
    api_persona_memory_dedupe_status,
    api_persona_memory_unit_put,
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


@pytest.mark.asyncio
async def test_editing_a_unit_keeps_the_groups_vectors(tmp_path):
    """人工编辑只改元数据，不能把该群向量变成没人引用的孤儿文件。

    线上向量以独立 sidecar 存放。WebUI 若在写回时丢掉 ``vector_file`` 引用，整组
    单元就会失去语义检索能力，而单元看起来「一切正常」——静默失效。
    """
    store = MemoryUnitFileStore(tmp_path)
    keep = MemoryUnit(
        unit_id="mem-keep",
        group_id="group-a",
        created_at="2026-01-01T00:00:00+00:00",
        summary="Alice prefers concise replies.",
        keywords=["concise"],
        embedding=[0.5] * 8,
    )
    edit = MemoryUnit(
        unit_id="mem-edit",
        group_id="group-a",
        created_at="2026-01-02T00:00:00+00:00",
        summary="Alice asked about deployment.",
        keywords=["deploy"],
        embedding=[0.25] * 8,
    )
    store.save("group-a", [keep, edit])

    response = await api_persona_memory_unit_put(
        _request({"summary": "Alice asked about the deployment."}, match={"unit_id": "mem-edit"}),
        tmp_path,
    )

    assert response.status == 200
    reloaded = {unit.unit_id: unit for unit in MemoryUnitFileStore(tmp_path).load("group-a")}
    assert reloaded["mem-keep"].embedding == [0.5] * 8, "未被编辑的单元必须保住向量"
    assert reloaded["mem-edit"].summary == "Alice asked about the deployment."
    # 文本变了，旧向量必须作废（否则过期向量会参与语义检索），但不能影响别的单元。
    assert reloaded["mem-edit"].embedding is None
