from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sirius_pulse.core.bg_tasks_delayed import DelayedQueueTasks
from sirius_pulse.core.intent import RESOLUTION_TELL, IntentFileStore
from sirius_pulse.providers.base import ToolCall
from sirius_pulse.tools.builtin import interaction_with_master


class _FakeNapCatAdapter:
    def __init__(self, root: str = "123456") -> None:
        self.plugin_config = {"root": root}
        self.private_messages: list[tuple[str, str]] = []

    async def send_private_message(self, user_id: str, message: str) -> dict[str, object]:
        self.private_messages.append((user_id, message))
        return {"status": "ok", "message_id": 42}


class _Store:
    def __init__(self, token: str, **data: Any) -> None:
        self.data = {"public_status_token": token, **data}
        self.reloaded = False

    def reload(self) -> None:
        self.reloaded = True

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.body[:size]


def test_metadata_exposes_unified_master_interaction() -> None:
    assert interaction_with_master.TOOL_META["name"] == "interaction_with_master"
    assert interaction_with_master.TOOL_META["silent"] is False
    assert [param["name"] for param in interaction_with_master.TOOL_META["parameters"]] == [
        "action",
        "message",
        "device_id",
    ]
    assert set(interaction_with_master.TOOL_META["config"]) == {
        "public_status_token",
        "base_url",
        "timeout_seconds",
    }


def test_runtime_preserves_action_specific_interaction_behavior() -> None:
    tool = SimpleNamespace(
        name="interaction_with_master",
        source_path=Path(interaction_with_master.__file__),
        side_effect="external_write",
        retry_safe=True,
        silent=False,
    )
    message_call = interaction_with_master_call("message")
    status_call = interaction_with_master_call("status")

    assert DelayedQueueTasks._side_effect_name(tool, {"action": "message"}) == "external_write"
    assert DelayedQueueTasks._side_effect_name(tool, {"action": "status"}) == "read_only"
    assert DelayedQueueTasks._retry_safe(tool, {"action": "message"}) is False
    assert DelayedQueueTasks._retry_safe(tool, {"action": "status"}) is True
    assert DelayedQueueTasks._is_autonomous_message_tool(tool, {"action": "message"}) is True
    assert DelayedQueueTasks._is_autonomous_message_tool(tool, {"action": "status"}) is False
    assert DelayedQueueTasks._tool_is_silent(tool, message_call) is True
    assert DelayedQueueTasks._tool_is_silent(tool, status_call) is False


def interaction_with_master_call(action: str) -> ToolCall:
    return ToolCall(
        id=action,
        function_name="interaction_with_master",
        function_arguments=json.dumps({"action": action}),
    )


@pytest.mark.asyncio
async def test_message_action_sends_raw_private_message() -> None:
    adapter = _FakeNapCatAdapter(root="10001")

    message = "刚刚发生了一件很有趣的事，想跟你讲一下。"
    result = await interaction_with_master.run(
        action="message",
        message=message,
        bridge=adapter,
        chat_context={
            "chat_type": "group",
            "chat_id": "20002",
            "group_id": "20002",
            "user_id": "30003",
        },
    )

    assert result["success"] is True
    assert adapter.private_messages == [("10001", message)]
    assert "通知" not in adapter.private_messages[0][1]
    assert "紧急度" not in adapter.private_messages[0][1]


@pytest.mark.asyncio
async def test_message_action_when_root_is_missing_returns_clear_failure() -> None:
    adapter = _FakeNapCatAdapter(root="")

    result = await interaction_with_master.run(action="message", message="hello", bridge=adapter)

    assert result["success"] is False
    assert "root QQ" in result["error"]
    assert adapter.private_messages == []


class _EngineContext:
    def __init__(self, work_path: Path) -> None:
        self._work_path = work_path

    def get_work_path(self) -> str:
        return str(self._work_path)


@pytest.mark.asyncio
async def test_autonomous_turn_may_reach_the_tool_through_the_real_executor(
    tmp_path: Path, monkeypatch
) -> None:
    """自主回合要真的能走到这一步：豁免标记、bridge 与 engine_context 都得在。"""
    from sirius_pulse.memory.user.unified_models import UnifiedUser
    from sirius_pulse.tools import ToolExecutor, ToolInvocationContext
    from sirius_pulse.tools.registry import ToolRegistry

    adapter = _FakeNapCatAdapter(root="10001")
    # 注册表会重新加载模块，所以这里要打规范模块上的函数；直接改
    # interaction_with_master._is_quiet_now 打不到那个新实例。
    monkeypatch.setattr("sirius_pulse.core.autonomy.is_quiet_hours", lambda now: False)

    registry = ToolRegistry()
    registry.load_from_directory(tmp_path / "tools", auto_install_deps=False, include_builtin=True)
    tool = registry.get("interaction_with_master")
    assert tool is not None and tool.allowed_when_self_initiated is True

    executor = ToolExecutor(work_path=tmp_path)
    executor.set_bridge("napcat", adapter)
    executor.set_engine_context(_EngineContext(tmp_path))

    result = await executor.execute_async(
        tool,
        {"action": "message", "message": "想跟你说一声，今天那个数据我核完了。"},
        invocation_context=ToolInvocationContext(
            caller=UnifiedUser(user_id="autonomy", name="autonomy"),
            self_initiated=True,
        ),
    )

    assert result.success is True, result.error
    assert adapter.private_messages == [("10001", "想跟你说一声，今天那个数据我核完了。")]


@pytest.mark.asyncio
async def test_night_message_on_her_own_time_waits_until_morning(
    tmp_path: Path, monkeypatch
) -> None:
    """夜里她主动想到的话不能 03:00 砸过去，先存成意图，早上再走投递。"""
    adapter = _FakeNapCatAdapter(root="10001")
    monkeypatch.setattr(interaction_with_master, "_is_quiet_now", lambda: True)

    result = await interaction_with_master.run(
        action="message",
        message="今天算出来一个挺有意思的东西，早上跟你讲。",
        bridge=adapter,
        engine_context=_EngineContext(tmp_path),
        invocation_context=SimpleNamespace(self_initiated=True),
    )

    assert adapter.private_messages == []
    assert result["success"] is True
    assert result["deferred"] is True
    store = IntentFileStore(tmp_path).load()
    carried = store.all()[0]
    assert carried.what == "今天算出来一个挺有意思的东西，早上跟你讲。"
    assert carried.resolution == RESOLUTION_TELL
    assert carried.audience == "private_10001"
    assert carried.audience_label == "主人（私聊）"


@pytest.mark.asyncio
async def test_night_message_still_sends_when_it_is_a_live_reply(
    tmp_path: Path, monkeypatch
) -> None:
    """对话正在进行时她在回话，不是在自言自语：这条不该被压到早上。"""
    adapter = _FakeNapCatAdapter(root="10001")
    monkeypatch.setattr(interaction_with_master, "_is_quiet_now", lambda: True)

    result = await interaction_with_master.run(
        action="message",
        message="在的，我马上看。",
        bridge=adapter,
        engine_context=_EngineContext(tmp_path),
        invocation_context=SimpleNamespace(self_initiated=False),
    )

    assert result["success"] is True
    assert adapter.private_messages == [("10001", "在的，我马上看。")]
    assert IntentFileStore(tmp_path).load().all() == []


@pytest.mark.asyncio
async def test_night_message_without_engine_context_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    """存不下来就不发——降级成"照发"正好违背了夜间静默本身。"""
    adapter = _FakeNapCatAdapter(root="10001")
    monkeypatch.setattr(interaction_with_master, "_is_quiet_now", lambda: True)

    result = await interaction_with_master.run(
        action="message",
        message="夜里想到的事",
        bridge=adapter,
        invocation_context=SimpleNamespace(self_initiated=True),
    )

    assert result["success"] is False
    assert adapter.private_messages == []


@pytest.mark.asyncio
async def test_status_action_reads_token_from_store_and_redacts_private_fields(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["authorization"] = request.headers["Authorization"]
        seen["timeout"] = timeout
        return _Response(
            {
                "generated_at": "2026-07-19T08:00:00Z",
                "devices": [
                    {
                        "id": "computer-1",
                        "name": "工作电脑",
                        "platform": "windows",
                        "status": "online",
                        "heartbeat_age_seconds": 4,
                        "foreground_app": {
                            "name": "编辑器",
                            "process_name": "secret.exe",
                            "package_name": "private.package",
                        },
                        "location": {
                            "country": "中国",
                            "city": "上海",
                            "latitude": 31.2,
                        },
                        "metrics": {"activity_state": "busy", "cpu_percent": 12.5},
                    }
                ],
            }
        )

    monkeypatch.delenv("MDS_PUBLIC_STATUS_TOKEN", raising=False)
    monkeypatch.setattr(interaction_with_master, "urlopen", fake_urlopen)
    store = _Store("store-token")

    result = await interaction_with_master.run(action="status", data_store=store)

    assert result["success"] is True
    assert result["summary"] == "已读取 1 台设备的主人当前状态参考。"
    assert result["text_blocks"][0].startswith("主人当前状态参考（MDS 生成时间：")
    assert "设备 工作电脑：在线" in result["text_blocks"][0]
    assert store.reloaded is True
    assert seen == {
        "url": "https://sparrived.xyz/mds/api/v1/public/snapshot",
        "authorization": "Bearer store-token",
        "timeout": 10,
    }
    device = result["devices"][0]
    assert device["location"] == {"country": "中国", "city": "上海"}
    assert device["foreground_app"] == {"name": "编辑器"}
    assert "secret.exe" not in json.dumps(result, ensure_ascii=False)
    assert "private.package" not in json.dumps(result, ensure_ascii=False)
    assert "store-token" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_status_action_filters_by_device_id(monkeypatch) -> None:
    monkeypatch.setenv("MDS_PUBLIC_STATUS_TOKEN", "env-token")
    monkeypatch.setattr(
        interaction_with_master,
        "urlopen",
        lambda request, timeout: _Response(
            {
                "generated_at": "now",
                "devices": [
                    {"id": "first", "status": "online"},
                    {"id": "second", "status": "offline"},
                ],
            }
        ),
    )

    result = await interaction_with_master.run(action="status", device_id="second")

    assert [device["id"] for device in result["devices"]] == ["second"]


@pytest.mark.asyncio
async def test_status_action_prefers_persona_configuration_over_environment(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["authorization"] = request.headers["Authorization"]
        seen["timeout"] = timeout
        return _Response({"generated_at": "now", "devices": []})

    monkeypatch.setenv("MDS_PUBLIC_STATUS_TOKEN", "env-token")
    monkeypatch.setenv("MDS_API_BASE_URL", "https://env.example/mds")
    monkeypatch.setattr(interaction_with_master, "urlopen", fake_urlopen)

    result = await interaction_with_master.run(
        action="status",
        data_store=_Store(
            "config-token",
            base_url="https://config.example/mds",
            timeout_seconds=15,
        ),
    )

    assert result["success"] is True
    assert seen == {
        "url": "https://config.example/mds/api/v1/public/snapshot",
        "authorization": "Bearer config-token",
        "timeout": 15,
    }


@pytest.mark.asyncio
async def test_status_action_reports_missing_token_without_network(monkeypatch) -> None:
    monkeypatch.delenv("MDS_PUBLIC_STATUS_TOKEN", raising=False)
    called = False

    def fail_urlopen(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be called without a token")

    monkeypatch.setattr(interaction_with_master, "urlopen", fail_urlopen)

    result = await interaction_with_master.run(action="status")

    assert result["success"] is False
    assert "MDS_PUBLIC_STATUS_TOKEN" in result["error"]
    assert called is False


@pytest.mark.asyncio
async def test_status_action_reports_malformed_json(monkeypatch) -> None:
    monkeypatch.setenv("MDS_PUBLIC_STATUS_TOKEN", "env-token")
    response = _Response.__new__(_Response)
    response.body = b"not-json"
    monkeypatch.setattr(interaction_with_master, "urlopen", lambda request, timeout: response)

    result = await interaction_with_master.run(action="status")

    assert result == {
        "success": False,
        "error": "MDS 返回的不是合法 JSON。",
        "summary": "主人当前状态读取失败",
    }
