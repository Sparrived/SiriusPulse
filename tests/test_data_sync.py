"""管家端数据同步 API 测试。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sirius_pulse.network.data_sync_api import (
    api_data_batch_post,
    api_data_glossary_post,
    api_data_messages_post,
    api_data_snapshot_get,
    api_data_snapshot_post,
    api_data_users_post,
)


def _make_request(method: str = "GET", body: dict | None = None) -> MagicMock:
    """创建 mock request，支持 await request.json()。"""
    req = MagicMock()
    req.method = method
    if body is not None:
        req.json = AsyncMock(return_value=body)
    return req


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """创建带测试数据的人格目录。"""
    d = tmp_path / "personas" / "sirius"
    d.mkdir(parents=True)

    # persona.json
    (d / "persona.json").write_text(
        json.dumps({"name": "小星", "aliases": ["星星"]}),
        encoding="utf-8",
    )

    # engine_state
    state_dir = d / "engine_state"
    state_dir.mkdir()
    (state_dir / "group_timestamps.json").write_text(
        json.dumps({"group_1": "2024-01-01T00:00:00"}),
        encoding="utf-8",
    )

    # glossary
    glossary_dir = d / "glossary"
    glossary_dir.mkdir()
    (glossary_dir / "terms.json").write_text(
        json.dumps({"小星": {"term": "小星", "definition": "AI助手"}}),
        encoding="utf-8",
    )

    # archive
    archive_dir = d / "archive"
    archive_dir.mkdir()
    (archive_dir / "group_1.jsonl").write_text(
        '{"user_id": "u1", "role": "user", "content": "你好", "timestamp": "t1"}\n',
        encoding="utf-8",
    )

    return d


class TestSnapshotGet:
    """GET /api/data/snapshot 测试。"""

    @pytest.mark.asyncio
    async def test_returns_persona_and_state(self, data_dir: Path):
        req = _make_request()
        resp = await api_data_snapshot_get(req, data_dir)
        body = json.loads(resp.body)

        assert "snapshot" in body
        snap = body["snapshot"]
        assert snap["persona"]["name"] == "小星"
        assert "group_1" in snap["group_timestamps"]
        assert "小星" in snap["glossary"]

    @pytest.mark.asyncio
    async def test_returns_archives(self, data_dir: Path):
        req = _make_request()
        resp = await api_data_snapshot_get(req, data_dir)
        body = json.loads(resp.body)

        snap = body["snapshot"]
        assert "group_1" in snap["archives"]
        assert len(snap["archives"]["group_1"]) == 1
        assert snap["archives"]["group_1"][0]["content"] == "你好"

    @pytest.mark.asyncio
    async def test_empty_dir_returns_empty_snapshot(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()

        req = _make_request()
        resp = await api_data_snapshot_get(req, empty)
        body = json.loads(resp.body)

        snap = body["snapshot"]
        assert snap.get("persona") is None
        assert snap.get("glossary") is None


class TestSnapshotPost:
    """POST /api/data/snapshot 测试。"""

    @pytest.mark.asyncio
    async def test_saves_state_to_disk(self, data_dir: Path):
        state = {
            "persona": {"name": "新名字"},
            "assistant_emotion": {"valence": 0.5},
            "group_timestamps": {"group_2": "2024-06-01T00:00:00"},
            "working_memories": {
                "group_2": [{"user_id": "u2", "role": "user", "content": "测试"}]
            },
        }

        req = _make_request("POST", {"state": state})
        resp = await api_data_snapshot_post(req, data_dir)
        body = json.loads(resp.body)
        assert body["success"] is True

        # 验证写入
        saved_emotion = json.loads(
            (data_dir / "engine_state" / "assistant_emotion.json").read_text(encoding="utf-8")
        )
        assert saved_emotion["valence"] == 0.5

        saved_wm = json.loads(
            (data_dir / "engine_state" / "groups" / "group_2.json").read_text(encoding="utf-8")
        )
        assert saved_wm["group_id"] == "group_2"


class TestMessagesPost:
    """POST /api/data/messages 测试。"""

    @pytest.mark.asyncio
    async def test_appends_messages_to_archive(self, data_dir: Path):
        messages = [
            {"group_id": "group_1", "user_id": "u2", "role": "user", "content": "新消息"},
            {"group_id": "group_1", "user_id": "ai", "role": "assistant", "content": "回复"},
        ]

        req = _make_request("POST", {"messages": messages})
        resp = await api_data_messages_post(req, data_dir)
        body = json.loads(resp.body)
        assert body["count"] == 2

        # 验证追加
        lines = (data_dir / "archive" / "group_1.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3  # 原有1条 + 新增2条


class TestBatchPost:
    """POST /api/data/batch 测试。"""

    @pytest.mark.asyncio
    async def test_batch_working_memory(self, data_dir: Path):
        operations = [
            {
                "type": "working_memory",
                "data": {
                    "group_id": "group_3",
                    "entries": [{"user_id": "u1", "content": "测试"}],
                },
            }
        ]

        req = _make_request("POST", {"operations": operations})
        resp = await api_data_batch_post(req, data_dir)
        body = json.loads(resp.body)
        assert body["count"] == 1

        saved = json.loads(
            (data_dir / "engine_state" / "groups" / "group_3.json").read_text(encoding="utf-8")
        )
        assert saved["group_id"] == "group_3"

    @pytest.mark.asyncio
    async def test_batch_timestamps_merge(self, data_dir: Path):
        operations = [
            {
                "type": "timestamps",
                "data": {"group_2": "2024-07-01T00:00:00"},
            }
        ]

        req = _make_request("POST", {"operations": operations})
        resp = await api_data_batch_post(req, data_dir)
        body = json.loads(resp.body)
        assert body["success"] is True

        saved = json.loads(
            (data_dir / "engine_state" / "group_timestamps.json").read_text(encoding="utf-8")
        )
        # 原有的保留，新的合并
        assert "group_1" in saved
        assert "group_2" in saved


class TestGlossaryPost:
    """POST /api/data/glossary 测试。"""

    @pytest.mark.asyncio
    async def test_merges_glossary_terms(self, data_dir: Path):
        req = _make_request("POST", {"terms": {"新术语": {"term": "新术语", "definition": "解释"}}})
        resp = await api_data_glossary_post(req, data_dir)
        body = json.loads(resp.body)
        assert body["count"] == 2  # 原有1个 + 新增1个

        terms = json.loads(
            (data_dir / "glossary" / "terms.json").read_text(encoding="utf-8")
        )
        assert "小星" in terms
        assert "新术语" in terms
