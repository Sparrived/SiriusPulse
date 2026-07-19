from __future__ import annotations

import subprocess
from pathlib import Path

from sirius_pulse.skills.builtin import bash


class _Store:
    def __init__(self, root: Path, **data: object) -> None:
        self.store_path = root / "skill_data" / "bash.json"
        self.data = data

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


def test_bash_skill_runs_validated_pipeline_in_persona_workspace(monkeypatch, tmp_path: Path):
    calls = _fake_bash(monkeypatch)
    store = _Store(tmp_path)

    result = bash.run("printf hello | cat", data_store=store)

    assert result["success"] is True
    assert result["text_blocks"] == ["ok\n"]
    assert calls["cwd"] == str(tmp_path.resolve())
    assert calls["args"][-1] == "printf hello | cat"


def test_bash_skill_rejects_shell_escape_and_outside_paths(monkeypatch, tmp_path: Path):
    calls = _fake_bash(monkeypatch)
    store = _Store(tmp_path)

    chained = bash.run("pwd; whoami", data_store=store)
    outside = bash.run("cat ../README.md", data_store=store)
    nested_shell = bash.run("bash -lc pwd", data_store=store)

    assert chained["success"] is False
    assert outside["success"] is False
    assert nested_shell["success"] is False
    assert "args" not in calls


def test_bash_skill_requires_explicit_write_policy(monkeypatch, tmp_path: Path):
    _fake_bash(monkeypatch)
    store = _Store(tmp_path)

    denied = bash.run("printf hi | tee note.txt", data_store=store)
    store.data["allow_write_commands"] = True
    allowed = bash.run("printf hi | tee note.txt", data_store=store)

    assert denied["success"] is False
    assert allowed["success"] is True


def test_bash_skill_clamps_output_to_persona_policy(monkeypatch, tmp_path: Path):
    _fake_bash(monkeypatch, stdout=b"x" * 400)
    store = _Store(tmp_path, max_output_chars=300)

    result = bash.run("pwd", max_output_chars=5000, data_store=store)

    assert result["success"] is True
    assert "[输出已截断]" in result["text_blocks"][0]
    assert result["internal_metadata"]["truncated"] is True
