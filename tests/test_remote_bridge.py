"""远程存储桥接测试。"""

from __future__ import annotations

import json
from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sirius_pulse.network.remote_bridge import RemoteStorageBridge


def _mock_aiohttp_session(response_status: int = 200, response_json: dict | None = None):
    """创建正确结构的 aiohttp.ClientSession mock。"""
    mock_resp = AsyncMock()
    mock_resp.status = response_status
    mock_resp.json = AsyncMock(return_value=response_json or {})
    mock_resp.text = AsyncMock(return_value=json.dumps(response_json or {}))

    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_cm)
    mock_session.get = MagicMock(return_value=mock_cm)

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    return session_cm


class TestRemoteStorageBridgeSnapshot:
    """快照加载/保存。"""

    @pytest.mark.asyncio
    async def test_load_snapshot_parses_response(self):
        bridge = RemoteStorageBridge("http://butler:9500")

        snapshot_data = {
            "persona": {"name": "小星"},
            "working_memories": {"g1": [{"content": "hello"}]},
            "group_timestamps": {"g1": "2024-01-01"},
            "glossary": {"术语": {"term": "术语"}},
        }

        mock_session = _mock_aiohttp_session(200, {"snapshot": snapshot_data})

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await bridge.load_snapshot()

        assert result["persona"]["name"] == "小星"
        assert bridge.get_persona()["name"] == "小星"
        assert bridge.get_working_memories()["g1"][0]["content"] == "hello"
        assert bridge.get_group_timestamps()["g1"] == "2024-01-01"

    @pytest.mark.asyncio
    async def test_load_snapshot_returns_empty_on_failure(self):
        bridge = RemoteStorageBridge("http://butler:9500")

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(side_effect=Exception("连接失败"))
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await bridge.load_snapshot()

        assert result == {}

    @pytest.mark.asyncio
    async def test_save_snapshot_posts_state(self):
        bridge = RemoteStorageBridge("http://butler:9500")

        mock_session = _mock_aiohttp_session(200)

        state = {"persona": {"name": "小星"}, "working_memories": {}}

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await bridge.save_snapshot(state)

        assert result is True


class TestRemoteStorageBridgePushMethods:
    """推送方法。"""

    def test_push_message_adds_critical(self):
        bridge = RemoteStorageBridge("http://butler:9500")
        bridge.push_message("g1", {"content": "hello"})
        assert bridge.write_buffer.pending_count == 1

    def test_push_token_usage_adds_to_buffer(self):
        bridge = RemoteStorageBridge("http://butler:9500")
        bridge.push_token_usage({"model": "gpt-4o", "tokens": 100})
        assert bridge.write_buffer.pending_count == 1

    def test_push_working_memory_adds_to_buffer(self):
        bridge = RemoteStorageBridge("http://butler:9500")
        bridge.push_working_memory("g1", [{"content": "hello"}])
        assert bridge.write_buffer.pending_count == 1

    def test_push_user_update_adds_critical(self):
        bridge = RemoteStorageBridge("http://butler:9500")
        bridge.push_user_update("u1", {"name": "用户"})
        assert bridge.write_buffer.pending_count == 1


class TestRemoteStorageBridgeSnapshotAccessors:
    """快照数据访问器。"""

    def test_accessors_return_empty_when_no_snapshot(self):
        bridge = RemoteStorageBridge("http://butler:9500")
        assert bridge.get_persona() is None
        assert bridge.get_working_memories() == {}
        assert bridge.get_group_timestamps() == {}
        assert bridge.get_glossary() == {}
        assert bridge.get_users() == []

    def test_accessors_return_snapshot_data(self):
        bridge = RemoteStorageBridge("http://butler:9500")
        bridge._snapshot = {
            "persona": {"name": "小星"},
            "working_memories": {"g1": [{"content": "hello"}]},
            "assistant_emotion": {"valence": 0.5},
            "glossary": {"术语": {"term": "术语"}},
        }
        assert bridge.get_persona()["name"] == "小星"
        assert bridge.get_assistant_emotion()["valence"] == 0.5
        assert "术语" in bridge.get_glossary()


class TestRemoteStorageBridgeLifecycle:
    """生命周期。"""

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        bridge = RemoteStorageBridge("http://butler:9500", flush_interval=0.1)
        await bridge.start()
        assert bridge.write_buffer._flush_task is not None

        await bridge.stop()
        assert bridge.write_buffer._flush_task.done()
