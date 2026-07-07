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
    api_persona_user_delete,
    api_persona_user_put,
    api_persona_users_get,
)


def _request(body: dict | None = None, *, query: dict | None = None, match: dict | None = None):
    async def json_body():
        return body or {}

    return SimpleNamespace(json=json_body, query=query or {}, match_info=match or {})


def _payload(response):
    return json.loads(response.text)


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


@pytest.mark.asyncio
async def test_persona_user_profile_update_list_and_delete(tmp_path):
    response = await api_persona_user_put(
        _request(
            {
                "group_id": "group-a",
                "name": "Alice",
                "engagement_rate": 0.75,
                "interaction_count": 12,
            },
            match={"user_id": "10001"},
        ),
        tmp_path,
    )
    assert _payload(response)["user"]["group_id"] == "group-a"

    response = await api_persona_users_get(_request(query={"limit": "20", "offset": "0"}), tmp_path)
    payload = _payload(response)
    assert payload["total"] == 1
    assert payload["users"][0]["group_id"] == "group-a"
    assert payload["users"][0]["name"] == "Alice"

    response = await api_persona_user_delete(
        _request(query={"group_id": "group-a"}, match={"user_id": "10001"}),
        tmp_path,
    )
    assert _payload(response)["deleted"] == 1
