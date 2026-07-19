from __future__ import annotations

import subprocess
from pathlib import Path

from sirius_pulse.skills.builtin import bash


class _Store:
    def __init__(self, root: Path, **data: object) -> None:
        self.store_path = root / "skill_data" / "bash.json"
        self.data = data
        self.reloaded = False

    def reload(self) -> None:
        self.reloaded = True

    def get(self, key: str, default: object = None) -> object:
        return self.data.get(key, default)


def _fake_bash(monkeypatch, stdout: bytes = b"ok\n") -> dict[str, object]:
    calls: dict[str, object] = {}

    def fake_run(args, **kwargs):
        calls["args"] = args
        calls.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(bash, "_find_bash", lambda: "bash")
    monkeypatch.setattr(bash.subprocess, "run", fake_run)
    return calls


def test_bash_skill_runs_standard_shell_syntax_anywhere_in_container(monkeypatch, tmp_path: Path):
    calls = _fake_bash(monkeypatch)
    store = _Store(tmp_path)
    container_cwd = tmp_path.parent
    command = """cat > report.txt <<'EOF'
今日小报
EOF
cat /proc/meminfo 2>/dev/null | grep -E '^(MemTotal|MemAvailable)' || echo unavailable"""

    result = bash.run(command, cwd=str(container_cwd), data_store=store)

    assert result["success"] is True
    assert result["text_blocks"] == ["ok\n"]
    assert calls["cwd"] == str(container_cwd.resolve())
    assert calls["args"][-1] == command


def test_bash_skill_rejects_invalid_command_and_missing_cwd(monkeypatch, tmp_path: Path):
    calls = _fake_bash(monkeypatch)
    store = _Store(tmp_path)

    empty = bash.run("", data_store=store)
    missing_cwd = bash.run("pwd", cwd=str(tmp_path / "missing"), data_store=store)
    oversized = bash.run("x" * 4_001, data_store=store)

    assert empty["success"] is False
    assert missing_cwd["success"] is False
    assert oversized["success"] is False
    assert "args" not in calls


def test_bash_skill_allows_commands_not_present_in_old_whitelist(monkeypatch, tmp_path: Path):
    calls = _fake_bash(monkeypatch)
    store = _Store(tmp_path)

    result = bash.run("free -h | head -3", data_store=store)

    assert result["success"] is True
    assert calls["args"][-1] == "free -h | head -3"


def test_bash_skill_clamps_output_to_persona_policy(monkeypatch, tmp_path: Path):
    _fake_bash(monkeypatch, stdout=b"x" * 400)
    store = _Store(tmp_path, max_output_chars=300)

    result = bash.run("pwd", max_output_chars=5000, data_store=store)

    assert result["success"] is True
    assert "[输出已截断]" in result["text_blocks"][0]
    assert result["internal_metadata"]["truncated"] is True
    assert store.reloaded is True
