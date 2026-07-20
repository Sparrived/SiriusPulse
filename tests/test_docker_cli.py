from __future__ import annotations

import pytest

from sirius_pulse.skills.builtin import _docker_cli


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            ["ps"],
            {"action": "list", "container": "", "tail_lines": 100, "all": False},
        ),
        (
            ["container", "ls", "--all"],
            {"action": "list", "container": "", "tail_lines": 100, "all": True},
        ),
        (
            ["inspect", "minecraft"],
            {"action": "inspect", "container": "minecraft", "tail_lines": 100},
        ),
        (
            ["logs", "-n50", "nginx"],
            {"action": "logs", "container": "nginx", "tail_lines": 50},
        ),
        (
            ["exec", "minecraft", "tail", "-n", "200", "/data/logs/latest.log"],
            {
                "action": "exec_readonly",
                "container": "minecraft",
                "tail_lines": 100,
                "command": ["tail", "-n", "200", "/data/logs/latest.log"],
            },
        ),
        (
            ["restart", "sirius-pulse-v2-test"],
            {"action": "restart", "container": "sirius-pulse-v2-test", "tail_lines": 100},
        ),
    ],
)
def test_docker_cli_translates_native_safe_commands_to_fixed_proxy_requests(arguments, expected):
    assert _docker_cli.build_request(arguments) == expected


@pytest.mark.parametrize(
    "arguments",
    [
        ["rm", "nginx"],
        ["container", "prune"],
        ["compose", "down"],
        ["system", "prune"],
        ["run", "alpine"],
        ["image", "rm", "nginx:latest"],
        ["volume", "rm", "data"],
        ["network", "rm", "bridge"],
    ],
)
def test_docker_cli_rejects_destructive_or_unbounded_commands(arguments):
    with pytest.raises(_docker_cli.DockerCommandError, match="不允许 Docker 操作"):
        _docker_cli.build_request(arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        ["logs", "--tail", "0", "nginx"],
        ["logs", "--follow", "nginx"],
        ["start", "nginx", "postgres"],
        ["ps", "--format", "{{.ID}}"],
        ["inspect", "../../host"],
    ],
)
def test_docker_cli_rejects_unsupported_options_and_targets(arguments):
    with pytest.raises(_docker_cli.DockerCommandError):
        _docker_cli.build_request(arguments)


def test_docker_cli_prints_proxy_output(monkeypatch, capsys):
    monkeypatch.setattr(
        _docker_cli,
        "request_host_proxy",
        lambda request: {
            "success": True,
            "containers": [
                {"name": "nginx", "status": "Up 1 hour", "image": "nginx:latest"}
            ],
        },
    )

    exit_code = _docker_cli.main(["ps"])

    assert exit_code == 0
    assert capsys.readouterr().out == "nginx\tUp 1 hour\tnginx:latest\n"


def test_docker_cli_emits_inspect_status_as_an_internal_marker(monkeypatch, capsys):
    status = {"name": "nginx", "status": "running"}
    monkeypatch.setattr(
        _docker_cli,
        "request_host_proxy",
        lambda request: {"success": True, "output": '{"Status":"running"}', "status": status},
    )

    exit_code = _docker_cli.main(["inspect", "nginx"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == '{"Status":"running"}\n'
    assert captured.err == _docker_cli.format_inspect_status_marker(status) + "\n"


def test_docker_cli_returns_nonzero_for_rejected_commands(capsys):
    exit_code = _docker_cli.main(["rm", "nginx"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "不允许 Docker 操作" in captured.err
