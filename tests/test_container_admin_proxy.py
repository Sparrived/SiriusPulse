from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_proxy_module():
    path = Path(__file__).parents[1] / "scripts" / "container_admin_proxy.py"
    spec = importlib.util.spec_from_file_location("container_admin_proxy", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _proxy(tmp_path: Path, *, allow_mutations: bool | None = None):
    module = _load_proxy_module()
    config = tmp_path / "container-admin.json"
    config.write_text(
        "{}"
        if allow_mutations is None
        else '{"allow_mutations":' + str(allow_mutations).lower() + "}",
        encoding="utf-8",
    )
    return module, module.ContainerAdminProxy(config)


def test_proxy_rejects_invalid_containers_without_running_docker(tmp_path, monkeypatch):
    module, proxy = _proxy(tmp_path)
    monkeypatch.setattr(proxy, "_run_docker", lambda *_: (_ for _ in ()).throw(AssertionError()))

    result = proxy.handle({"action": "logs", "container": "../../host", "tail_lines": 20})

    assert result == {"success": False, "error": "无效的容器名称"}


def test_proxy_blocks_mutations_until_host_policy_allows_them(tmp_path, monkeypatch):
    module, proxy = _proxy(tmp_path, allow_mutations=False)
    monkeypatch.setattr(proxy, "_run_docker", lambda *_: (_ for _ in ()).throw(AssertionError()))

    result = proxy.handle({"action": "restart", "container": "nginx"})

    assert result == {"success": False, "error": "宿主机策略未允许变更容器状态"}


def test_proxy_allows_mutations_by_default_with_fixed_docker_arguments(tmp_path, monkeypatch):
    module, proxy = _proxy(tmp_path)
    seen = {}

    def fake_run(arguments, config):
        seen["arguments"] = arguments
        return "postgres"

    monkeypatch.setattr(proxy, "_run_docker", fake_run)

    result = proxy.handle({"action": "restart", "container": "postgres"})

    assert result == {"success": True, "output": "postgres"}
    assert seen["arguments"] == ["restart", "postgres"]


def test_proxy_allows_fixed_readonly_minecraft_log_commands(tmp_path, monkeypatch):
    module, proxy = _proxy(tmp_path)
    seen = {}

    def fake_run(arguments, config):
        seen["arguments"] = arguments
        return "java.lang.RuntimeException: crash"

    monkeypatch.setattr(proxy, "_run_docker", fake_run)

    result = proxy.handle(
        {
            "action": "exec_readonly",
            "container": "minecraft",
            "command": ["tail", "-n", "200", "/data/logs/latest.log"],
        }
    )

    assert result == {"success": True, "output": "java.lang.RuntimeException: crash"}
    assert seen["arguments"] == [
        "exec",
        "minecraft",
        "tail",
        "-n",
        "200",
        "/data/logs/latest.log",
    ]


def test_proxy_rejects_unbounded_exec_before_running_docker(tmp_path, monkeypatch):
    module, proxy = _proxy(tmp_path)
    monkeypatch.setattr(proxy, "_run_docker", lambda *_: (_ for _ in ()).throw(AssertionError()))

    shell = proxy.handle(
        {"action": "exec_readonly", "container": "minecraft", "command": ["sh", "-lc", "id"]}
    )
    outside_data = proxy.handle(
        {"action": "exec_readonly", "container": "minecraft", "command": ["cat", "/etc/passwd"]}
    )
    recursive_grep = proxy.handle(
        {
            "action": "exec_readonly",
            "container": "minecraft",
            "command": ["grep", "-r", "error", "/data/logs/latest.log"],
        }
    )

    assert shell == {"success": False, "error": "只读 exec 仅允许 ls、cat、head、tail 或 grep"}
    assert outside_data == {"success": False, "error": "只读 exec 只能访问容器内的 /data 路径"}
    assert recursive_grep == {"success": False, "error": "只读 grep 仅允许 -i、-n、-E 选项"}


def test_proxy_lists_all_host_containers(tmp_path, monkeypatch):
    module, proxy = _proxy(tmp_path)
    monkeypatch.setattr(
        proxy,
        "_run_docker",
        lambda *_: "nginx\tUp 2 hours\tnginx:latest\npostgres\tExited (0) 2 hours ago\tpostgres:16",
    )

    result = proxy.handle({"action": "list"})

    assert result == {
        "success": True,
        "containers": [
            {"name": "nginx", "status": "Up 2 hours", "image": "nginx:latest"},
            {"name": "postgres", "status": "Exited (0) 2 hours ago", "image": "postgres:16"},
        ],
    }


def test_proxy_passes_a_valid_container_name_filter_to_docker(tmp_path, monkeypatch):
    module, proxy = _proxy(tmp_path)
    seen = {}

    def fake_run(arguments, config):
        seen["arguments"] = arguments
        return "minecraft\tUp 1 hour\tminecraft-server-minecraft"

    monkeypatch.setattr(proxy, "_run_docker", fake_run)

    result = proxy.handle({"action": "list", "name_filter": "minecraft"})

    assert result["success"] is True
    assert seen["arguments"] == [
        "ps",
        "-a",
        "--filter",
        "name=minecraft",
        "--format",
        "{{.Names}}\t{{.Status}}\t{{.Image}}",
    ]


def test_proxy_inspect_keeps_diagnostics_and_returns_status_card_fields(tmp_path, monkeypatch):
    module, proxy = _proxy(tmp_path)
    seen = []

    def fake_run(arguments, config):
        seen.append(arguments)
        if arguments[1:3] == ["--format", "{{json .State}}"]:
            return '{"Status":"running","Error":""}'
        if arguments[0] == "inspect":
            return (
                "nginx:latest\trunning\ttrue\t0\t2026-07-20T02:00:00Z\t"
                "0001-01-01T00:00:00Z\thealthy\tunless-stopped"
            )
        return "1.25%\t128MiB / 1GiB\t12.5%\t4kB / 3kB\t0B / 0B\t8"

    monkeypatch.setattr(proxy, "_run_docker", fake_run)
    monkeypatch.setattr(
        module,
        "_host_status",
        lambda: {
            "cpu_percent": "8.0%",
            "memory_usage": "4.0 GiB / 16.0 GiB (25.0%)",
            "disk_usage": "20.0 GiB / 100.0 GiB (20.0%)",
            "load_1": "0.42",
            "uptime": "3天 4小时",
        },
    )

    result = proxy.handle({"action": "inspect", "container": "nginx"})

    assert result == {
        "success": True,
        "output": '{"Status":"running","Error":""}',
        "status": {
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
        },
    }
    assert seen == [
        ["inspect", "--format", "{{json .State}}", "nginx"],
        ["inspect", "--format", module._STATUS_FORMAT, "nginx"],
        ["stats", "--no-stream", "--format", module._STATS_FORMAT, "nginx"],
    ]
