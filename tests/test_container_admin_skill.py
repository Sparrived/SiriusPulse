from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.skills.builtin import container_admin
from sirius_pulse.skills.executor import SkillExecutor
from sirius_pulse.skills.models import SkillInvocationContext, SkillResult
from sirius_pulse.skills.registry import SkillRegistry


def _developer_context() -> SkillInvocationContext:
    developer = UnifiedUser(user_id="dev-1", name="Dev", metadata={"is_developer": True})
    return SkillInvocationContext(caller=developer, developer_profiles=[developer])


@pytest.mark.asyncio
async def test_container_admin_forwards_a_bounded_request_to_the_proxy(monkeypatch):
    seen = {}

    def fake_request(request):
        seen.update(request)
        return {"success": True, "output": "nginx restarted\n"}

    monkeypatch.setattr(container_admin, "_request_host_proxy", fake_request)

    result = await container_admin.run(
        action="restart",
        container="nginx",
        tail_lines=999,
        invocation_context=_developer_context(),
    )

    assert result["success"] is True
    assert result["text_blocks"] == ["nginx restarted"]
    assert seen == {"action": "restart", "container": "nginx", "tail_lines": 200}


@pytest.mark.asyncio
async def test_container_admin_rejects_invalid_targets_before_contacting_proxy(monkeypatch):
    monkeypatch.setattr(
        container_admin, "_request_host_proxy", lambda _: pytest.fail("unexpected call")
    )

    result = await container_admin.run(
        action="logs",
        container="../../host",
        invocation_context=_developer_context(),
    )

    assert result["success"] is False
    assert "container" in result["error"]


@pytest.mark.asyncio
async def test_container_admin_requires_a_developer_context():
    with pytest.raises(PermissionError):
        await container_admin.run(action="list")


class _Registry:
    def get(self, name):
        return object() if name == "file_upload" else None


class _Executor:
    def __init__(self) -> None:
        self.calls = []

    async def execute_async(self, skill, params, invocation_context=None):
        self.calls.append((skill, params, invocation_context))
        return SkillResult(success=True, internal_metadata={"message_id": 42})


class _EngineContext:
    def __init__(self) -> None:
        self.skill_registry = _Registry()
        self.skill_executor = _Executor()


class _NapCatAdapter:
    def __init__(self) -> None:
        self.calls = []

    async def send_group_msg(self, group_id, message):
        self.calls.append((group_id, message))
        return {"data": {"message_id": 88}}


@pytest.mark.asyncio
async def test_inspect_keeps_diagnostics_and_sends_a_private_status_card(monkeypatch, tmp_path):
    status = {
        "name": "nginx",
        "image": "nginx:latest",
        "status": "running",
        "running": "true",
        "exit_code": "0",
        "started_at": "2026-07-20T02:00:00Z",
        "finished_at": "0001-01-01T00:00:00Z",
        "health": "healthy",
        "restart_policy": "unless-stopped",
        "resources": {
            "cpu_percent": "1.25%",
            "memory_usage": "128MiB / 1GiB",
            "memory_percent": "12.5%",
            "network_io": "4kB / 3kB",
            "block_io": "0B / 0B",
            "pids": "8",
        },
        "host": {
            "cpu_percent": "8.0%",
            "memory_usage": "4.0 GiB / 16.0 GiB (25.0%)",
            "disk_usage": "20.0 GiB / 100.0 GiB (20.0%)",
            "load_1": "0.42",
            "uptime": "3天 4小时",
        },
    }
    card_path = tmp_path / "container_status.png"
    engine_context = _EngineContext()

    monkeypatch.setattr(
        container_admin,
        "_request_host_proxy",
        lambda request: {"success": True, "output": '{"Status":"running"}', "status": status},
    )

    async def fake_render(rendered_status, data_store):
        assert rendered_status == status
        return card_path

    monkeypatch.setattr(container_admin, "_render_status_card", fake_render)

    result = await container_admin.run(
        action="inspect",
        container="nginx",
        chat_context={"chat_type": "private", "chat_id": "1001"},
        engine_context=engine_context,
        invocation_context=_developer_context(),
    )

    assert result["success"] is True
    assert result["text_blocks"][0] == '{"Status":"running"}'
    assert result["internal_metadata"]["message_id"] == 42
    assert result["internal_metadata"]["chat_type"] == "private"
    assert engine_context.skill_executor.calls[0][1] == {
        "action": "image",
        "image_path": str(card_path),
    }


@pytest.mark.asyncio
async def test_inspect_sends_a_local_card_through_the_real_executor(monkeypatch, tmp_path: Path):
    status = {
        "name": "minecraft",
        "image": "minecraft-server:latest",
        "status": "running",
        "running": "true",
        "exit_code": "0",
        "started_at": "2026-07-20T02:00:00Z",
        "finished_at": "0001-01-01T00:00:00Z",
        "health": "healthy",
        "restart_policy": "unless-stopped",
    }
    card_path = tmp_path / "container_status.png"
    card_path.write_bytes(b"png")
    adapter = _NapCatAdapter()
    registry = SkillRegistry()
    registry.load_from_directory(tmp_path / "skills", auto_install_deps=False, include_builtin=True)
    executor = SkillExecutor(tmp_path)
    executor.set_bridge("napcat", adapter)
    executor.set_chat_context(group_id="9001", user_id="qq_1001")
    engine_context = SimpleNamespace(skill_registry=registry, skill_executor=executor)

    monkeypatch.setattr(
        container_admin,
        "_request_host_proxy",
        lambda _: {"success": True, "output": '{"Status":"running"}', "status": status},
    )

    async def fake_render(*_args):
        return card_path

    monkeypatch.setattr(container_admin, "_render_status_card", fake_render)

    result = await container_admin.run(
        action="inspect",
        container="minecraft",
        chat_context={"chat_type": "group", "chat_id": "9001"},
        engine_context=engine_context,
        invocation_context=_developer_context(),
    )

    assert result["success"] is True
    assert result["internal_metadata"]["card_sent"] is True
    assert result["internal_metadata"]["message_id"] == 88
    assert adapter.calls == [
        (
            "9001",
            [
                {
                    "type": "image",
                    "data": {
                        "file": f"base64://{base64.b64encode(card_path.read_bytes()).decode('ascii')}"
                    },
                }
            ],
        )
    ]


@pytest.mark.asyncio
async def test_inspect_preserves_diagnostics_without_a_chat_context(monkeypatch):
    status = {
        "name": "nginx",
        "image": "nginx:latest",
        "status": "running",
        "running": "true",
        "exit_code": "0",
        "started_at": "2026-07-20T02:00:00Z",
        "finished_at": "0001-01-01T00:00:00Z",
        "health": "healthy",
        "restart_policy": "unless-stopped",
    }
    monkeypatch.setattr(
        container_admin,
        "_request_host_proxy",
        lambda _: {"success": True, "output": '{"Status":"running"}', "status": status},
    )
    monkeypatch.setattr(
        container_admin, "_render_status_card", lambda *_: pytest.fail("unexpected call")
    )

    result = await container_admin.run(
        action="inspect",
        container="nginx",
        invocation_context=_developer_context(),
    )

    assert result["success"] is True
    assert result["text_blocks"][0] == '{"Status":"running"}'
    assert result["internal_metadata"]["card_sent"] is False


@pytest.mark.asyncio
async def test_inspect_preserves_diagnostics_when_card_delivery_fails(monkeypatch, tmp_path):
    status = {
        "name": "nginx",
        "image": "nginx:latest",
        "status": "running",
        "running": "true",
        "exit_code": "0",
        "started_at": "2026-07-20T02:00:00Z",
        "finished_at": "0001-01-01T00:00:00Z",
        "health": "healthy",
        "restart_policy": "unless-stopped",
    }
    engine_context = _EngineContext()

    async def failed_delivery(*args, **kwargs):
        return SkillResult(success=False, error="NapCat unavailable")

    monkeypatch.setattr(engine_context.skill_executor, "execute_async", failed_delivery)
    monkeypatch.setattr(
        container_admin,
        "_request_host_proxy",
        lambda _: {"success": True, "output": '{"Status":"running"}', "status": status},
    )

    async def fake_render(*args):
        return tmp_path / "container_status.png"

    monkeypatch.setattr(container_admin, "_render_status_card", fake_render)

    result = await container_admin.run(
        action="inspect",
        container="nginx",
        chat_context={"chat_type": "group", "chat_id": "9001"},
        engine_context=engine_context,
        invocation_context=_developer_context(),
    )

    assert result["success"] is True
    assert result["text_blocks"][0] == '{"Status":"running"}'
    assert result["internal_metadata"]["card_sent"] is False
    assert result["internal_metadata"]["card_error"] == "NapCat unavailable"


def test_status_card_html_escapes_container_fields():
    status = {
        "name": "<script>alert(1)</script>",
        "image": "image&tag",
        "status": "running",
        "running": "true",
        "exit_code": "0",
        "started_at": "2026-07-20T02:00:00Z",
        "finished_at": "",
        "health": "healthy",
        "restart_policy": "always",
    }

    normalized = container_admin._normalize_status(status)
    assert normalized is not None
    card_html = container_admin._build_status_card_html(normalized)

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in card_html
    assert "image&amp;tag" in card_html


def test_container_admin_is_registered_as_a_developer_only_builtin(tmp_path):
    registry = SkillRegistry()
    registry.load_from_directory(tmp_path / "skills", auto_install_deps=False, include_builtin=True)

    skill = registry.get("container_admin")

    assert skill is not None
    assert skill.developer_only is True
    assert [item.name for item in skill.parameters] == ["action", "container", "tail_lines"]
    assert "inspect" in skill.parameters[0].choices
    assert "query" not in skill.parameters[0].choices
