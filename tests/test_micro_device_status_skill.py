from __future__ import annotations

import json
from typing import Any

from sirius_pulse.skills.builtin import micro_device_status


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


def test_metadata_exposes_developer_status_for_persona_sensing() -> None:
    assert micro_device_status.SKILL_META["name"] == "developer_status"
    assert "developer_only" not in micro_device_status.SKILL_META
    assert "人格需要感知开发者当前状态时使用" in micro_device_status.SKILL_META["description"]
    assert set(micro_device_status.SKILL_META["config"]) == {
        "public_status_token",
        "base_url",
        "timeout_seconds",
    }


def test_run_reads_token_from_store_and_redacts_private_public_fields(monkeypatch) -> None:
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
    monkeypatch.setattr(micro_device_status, "urlopen", fake_urlopen)
    store = _Store("store-token")

    result = micro_device_status.run(data_store=store)

    assert result["success"] is True
    assert result["summary"] == "已读取 1 台设备的开发者当前状态参考。"
    assert result["text_blocks"][0].startswith("开发者当前状态参考（MDS 生成时间：")
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


def test_run_filters_by_device_id(monkeypatch) -> None:
    monkeypatch.setenv("MDS_PUBLIC_STATUS_TOKEN", "env-token")
    monkeypatch.setattr(
        micro_device_status,
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

    result = micro_device_status.run(device_id="second")

    assert [device["id"] for device in result["devices"]] == ["second"]


def test_run_prefers_persona_configuration_over_environment(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["authorization"] = request.headers["Authorization"]
        seen["timeout"] = timeout
        return _Response({"generated_at": "now", "devices": []})

    monkeypatch.setenv("MDS_PUBLIC_STATUS_TOKEN", "env-token")
    monkeypatch.setenv("MDS_API_BASE_URL", "https://env.example/mds")
    monkeypatch.setattr(micro_device_status, "urlopen", fake_urlopen)

    result = micro_device_status.run(
        data_store=_Store(
            "config-token",
            base_url="https://config.example/mds",
            timeout_seconds=15,
        )
    )

    assert result["success"] is True
    assert seen == {
        "url": "https://config.example/mds/api/v1/public/snapshot",
        "authorization": "Bearer config-token",
        "timeout": 15,
    }


def test_run_reports_missing_token_without_network(monkeypatch) -> None:
    monkeypatch.delenv("MDS_PUBLIC_STATUS_TOKEN", raising=False)
    called = False

    def fail_urlopen(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be called without a token")

    monkeypatch.setattr(micro_device_status, "urlopen", fail_urlopen)

    result = micro_device_status.run()

    assert result["success"] is False
    assert "MDS_PUBLIC_STATUS_TOKEN" in result["error"]
    assert called is False


def test_run_reports_malformed_json(monkeypatch) -> None:
    monkeypatch.setenv("MDS_PUBLIC_STATUS_TOKEN", "env-token")
    response = _Response.__new__(_Response)
    response.body = b"not-json"
    monkeypatch.setattr(micro_device_status, "urlopen", lambda request, timeout: response)

    result = micro_device_status.run()

    assert result == {
        "success": False,
        "error": "MDS 返回的不是合法 JSON。",
        "summary": "开发者当前状态读取失败",
    }
