#!/usr/bin/env python3
"""Host-side restricted Docker proxy for the Sirius Bash Docker bridge."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

_ACTIONS = {"list", "inspect", "logs", "start", "stop", "restart", "exec_readonly"}
_MUTATIONS = {"start", "stop", "restart"}
_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_READONLY_EXEC_TOOLS = {"ls", "cat", "head", "tail", "grep"}
_DATA_ROOT = PurePosixPath("/data")
_MAX_REQUEST_BYTES = 4096
_MAX_OUTPUT_CHARS = 50_000
_STATUS_FORMAT = (
    "{{.Config.Image}}\t{{.State.Status}}\t{{.State.Running}}\t{{.State.ExitCode}}\t"
    "{{.State.StartedAt}}\t{{.State.FinishedAt}}\t{{if .State.Health}}{{.State.Health.Status}}{{end}}\t"
    "{{.HostConfig.RestartPolicy.Name}}"
)
_STATS_FORMAT = "{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}\t{{.BlockIO}}\t{{.PIDs}}"


class ProxyError(Exception):
    """Expected request or Docker error."""


class ContainerAdminProxy:
    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path

    def handle(self, payload: Any) -> dict[str, Any]:
        try:
            if not isinstance(payload, dict):
                raise ProxyError("请求必须是 JSON 对象")
            config = self._load_config()
            action = str(payload.get("action") or "").strip().lower()
            if action not in _ACTIONS:
                raise ProxyError("不支持的容器操作")
            if action in _MUTATIONS and not config["allow_mutations"]:
                raise ProxyError("宿主机策略未允许变更容器状态")

            target = str(payload.get("container") or "").strip()
            if action == "list":
                if target:
                    raise ProxyError("list 操作不能指定 container")
                return self._list_containers(
                    config,
                    all_containers=payload.get("all") is not False,
                    name_filter=self._name_filter(payload.get("name_filter")),
                )
            if not _CONTAINER_NAME.fullmatch(target):
                raise ProxyError("无效的容器名称")

            if action == "inspect":
                return self._inspect_container(target, config)
            elif action == "logs":
                tail_lines = self._tail_lines(payload.get("tail_lines"), config["max_log_lines"])
                output = self._run_docker(["logs", "--tail", str(tail_lines), target], config)
            elif action == "exec_readonly":
                command = self._readonly_exec_command(
                    payload.get("command"), maximum_lines=config["max_log_lines"]
                )
                output = self._run_docker(["exec", target, *command], config)
            else:
                output = self._run_docker([action, target], config)
            return {"success": True, "output": output}
        except ProxyError as exc:
            return {"success": False, "error": str(exc)}

    def _load_config(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProxyError(f"未找到代理配置: {self._config_path}") from exc
        except json.JSONDecodeError as exc:
            raise ProxyError(f"代理配置不是有效 JSON: {exc.msg}") from exc
        if not isinstance(raw, dict):
            raise ProxyError("代理配置必须是 JSON 对象")

        return {
            "allow_mutations": raw.get("allow_mutations", True) is not False,
            "max_log_lines": self._bounded_int(raw.get("max_log_lines", 200), 1, 200),
            "timeout_seconds": self._bounded_int(raw.get("timeout_seconds", 15), 1, 60),
        }

    @staticmethod
    def _bounded_int(value: Any, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = minimum
        return max(minimum, min(maximum, number))

    def _tail_lines(self, value: Any, maximum: int) -> int:
        return self._bounded_int(value, 1, maximum)

    @staticmethod
    def _name_filter(value: Any) -> str:
        if value is None or value == "":
            return ""
        if not isinstance(value, str) or not _CONTAINER_NAME.fullmatch(value):
            raise ProxyError("无效的容器名称过滤条件")
        return value

    def _readonly_exec_command(self, value: Any, *, maximum_lines: int) -> list[str]:
        if not isinstance(value, list) or len(value) < 2 or not all(isinstance(item, str) for item in value):
            raise ProxyError("只读 exec 请求必须指定日志命令和 /data 路径")
        tool, *args = value
        if tool not in _READONLY_EXEC_TOOLS:
            raise ProxyError("只读 exec 仅允许 ls、cat、head、tail 或 grep")
        if tool == "ls":
            return self._readonly_ls(args)
        if tool == "cat":
            return [tool, *self._data_paths(args)]
        if tool in {"head", "tail"}:
            return self._readonly_line_reader(tool, args, maximum_lines)
        return self._readonly_grep(args)

    def _readonly_ls(self, args: list[str]) -> list[str]:
        options: list[str] = []
        while args and args[0].startswith("-"):
            option = args.pop(0)
            if not re.fullmatch(r"-[alth]+", option):
                raise ProxyError("只读 ls 仅允许 -a、-l、-t、-h 选项")
            options.append(option)
        return ["ls", *options, *self._data_paths(args)]

    def _readonly_line_reader(self, tool: str, args: list[str], maximum_lines: int) -> list[str]:
        line_count: int | None = None
        if args and args[0] in {"-n", "--lines"}:
            if len(args) < 3:
                raise ProxyError(f"{tool} 的行数或路径缺失")
            line_count = self._tail_lines(args[1], maximum_lines)
            args = args[2:]
        elif args and args[0].startswith("-n") and len(args[0]) > 2:
            line_count = self._tail_lines(args[0][2:], maximum_lines)
            args = args[1:]
        elif args and args[0].startswith("--lines="):
            line_count = self._tail_lines(args[0].split("=", 1)[1], maximum_lines)
            args = args[1:]
        paths = self._data_paths(args)
        if len(paths) != 1:
            raise ProxyError(f"只读 {tool} 必须且只能读取一个文件")
        command = [tool]
        if line_count is not None:
            command.extend(["-n", str(line_count)])
        command.append(paths[0])
        return command

    def _readonly_grep(self, args: list[str]) -> list[str]:
        options: list[str] = []
        while args and args[0].startswith("-") and args[0] != "--":
            option = args.pop(0)
            if not re.fullmatch(r"-[inE]+", option):
                raise ProxyError("只读 grep 仅允许 -i、-n、-E 选项")
            options.append(option)
        if args and args[0] == "--":
            args.pop(0)
        if len(args) < 2:
            raise ProxyError("只读 grep 必须指定模式和至少一个 /data 文件")
        pattern = args.pop(0)
        return ["grep", *options, "--", pattern, *self._data_paths(args)]

    @staticmethod
    def _data_paths(values: list[str]) -> list[str]:
        if not values:
            raise ProxyError("只读 exec 必须指定 /data 路径")
        paths: list[str] = []
        for value in values:
            path = PurePosixPath(value)
            if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(_DATA_ROOT):
                raise ProxyError("只读 exec 只能访问容器内的 /data 路径")
            paths.append(str(path))
        return paths

    def _list_containers(
        self, config: dict[str, Any], *, all_containers: bool, name_filter: str
    ) -> dict[str, Any]:
        arguments = ["ps"]
        if all_containers:
            arguments.append("-a")
        if name_filter:
            arguments.extend(["--filter", f"name={name_filter}"])
        arguments.extend(["--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"])
        output = self._run_docker(arguments, config)
        containers = []
        for line in output.splitlines():
            name, status_text, image = (line.split("\t", 2) + ["", "", ""])[:3]
            containers.append({"name": name, "status": status_text, "image": image})
        return {"success": True, "containers": containers}

    def _inspect_container(self, target: str, config: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            "output": self._run_docker(["inspect", "--format", "{{json .State}}", target], config),
            "status": self._status_snapshot(target, config),
        }

    def _status_snapshot(self, target: str, config: dict[str, Any]) -> dict[str, Any]:
        output = self._run_docker(["inspect", "--format", _STATUS_FORMAT, target], config)
        fields = output.split("\t")
        if len(fields) != 8:
            raise ProxyError("Docker 返回了无法解析的容器状态")
        return {
            "name": target,
            "image": fields[0],
            "status": fields[1],
            "running": fields[2],
            "exit_code": fields[3],
            "started_at": fields[4],
            "finished_at": fields[5],
            "health": fields[6],
            "restart_policy": fields[7],
            "resources": self._container_resources(target, config),
            "host": _host_status(),
        }

    def _container_resources(self, target: str, config: dict[str, Any]) -> dict[str, str]:
        try:
            output = self._run_docker(
                ["stats", "--no-stream", "--format", _STATS_FORMAT, target], config
            )
        except ProxyError:
            return _unavailable_container_resources()
        fields = output.split("\t")
        if len(fields) != 6 or not fields[0]:
            return _unavailable_container_resources()
        return {
            "cpu_percent": fields[0],
            "memory_usage": fields[1],
            "memory_percent": fields[2],
            "network_io": fields[3],
            "block_io": fields[4],
            "pids": fields[5],
        }

    def _run_docker(self, arguments: list[str], config: dict[str, Any]) -> str:
        try:
            completed = subprocess.run(
                ["docker", *arguments],
                capture_output=True,
                check=False,
                text=True,
                timeout=config["timeout_seconds"],
            )
        except FileNotFoundError as exc:
            raise ProxyError("宿主机未找到 docker 命令") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProxyError("Docker 操作超时") from exc
        except OSError as exc:
            raise ProxyError(f"无法启动 Docker 命令: {exc}") from exc

        output = (completed.stdout + completed.stderr).strip()
        if completed.returncode:
            raise ProxyError(output or f"Docker 操作失败，退出码 {completed.returncode}")
        if len(output) > _MAX_OUTPUT_CHARS:
            return f"{output[:_MAX_OUTPUT_CHARS]}\n[输出已截断]"
        return output


def _unavailable_container_resources() -> dict[str, str]:
    return {
        "cpu_percent": "未上报",
        "memory_usage": "未上报",
        "memory_percent": "未上报",
        "network_io": "未上报",
        "block_io": "未上报",
        "pids": "未上报",
    }


def _host_status() -> dict[str, str]:
    memory = _meminfo()
    memory_total = memory.get("MemTotal", 0) * 1024
    memory_available = memory.get("MemAvailable", 0) * 1024
    memory_used = max(0, memory_total - memory_available)
    disk = shutil.disk_usage("/")
    try:
        load_1 = f"{os.getloadavg()[0]:.2f}"
    except OSError:
        load_1 = "未上报"
    return {
        "cpu_percent": _cpu_percent(),
        "memory_usage": _usage_text(memory_used, memory_total),
        "disk_usage": _usage_text(disk.used, disk.total),
        "load_1": load_1,
        "uptime": _host_uptime(),
    }


def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
    except (OSError, ValueError, IndexError):
        return {}
    return values


def _cpu_percent() -> str:
    first = _cpu_times()
    if first is None:
        return "未上报"
    time.sleep(0.15)
    second = _cpu_times()
    if second is None:
        return "未上报"
    total = second[0] - first[0]
    idle = second[1] - first[1]
    if total <= 0:
        return "未上报"
    return f"{(total - idle) / total * 100:.1f}%"


def _cpu_times() -> tuple[int, int] | None:
    try:
        values = [
            int(value)
            for value in Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
        ]
    except (OSError, ValueError, IndexError):
        return None
    if len(values) < 5:
        return None
    return sum(values), values[3] + values[4]


def _host_uptime() -> str:
    try:
        seconds = int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))
    except (OSError, ValueError, IndexError):
        return "未上报"
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes = seconds // 60
    if days:
        return f"{days}天 {hours}小时"
    if hours:
        return f"{hours}小时 {minutes}分"
    return f"{minutes}分"


def _usage_text(used: int, total: int) -> str:
    if total <= 0:
        return "未上报"
    return f"{_format_bytes(used)} / {_format_bytes(total)} ({used / total * 100:.1f}%)"


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} {unit}"
        amount /= 1024
    return "0 B"


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
        if len(raw) > _MAX_REQUEST_BYTES:
            response = {"success": False, "error": "请求过长"}
        else:
            try:
                response = self.server.proxy.handle(json.loads(raw.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = {"success": False, "error": "请求不是有效 JSON"}
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


_UnixStreamServer = getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)


class _UnixServer(socketserver.ThreadingMixIn, _UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def _prepare_socket(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise RuntimeError(f"拒绝覆盖非 Socket 文件: {path}")
        path.unlink()


def serve(*, socket_path: Path, socket_gid: int, proxy: ContainerAdminProxy) -> None:
    if not hasattr(socket, "AF_UNIX"):
        raise RuntimeError("当前平台不支持 Unix Socket")
    _prepare_socket(socket_path)
    with _UnixServer(str(socket_path), _RequestHandler) as server:
        server.proxy = proxy
        os.chown(socket_path, -1, socket_gid)
        os.chmod(socket_path, 0o660)
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
        signal.signal(signal.SIGINT, lambda *_: threading.Thread(target=server.shutdown).start())
        server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--socket", type=Path, default=Path("/run/sirius-container-admin.sock"))
    parser.add_argument("--socket-gid", type=int, default=os.getgid())
    args = parser.parse_args()
    serve(
        socket_path=args.socket,
        socket_gid=args.socket_gid,
        proxy=ContainerAdminProxy(args.config),
    )


if __name__ == "__main__":
    main()
