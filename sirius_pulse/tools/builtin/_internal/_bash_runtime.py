"""Runtime helpers for the built-in Bash tool."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from sirius_pulse.tools.builtin._internal import _docker_cli
from sirius_pulse.tools.models import ToolInvocationContext

_SENSITIVE_ENV = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION)", re.IGNORECASE
)
_RUNTIME_DIR_NAME = "runtime"
_RUNTIME_BIN_DIR = "bin"
_RUNTIME_PYTHON_DIR = "python"
_RUNTIME_NODE_DIR = "node"
_RUNTIME_CACHE_DIR = "cache"
# Her own place, beside the runtime, inside the mounted persona directory.  It
# must not be ``/home/sirius``: that lives in the image layer and every rebuild
# of the image takes it with it.
_HOME_DIR_NAME = "home"
_DOCKER_FUNCTION_TEMPLATE = """docker() {{
    {python_executable} -m sirius_pulse.tools.builtin._internal._docker_cli \"$@\"
}}
docker-compose() {{
    {python_executable} -m sirius_pulse.tools.builtin._internal._docker_cli compose \"$@\"
}}
"""


def load_policy(
    data_store: Any,
    *,
    default_timeout: float,
    default_output: int,
    minimum_output: int,
) -> dict[str, Any]:
    reload_store = getattr(data_store, "reload", None)
    if callable(reload_store):
        reload_store()

    max_timeout = bounded_number(
        data_store.get("max_timeout_seconds", default_timeout) if data_store else default_timeout,
        default=default_timeout,
        minimum=1.0,
        maximum=60.0,
    )
    max_output = int(
        bounded_number(
            data_store.get("max_output_chars", default_output) if data_store else default_output,
            default=default_output,
            minimum=minimum_output,
            maximum=50_000,
        )
    )
    return {"max_timeout_seconds": max_timeout, "max_output_chars": max_output}


def validate_command(command: str, *, max_length: int) -> str:
    text = str(command or "").strip()
    if not text:
        raise ValueError("command 不能为空")
    if len(text) > max_length:
        raise ValueError(f"command 过长，最多 {max_length} 个字符")
    if "\0" in text:
        raise ValueError("command 不能包含空字节")
    return text


def resolve_cwd(cwd: str, *, home: Path | None = None) -> Path:
    """Resolve the working directory, falling back to the persona's own home.

    ``"."`` means "wherever she normally works", which is her persistent home
    rather than the process directory: the process cwd is ``/app``, inside the
    image layer, and a rebuild would take everything written there with it.

    ``~`` is expanded against the *persona* home for the same reason — the
    process home is ephemeral, and a ``~`` that means one thing to the Python
    side and another to the bash side is how a file gets written to a place
    nobody looks at again.
    """
    requested = str(cwd or "").strip() or "."
    resolved = expand_persona_path(requested, home=home)
    if not resolved.is_dir():
        raise ValueError(f"cwd 不是目录: {cwd}")
    return resolved


def expand_persona_path(value: str, *, home: Path | None = None) -> Path:
    """Expand ``~`` against the persona home, then resolve to an absolute path.

    Without a workspace (``home is None``) this is plain ``expanduser``, so
    callers that have no persona context keep their previous behaviour.
    """
    text = str(value or "").strip()
    if home is None:
        return Path(text or ".").expanduser().resolve()
    if text in {"", ".", "~", "~/"}:
        return home
    if text.startswith("~/"):
        return (home / text[2:]).resolve()
    return Path(text).expanduser().resolve()


def find_bash() -> str | None:
    configured = os.environ.get("SIRIUS_BASH_PATH", "").strip()
    return configured or shutil.which("bash")


def docker_function() -> str:
    """Build the Docker shell function with the active Sirius interpreter."""
    return _DOCKER_FUNCTION_TEMPLATE.format(python_executable=shlex.quote(sys.executable))


async def send_inspect_status_cards(
    statuses: list[dict[str, Any]],
    *,
    data_store: Any,
    chat_context: dict[str, Any] | None,
    engine_context: Any,
    invocation_context: ToolInvocationContext | None,
    status_card: Any,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for raw_status in statuses:
        status = status_card.normalize_status(raw_status)
        record: dict[str, Any] = {
            "status": status,
            "sent": False,
            "error": "",
            "message_id": None,
        }
        cards.append(record)
        if status is None:
            record["error"] = "Docker 代理未返回有效的容器状态"
            continue
        if not chat_target(chat_context)[1]:
            record["error"] = "当前调用没有 QQ 会话上下文"
            continue
        registry = getattr(engine_context, "tool_registry", None)
        executor = getattr(engine_context, "tool_executor", None)
        if registry is None or executor is None:
            record["error"] = "Tool 运行上下文未就绪，无法发送状态卡片"
            continue
        group_file_exec = registry.get("group_file_exec")
        if group_file_exec is None:
            record["error"] = "未找到 group_file_exec Tool，无法发送状态卡片"
            continue
        try:
            image_path = await status_card.render_status_card(status, data_store)
            record["card_path"] = str(image_path)
            sent = await executor.execute_async(
                group_file_exec,
                {"action": "image", "image_path": str(image_path)},
                invocation_context=invocation_context,
            )
            if sent.success:
                record["sent"] = True
                record["message_id"] = sent.internal_metadata.get("message_id")
            else:
                record["error"] = sent.error or "状态卡片发送失败"
        except Exception as exc:
            record["error"] = str(exc)
    return cards


def container_recovery_hint(command: str) -> str:
    text = command.lower()
    if not any(token in text for token in ("docker", "minecraft", "systemctl")):
        return ""
    return (
        "\n容器排障请继续使用受支持命令，不要在 Sirius 容器中使用 systemctl 或宿主机 /var/log："
        "\n1. docker ps -a"
        "\n2. docker inspect <容器名称>"
        "\n3. docker logs --tail 200 <容器名称>"
        "\n4. docker exec <容器名称> tail -n 200 /data/logs/latest.log"
        "\n5. docker exec <容器名称> ls -lt /data/crash-reports"
    )


def safe_environment() -> dict[str, str]:
    keep = {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SIRIUS_CONTAINER_ADMIN_SOCKET",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USER",
        "USERPROFILE",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in keep and not _SENSITIVE_ENV.search(key)
    }


def runtime_environment(data_store: Any) -> dict[str, str]:
    environment = safe_environment()
    runtime_root = runtime_root_path(data_store)
    if runtime_root is None:
        return environment

    runtime_bin = runtime_root / _RUNTIME_BIN_DIR
    runtime_python = runtime_root / _RUNTIME_PYTHON_DIR
    runtime_node = runtime_root / _RUNTIME_NODE_DIR
    runtime_cache = runtime_root / _RUNTIME_CACHE_DIR
    home = home_root_path(data_store)
    for directory in (
        runtime_bin,
        runtime_python,
        runtime_node,
        runtime_node / "bin",
        runtime_cache,
        runtime_cache / "pip",
        runtime_cache / "npm",
        runtime_cache / "go",
        *([home] if home is not None else []),
    ):
        directory.mkdir(parents=True, exist_ok=True)

    environment["SIRIUS_RUNTIME_ROOT"] = str(runtime_root)
    environment["SIRIUS_RUNTIME_BIN"] = str(runtime_bin)
    environment["PIP_TARGET"] = str(runtime_python)
    environment["PIP_CACHE_DIR"] = str(runtime_cache / "pip")
    environment["NPM_CONFIG_PREFIX"] = str(runtime_node)
    environment["NPM_CONFIG_CACHE"] = str(runtime_cache / "npm")
    environment["GOBIN"] = str(runtime_bin)
    environment["GOMODCACHE"] = str(runtime_cache / "go")
    environment["PATH"] = os.pathsep.join(
        [str(runtime_bin), str(runtime_node / "bin"), environment.get("PATH", "")]
    )
    environment["PYTHONPATH"] = prepend_environment_path(
        str(runtime_python), environment.get("PYTHONPATH", "")
    )
    if home is not None:
        # ``HOME`` also survives a rebuild only if it points into the mount.
        # ``bash -lc`` keeps it, and ``~`` then expands to a directory that is
        # still there after the next deploy.
        environment["HOME"] = str(home)
        environment["SIRIUS_HOME"] = str(home)
    return environment


def home_root_path(data_store: Any) -> Path | None:
    """The persona's persistent home, or ``None`` when there is no workspace."""
    runtime_root = runtime_root_path(data_store)
    return None if runtime_root is None else runtime_root.parent / _HOME_DIR_NAME


def runtime_exports(environment: dict[str, str]) -> str:
    """Re-export the runtime environment inside the command prelude.

    ``bash -lc`` reads ``/etc/profile``, which unconditionally resets ``PATH``
    (and would drop the runtime directories we put in front of it).  Exporting
    again *after* profile processing is the only place that survives, because
    the prelude is prepended to the command text and therefore runs last.
    """
    names = ("HOME", "PATH", "PYTHONPATH", "PIP_TARGET", "NPM_CONFIG_PREFIX", "GOBIN")
    lines = [
        f"export {name}={shlex.quote(environment[name])}" for name in names if environment.get(name)
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def runtime_root_path(data_store: Any) -> Path | None:
    store_path = getattr(data_store, "store_path", None)
    if not store_path:
        return None
    path = Path(store_path).expanduser().resolve()
    if path.parent.name != "tool_data":
        return None
    return path.parent.parent / _RUNTIME_DIR_NAME


def prepend_environment_path(value: str, existing: str) -> str:
    return os.pathsep.join(item for item in (value, existing) if item)


def bounded_number(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def decode_output(raw: bytes, limit: int) -> str:
    return truncate_text(raw.decode("utf-8", errors="replace"), limit)[0]


def decode_output_with_inspect_status(
    raw: bytes, limit: int
) -> tuple[str, list[dict[str, Any]], bool]:
    statuses: list[dict[str, Any]] = []
    kept_lines: list[str] = []
    for line in raw.decode("utf-8", errors="replace").splitlines(keepends=True):
        if not line.startswith(_docker_cli.INSPECT_STATUS_MARKER):
            kept_lines.append(line)
            continue
        encoded = line[len(_docker_cli.INSPECT_STATUS_MARKER) :].strip()
        try:
            value = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
        except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            kept_lines.append(line)
            continue
        if isinstance(value, dict):
            statuses.append(value)
    output, truncated = truncate_text("".join(kept_lines), limit)
    return output, statuses, truncated


def chat_target(chat_context: dict[str, Any] | None) -> tuple[str, str]:
    context = chat_context or {}
    chat_type = str(context.get("chat_type") or "").strip()
    if chat_type not in {"group", "private"}:
        return "", ""
    return chat_type, str(context.get("chat_id") or context.get("group_id") or "").strip()


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) > limit:
        return f"{text[:limit]}\n[输出已截断]", True
    return text, False
