from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sirius_pulse.skills.builtin import _docker_cli
from sirius_pulse.skills.builtin import bash
from sirius_pulse.skills.models import SkillResult


class _Store:
    def __init__(self, root: Path, **data: object) -> None:
        self.store_path = root / "skill_data" / "bash.json"
        self.data = data
        self.reloaded = False

    def reload(self) -> None:
        self.reloaded = True

    def get(self, key: str, default: object = None) -> object:
        return self.data.get(key, default)


def _fake_bash(
    monkeypatch, stdout: bytes = b"ok\n", stderr: bytes = b""
) -> dict[str, object]:
    calls: dict[str, object] = {}

    def fake_run(args, **kwargs):
        calls["args"] = args
        calls.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(bash, "_find_bash", lambda: "bash")
    monkeypatch.setattr(bash.subprocess, "run", fake_run)
    return calls


@pytest.mark.asyncio
async def test_bash_skill_runs_standard_shell_syntax_anywhere_in_container(monkeypatch, tmp_path: Path):
    calls = _fake_bash(monkeypatch)
    store = _Store(tmp_path)
    container_cwd = tmp_path.parent
    command = """cat > report.txt <<'EOF'
今日小报
EOF
cat /proc/meminfo 2>/dev/null | grep -E '^(MemTotal|MemAvailable)' || echo unavailable"""

    result = await bash.run(command, cwd=str(container_cwd), data_store=store)

    assert result["success"] is True
    assert result["text_blocks"] == ["ok\n"]
    assert calls["cwd"] == str(container_cwd.resolve())
    assert calls["args"][-1].endswith(command)
    assert "docker()" in calls["args"][-1]


@pytest.mark.asyncio
async def test_bash_skill_rejects_invalid_command_and_missing_cwd(monkeypatch, tmp_path: Path):
    calls = _fake_bash(monkeypatch)
    store = _Store(tmp_path)

    empty = await bash.run("", data_store=store)
    missing_cwd = await bash.run("pwd", cwd=str(tmp_path / "missing"), data_store=store)
    oversized = await bash.run("x" * 4_001, data_store=store)

    assert empty["success"] is False
    assert missing_cwd["success"] is False
    assert oversized["success"] is False
    assert "args" not in calls


@pytest.mark.asyncio
async def test_bash_skill_allows_commands_not_present_in_old_whitelist(monkeypatch, tmp_path: Path):
    calls = _fake_bash(monkeypatch)
    store = _Store(tmp_path)

    result = await bash.run("free -h | head -3", data_store=store)

    assert result["success"] is True
    assert calls["args"][-1].endswith("free -h | head -3")


@pytest.mark.asyncio
async def test_bash_skill_makes_the_restricted_docker_cli_available(monkeypatch, tmp_path: Path):
    calls = _fake_bash(monkeypatch)

    result = await bash.run("docker logs --tail 20 nginx | grep error", data_store=_Store(tmp_path))

    assert result["success"] is True
    assert (
        f'{shlex.quote(sys.executable)} -m sirius_pulse.skills.builtin._docker_cli "$@"'
        in calls["args"][-1]
    )
    assert calls["args"][-1].endswith("docker logs --tail 20 nginx | grep error")
    assert result["internal_metadata"]["docker_bridge_enabled"] is True


@pytest.mark.asyncio
async def test_bash_skill_clamps_output_to_persona_policy(monkeypatch, tmp_path: Path):
    _fake_bash(monkeypatch, stdout=b"x" * 400)
    store = _Store(tmp_path, max_output_chars=300)

    result = await bash.run("pwd", max_output_chars=5000, data_store=store)

    assert result["success"] is True
    assert "[输出已截断]" in result["text_blocks"][0]
    assert result["internal_metadata"]["truncated"] is True
    assert store.reloaded is True


@pytest.mark.asyncio
async def test_bash_docker_inspect_sends_a_status_card_and_hides_internal_marker(
    monkeypatch, tmp_path: Path
):
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
    marker = _docker_cli.format_inspect_status_marker(status).encode("utf-8") + b"\n"
    _fake_bash(monkeypatch, stdout=b'{"Status":"running"}\n', stderr=marker)
    card_path = tmp_path / "container_status.png"
    sent: dict[str, object] = {}

    async def fake_render(rendered_status, data_store):
        assert rendered_status["name"] == "nginx"
        return card_path

    class _Registry:
        def get(self, name):
            return object() if name == "file_upload" else None

    class _Executor:
        async def execute_async(self, skill, params, invocation_context=None):
            sent["params"] = params
            return SkillResult(success=True, internal_metadata={"message_id": 42})

    monkeypatch.setattr(bash._container_status_card, "render_status_card", fake_render)
    engine_context = SimpleNamespace(skill_registry=_Registry(), skill_executor=_Executor())

    result = await bash.run(
        "docker inspect nginx",
        data_store=_Store(tmp_path),
        chat_context={"chat_type": "group", "chat_id": "9001"},
        engine_context=engine_context,
    )

    assert result["success"] is True
    assert sent["params"] == {"action": "image", "image_path": str(card_path)}
    assert result["internal_metadata"]["status_cards"][0]["sent"] is True
    assert result["internal_metadata"]["status_cards"][0]["message_id"] == 42
    assert "__SIRIUS_DOCKER_INSPECT_STATUS__" not in result["text_blocks"][0]
    assert "nginx：running，健康检查 healthy" in result["text_blocks"][0]


def test_bash_preserves_an_invalid_status_marker_as_regular_output():
    raw = b"before\n__SIRIUS_DOCKER_INSPECT_STATUS__:not-base64\nafter\n"

    output, statuses, truncated = bash._decode_output_with_inspect_status(raw, 8_000)

    assert output == raw.decode("utf-8")
    assert statuses == []
    assert truncated is False
