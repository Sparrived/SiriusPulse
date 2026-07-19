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


def _proxy(tmp_path: Path, *, allow_mutations: bool = False):
    module = _load_proxy_module()
    config = tmp_path / "container-admin.json"
    config.write_text(
        '{"allowed_containers":["nginx"],"allow_mutations":'
        + ("true" if allow_mutations else "false")
        + "}",
        encoding="utf-8",
    )
    return module, module.ContainerAdminProxy(config)


def test_proxy_rejects_unallowed_containers_without_running_docker(tmp_path, monkeypatch):
    module, proxy = _proxy(tmp_path)
    monkeypatch.setattr(proxy, "_run_docker", lambda *_: (_ for _ in ()).throw(AssertionError()))

    result = proxy.handle({"action": "logs", "container": "postgres", "tail_lines": 20})

    assert result == {"success": False, "error": "目标容器不在宿主机允许列表中"}


def test_proxy_blocks_mutations_until_host_policy_allows_them(tmp_path, monkeypatch):
    module, proxy = _proxy(tmp_path)
    monkeypatch.setattr(proxy, "_run_docker", lambda *_: (_ for _ in ()).throw(AssertionError()))

    result = proxy.handle({"action": "restart", "container": "nginx"})

    assert result == {"success": False, "error": "宿主机策略未允许变更容器状态"}


def test_proxy_uses_fixed_docker_arguments_for_an_allowed_mutation(tmp_path, monkeypatch):
    module, proxy = _proxy(tmp_path, allow_mutations=True)
    seen = {}

    def fake_run(arguments, config):
        seen["arguments"] = arguments
        return "nginx"

    monkeypatch.setattr(proxy, "_run_docker", fake_run)

    result = proxy.handle({"action": "restart", "container": "nginx"})

    assert result == {"success": True, "output": "nginx"}
    assert seen["arguments"] == ["restart", "nginx"]


def test_proxy_filters_list_results_to_the_host_allowlist(tmp_path, monkeypatch):
    module, proxy = _proxy(tmp_path)
    monkeypatch.setattr(
        proxy,
        "_run_docker",
        lambda *_: "nginx\tUp 2 hours\tnginx:latest\npostgres\tUp 2 hours\tpostgres:16",
    )

    result = proxy.handle({"action": "list"})

    assert result == {
        "success": True,
        "containers": [{"name": "nginx", "status": "Up 2 hours", "image": "nginx:latest"}],
    }


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
