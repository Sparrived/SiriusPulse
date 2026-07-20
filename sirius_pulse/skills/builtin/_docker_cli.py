"""Restricted Docker CLI bridge used by the built-in Bash skill."""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import sys
from typing import Any, Sequence

_SOCKET_PATH = "/run/sirius-container-admin.sock"
_REQUEST_TIMEOUT = 15.0
_MAX_RESPONSE_BYTES = 50_000
_MAX_LOG_LINES = 200
_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ALLOWED_COMMANDS = "ps、inspect、logs、start、stop、restart"
INSPECT_STATUS_MARKER = "__SIRIUS_DOCKER_INSPECT_STATUS__:"


class DockerCommandError(ValueError):
    """Raised when the emulated Docker CLI command is not allowed."""


def build_request(arguments: Sequence[str]) -> dict[str, Any]:
    """Convert a small safe subset of Docker CLI syntax into a proxy request."""
    args = [str(arg) for arg in arguments]
    if not args:
        raise DockerCommandError(f"未指定 Docker 子命令；只允许 {_ALLOWED_COMMANDS}")

    command = args.pop(0)
    if command == "container":
        if not args:
            raise DockerCommandError(f"未指定 docker container 子命令；只允许 {_ALLOWED_COMMANDS}")
        command = args.pop(0)
        if command == "ls":
            command = "ps"

    if command == "ps":
        return _parse_ps(args)
    if command in {"inspect", "start", "stop", "restart"}:
        return _parse_single_container(command, args)
    if command == "logs":
        return _parse_logs(args)

    raise DockerCommandError(
        f"不允许 Docker 操作: {command}。容器删除、清理、重建、镜像、卷、网络和 exec 操作均被拒绝；"
        f"只允许 {_ALLOWED_COMMANDS}。"
    )


def request_host_proxy(request: dict[str, Any]) -> dict[str, Any]:
    """Submit one fixed request to the host-side restricted Docker proxy."""
    socket_path = os.environ.get("SIRIUS_CONTAINER_ADMIN_SOCKET", _SOCKET_PATH)
    encoded = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(_REQUEST_TIMEOUT)
        client.connect(socket_path)
        client.sendall(encoded)
        response = bytearray()
        while len(response) <= _MAX_RESPONSE_BYTES:
            chunk = client.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            if b"\n" in chunk:
                break

    if len(response) > _MAX_RESPONSE_BYTES:
        raise DockerCommandError("Docker 代理响应过长")
    try:
        decoded = json.loads(response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DockerCommandError(f"Docker 代理返回了无效响应: {exc}") from exc
    if not isinstance(decoded, dict):
        raise DockerCommandError("Docker 代理返回了无效响应")
    return decoded


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the bridge with Docker-like stdout, stderr, and exit codes."""
    try:
        request = build_request(arguments if arguments is not None else sys.argv[1:])
        response = request_host_proxy(request)
    except (DockerCommandError, OSError, TimeoutError) as exc:
        print(f"docker: {exc}", file=sys.stderr)
        return 2

    if not response.get("success"):
        print(f"docker: {response.get('error') or 'Docker 操作失败'}", file=sys.stderr)
        return 1

    output = str(response.get("output") or "").strip()
    if not output and isinstance(response.get("containers"), list):
        output = "\n".join(
            f"{item.get('name', '')}\t{item.get('status', '')}\t{item.get('image', '')}"
            for item in response["containers"]
            if isinstance(item, dict)
        )
    if output:
        print(output)
    status = response.get("status")
    if request.get("action") == "inspect" and isinstance(status, dict):
        print(format_inspect_status_marker(status), file=sys.stderr)
    return 0


def format_inspect_status_marker(status: dict[str, Any]) -> str:
    """Encode inspect status for the Bash skill without exposing it to shell output."""
    payload = json.dumps(status, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return INSPECT_STATUS_MARKER + base64.b64encode(payload).decode("ascii")


def _parse_ps(args: list[str]) -> dict[str, Any]:
    all_containers = False
    for arg in args:
        if arg in {"-a", "--all"}:
            all_containers = True
            continue
        raise DockerCommandError(f"docker ps 不支持参数: {arg}；只允许 -a 或 --all")
    return {"action": "list", "container": "", "tail_lines": 100, "all": all_containers}


def _parse_single_container(command: str, args: list[str]) -> dict[str, Any]:
    if len(args) != 1:
        raise DockerCommandError(f"docker {command} 必须且只能指定一个容器名称")
    return {
        "action": command,
        "container": _validated_container(args[0]),
        "tail_lines": 100,
    }


def _parse_logs(args: list[str]) -> dict[str, Any]:
    tail_lines = 100
    container = ""
    position = 0
    while position < len(args):
        arg = args[position]
        if arg in {"--tail", "-n"}:
            position += 1
            if position >= len(args):
                raise DockerCommandError(f"docker logs 的 {arg} 缺少行数")
            tail_lines = _validated_tail(args[position])
        elif arg.startswith("--tail="):
            tail_lines = _validated_tail(arg.split("=", 1)[1])
        elif arg.startswith("-n") and len(arg) > 2:
            tail_lines = _validated_tail(arg[2:])
        elif arg.startswith("-"):
            raise DockerCommandError(f"docker logs 不支持参数: {arg}；只允许 --tail 或 -n")
        elif container:
            raise DockerCommandError("docker logs 必须且只能指定一个容器名称")
        else:
            container = _validated_container(arg)
        position += 1

    if not container:
        raise DockerCommandError("docker logs 必须指定容器名称")
    return {"action": "logs", "container": container, "tail_lines": tail_lines}


def _validated_container(value: str) -> str:
    container = str(value or "").strip()
    if not _CONTAINER_NAME.fullmatch(container):
        raise DockerCommandError("容器名称无效")
    return container


def _validated_tail(value: str) -> int:
    try:
        lines = int(value)
    except ValueError as exc:
        raise DockerCommandError("日志行数必须是整数") from exc
    if not 1 <= lines <= _MAX_LOG_LINES:
        raise DockerCommandError(f"日志行数必须在 1 到 {_MAX_LOG_LINES} 之间")
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
