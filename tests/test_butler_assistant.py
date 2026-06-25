"""助手-管家通信协议与服务的业务行为测试。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sirius_pulse.network.protocol import (
    ButlerMessage,
    MessageType,
    make_error,
    make_heartbeat,
    make_release,
    make_takeover,
    make_takeover_ack,
)

# ------------------------------------------------------------------
# 协议序列化/反序列化
# ------------------------------------------------------------------


class TestProtocolSerialization:
    """协议消息的序列化与反序列化。"""

    def test_takeover_message_roundtrip(self):
        msg = make_takeover(token="secret123")
        json_str = msg.to_json()
        restored = ButlerMessage.from_json(json_str)

        assert restored.type == MessageType.TAKEOVER
        assert restored.payload["token"] == "secret123"

    def test_heartbeat_message_roundtrip(self):
        msg = make_heartbeat()
        json_str = msg.to_json()
        restored = ButlerMessage.from_json(json_str)

        assert restored.type == MessageType.HEARTBEAT

    def test_release_message_roundtrip(self):
        msg = make_release()
        json_str = msg.to_json()
        restored = ButlerMessage.from_json(json_str)

        assert restored.type == MessageType.RELEASE

    def test_error_message_roundtrip(self):
        msg = make_error("认证失败")
        json_str = msg.to_json()
        restored = ButlerMessage.from_json(json_str)

        assert restored.type == MessageType.ERROR
        assert restored.payload["reason"] == "认证失败"

    def test_takeover_ack_roundtrip(self):
        msg = make_takeover_ack(success=True)
        json_str = msg.to_json()
        restored = ButlerMessage.from_json(json_str)

        assert restored.type == MessageType.TAKEOVER_ACK

    def test_takeover_nack_roundtrip(self):
        msg = make_takeover_ack(success=False)
        json_str = msg.to_json()
        restored = ButlerMessage.from_json(json_str)

        assert restored.type == MessageType.TAKEOVER_NACK

    def test_from_json_invalid_json_raises(self):
        with pytest.raises(ValueError, match="无效的 JSON"):
            ButlerMessage.from_json("not json")

    def test_from_json_missing_type_raises(self):
        with pytest.raises(ValueError, match="缺少 type 字段"):
            ButlerMessage.from_json('{"persona_name": "sirius"}')

    def test_from_json_unknown_type_raises(self):
        with pytest.raises(ValueError, match="未知消息类型"):
            ButlerMessage.from_json('{"type": "unknown_type"}')

    def test_timestamp_preserved(self):
        ts = 1700000000.0
        msg = ButlerMessage(type=MessageType.HEARTBEAT, timestamp=ts)
        json_str = msg.to_json()
        restored = ButlerMessage.from_json(json_str)
        assert restored.timestamp == ts

    def test_persona_name_defaults_to_empty(self):
        """persona_name 向后兼容，默认为空字符串。"""
        msg = ButlerMessage(type=MessageType.HEARTBEAT)
        assert msg.persona_name == ""

        msg_with_name = ButlerMessage(type=MessageType.HEARTBEAT, persona_name="sirius")
        assert msg_with_name.persona_name == "sirius"


# ------------------------------------------------------------------
# ButlerServer 逻辑
# ------------------------------------------------------------------


class TestButlerServerLogic:
    """ButlerServer 的核心业务逻辑。"""

    def _make_server(self, tmp_path: Path, token: str | None = None):
        from sirius_pulse.network.butler_server import ButlerServer

        data_dir = tmp_path / "personas" / "sirius"
        data_dir.mkdir(parents=True, exist_ok=True)
        return ButlerServer(
            host="127.0.0.1",
            port=0,
            data_dir=data_dir,
            token=token,
        )

    @pytest.mark.asyncio
    async def test_takeover_writes_enabled_flag_false(self, tmp_path):
        """接管时写入 enabled=0 标志文件。"""
        server = self._make_server(tmp_path)

        # 模拟 WebSocket
        mock_ws = AsyncMock()
        ack = await server._handle_takeover(make_takeover(), mock_ws)

        assert ack.type == MessageType.TAKEOVER_ACK
        assert server.is_taken_over

        # 验证标志文件
        flag_path = tmp_path / "personas" / "sirius" / "engine_state" / "enabled"
        assert flag_path.exists()
        assert flag_path.read_text(encoding="utf-8") == "0"

    @pytest.mark.asyncio
    async def test_release_writes_enabled_flag_true(self, tmp_path):
        """释放时写入 enabled=1 标志文件。"""
        server = self._make_server(tmp_path)

        mock_ws = AsyncMock()
        await server._handle_takeover(make_takeover(), mock_ws)

        server._release_persona()

        assert not server.is_taken_over
        flag_path = tmp_path / "personas" / "sirius" / "engine_state" / "enabled"
        assert flag_path.read_text(encoding="utf-8") == "1"

    @pytest.mark.asyncio
    async def test_takeover_nonexistent_data_dir_fails(self, tmp_path):
        """数据目录不存在时接管应失败。"""
        from sirius_pulse.network.butler_server import ButlerServer

        server = ButlerServer(
            host="127.0.0.1",
            port=0,
            data_dir=tmp_path / "nonexistent",
        )
        mock_ws = AsyncMock()

        ack = await server._handle_takeover(make_takeover(), mock_ws)

        assert ack.type == MessageType.ERROR
        assert "不存在" in ack.payload["reason"]

    @pytest.mark.asyncio
    async def test_takeover_already_taken_fails(self, tmp_path):
        """已被接管时不能再次被接管。"""
        server = self._make_server(tmp_path)
        mock_ws = AsyncMock()

        await server._handle_takeover(make_takeover(), mock_ws)
        ack = await server._handle_takeover(make_takeover(), mock_ws)

        assert ack.type == MessageType.ERROR
        assert "已被其他助手接管" in ack.payload["reason"]

    @pytest.mark.asyncio
    async def test_takeover_with_wrong_token_fails(self, tmp_path):
        """token 不匹配时应拒绝。"""
        server = self._make_server(tmp_path, token="correct_token")
        mock_ws = AsyncMock()

        ack = await server._handle_takeover(
            make_takeover(token="wrong_token"),
            mock_ws,
        )

        assert ack.type == MessageType.ERROR
        assert "认证失败" in ack.payload["reason"]

    @pytest.mark.asyncio
    async def test_takeover_with_correct_token_succeeds(self, tmp_path):
        """token 匹配时应成功。"""
        server = self._make_server(tmp_path, token="correct_token")
        mock_ws = AsyncMock()

        ack = await server._handle_takeover(
            make_takeover(token="correct_token"),
            mock_ws,
        )

        assert ack.type == MessageType.TAKEOVER_ACK


# ------------------------------------------------------------------
# PersonaWorker set_adapter_enabled
# ------------------------------------------------------------------


class TestPersonaWorkerAdapterControl:
    """PersonaWorker 的 adapter 开关控制。"""

    def test_set_adapter_enabled_true(self):
        """set_adapter_enabled(True) 应启用所有 adapter。"""
        from sirius_pulse.persona_worker import PersonaWorker

        worker = PersonaWorker.__new__(PersonaWorker)
        mock_adapter1 = MagicMock()
        mock_adapter1._enabled = False
        mock_adapter2 = MagicMock()
        mock_adapter2._enabled = False
        worker._adapters = [mock_adapter1, mock_adapter2]

        worker.set_adapter_enabled(True)

        assert mock_adapter1._enabled is True
        assert mock_adapter2._enabled is True

    def test_set_adapter_enabled_false(self):
        """set_adapter_enabled(False) 应禁用所有 adapter。"""
        from sirius_pulse.persona_worker import PersonaWorker

        worker = PersonaWorker.__new__(PersonaWorker)
        mock_adapter1 = MagicMock()
        mock_adapter1._enabled = True
        mock_adapter2 = MagicMock()
        mock_adapter2._enabled = True
        worker._adapters = [mock_adapter1, mock_adapter2]

        worker.set_adapter_enabled(False)

        assert mock_adapter1._enabled is False
        assert mock_adapter2._enabled is False
