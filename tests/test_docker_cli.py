from __future__ import annotations

import pytest

from sirius_pulse.tools.builtin._internal import _docker_cli


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            ["ps"],
            {
                "action": "list",
                "container": "",
                "tail_lines": 100,
                "all": False,
                "name_filters": [],
            },
        ),
        (
            ["container", "ls", "--all"],
            {
                "action": "list",
                "container": "",
                "tail_lines": 100,
                "all": True,
                "name_filters": [],
            },
        ),
        (
            ["ps", "-a", "--filter", "name=mc", "--filter=name=minecraft"],
            {
                "action": "list",
                "container": "",
                "tail_lines": 100,
                "all": True,
                "name_filters": ["mc", "minecraft"],
            },
        ),
        (
            [
                "ps",
                "-a",
                "--filter",
                "name=mc",
                "--format",
                "table {{.Names}}\\t{{.Status}}\\t{{.Ports}}",
            ],
            {
                "action": "list",
                "container": "",
                "tail_lines": 100,
                "all": True,
                "name_filters": ["mc"],
                "format": "table {{.Names}}\\t{{.Status}}\\t{{.Ports}}",
            },
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
                "action": "docker",
                "arguments": ["exec", "minecraft", "tail", "-n", "200", "/data/logs/latest.log"],
            },
        ),
        (
            ["restart", "sirius-pulse-v2-test"],
            {"action": "restart", "container": "sirius-pulse-v2-test", "tail_lines": 100},
        ),
        (
            ["stats", "--no-stream", "minecraft"],
            {"action": "stats", "container": "minecraft", "tail_lines": 100},
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
        ["logs", "--tail", "0", "nginx"],
        ["logs", "--follow", "nginx"],
        ["start", "nginx", "postgres"],
        ["ps", "--format", "{{json .}}"],
    ],
)
def test_docker_cli_preserves_untranslated_commands_for_proxy_policy(arguments):
    """未被翻译的 docker 子命令/选项原样透传给代理策略，由代理侧决定是否放行。"""
    assert _docker_cli.build_request(arguments) == {"action": "docker", "arguments": arguments}


def test_docker_cli_still_rejects_invalid_common_inspect_target():
    with pytest.raises(_docker_cli.DockerCommandError):
        _docker_cli.build_request(["inspect", "../../host"])


def test_docker_cli_prints_proxy_output(monkeypatch, capsys):
    monkeypatch.setattr(
        _docker_cli,
        "request_host_proxy",
        lambda request: {
            "success": True,
            "containers": [{"name": "nginx", "status": "Up 1 hour", "image": "nginx:latest"}],
        },
    )

    exit_code = _docker_cli.main(["ps"])

    assert exit_code == 0
    assert capsys.readouterr().out == "nginx\tUp 1 hour\tnginx:latest\n"


def test_docker_cli_treats_a_closed_output_pipe_as_success(monkeypatch):
    class ClosedPipe:
        closed = False

        def write(self, value):
            raise BrokenPipeError

        def close(self):
            self.closed = True

    pipe = ClosedPipe()
    monkeypatch.setattr(
        _docker_cli,
        "request_host_proxy",
        lambda request: {"success": True, "output": "crash report"},
    )
    monkeypatch.setattr(_docker_cli.sys, "stdout", pipe)

    assert _docker_cli.main(["ps"]) == 0
    assert pipe.closed is True


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


def test_docker_cli_returns_nonzero_for_proxy_rejected_commands(monkeypatch, capsys):
    monkeypatch.setattr(
        _docker_cli,
        "request_host_proxy",
        lambda request: {"success": False, "error": "拒绝不可逆 Docker 删除操作"},
    )
    exit_code = _docker_cli.main(["rm", "nginx"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "拒绝不可逆 Docker 删除操作" in captured.err
