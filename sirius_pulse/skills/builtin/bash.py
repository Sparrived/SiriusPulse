"""人格可调用的受控 Bash 工具。"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder

_READ_ONLY_COMMANDS = {
    "cat",
    "cut",
    "date",
    "df",
    "du",
    "echo",
    "find",
    "false",
    "free",
    "grep",
    "head",
    "hostname",
    "id",
    "ls",
    "ps",
    "printf",
    "pwd",
    "sed",
    "sort",
    "tail",
    "tr",
    "true",
    "uname",
    "uniq",
    "uptime",
    "wc",
    "whoami",
}
_WRITE_COMMANDS = {"cp", "mkdir", "mv", "tee", "touch"}
_DESTRUCTIVE_COMMANDS = {"rm", "rmdir"}
_ALWAYS_BLOCKED_COMMANDS = {
    "bash",
    "cmd",
    "command",
    "eval",
    "node",
    "perl",
    "powershell",
    "pwsh",
    "python",
    "python3",
    "sh",
    "sudo",
    "xargs",
}
_DANGEROUS_FIND_FLAGS = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
_DANGEROUS_GIT_COMMANDS = {
    "apply",
    "checkout",
    "clean",
    "clone",
    "commit",
    "config",
    "fetch",
    "merge",
    "pull",
    "push",
    "rebase",
    "reset",
    "submodule",
    "worktree",
}
_FORBIDDEN_SYNTAX = re.compile(r"(?:[;&<>`$]|\r|\n|&&|\|\|)")
_PARENT_PATH = re.compile(r"(?:^|[/\\])\.\.(?:[/\\]|$)")
_ABSOLUTE_PATH = re.compile(r"^(?:/|\\|[A-Za-z]:[/\\])")
_ABSOLUTE_OPTION_VALUE = re.compile(r"=(?:/|\\|[A-Za-z]:[/\\])")
_SENSITIVE_ENV = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION)", re.IGNORECASE
)
_DEFAULT_MAX_TIMEOUT = 15.0
_DEFAULT_MAX_OUTPUT = 12_000
_MAX_COMMAND_LENGTH = 4_000
_MIN_OUTPUT = 256

_config = ConfigBuilder()
_config.group("Bash 执行").add(
    "command",
    type="str",
    description=(
        "要执行的受控 Bash 命令。只允许配置中的命令和简单管道；禁止命令串联、重定向、"
        "子命令替换和解释器嵌套。路径必须使用人格工作区内的相对路径。"
    ),
    required=True,
)
_config.group("Bash 执行").add(
    "cwd",
    type="str",
    description="工作目录，相对于当前人格工作区，默认为工作区根目录。",
    default=".",
)
_config.group("Bash 执行").add(
    "timeout_seconds",
    type="float",
    description="本次命令的超时时间；实际值不会超过人格配置的上限。",
    default=10.0,
)
_config.group("Bash 执行").add(
    "max_output_chars",
    type="int",
    description="最多返回多少字符；实际值不会超过人格配置的上限。",
    default=8_000,
)

SKILL_META = {
    "name": "bash",
    "description": (
        "在当前人格工作区内执行受控 Bash 命令，用于替代分散的文件读取、目录检索和系统状态查询。"
        "支持根据上一步结果连续串行调用；默认只允许只读命令，写入或删除必须由人格 Skill 配置显式开启。"
    ),
    "version": "1.0.0",
    "side_effect": "unknown",
    "tags": ["bash", "shell", "file", "system", "workspace"],
    "parameters": _config.build(),
    "config": {
        "allowed_commands": {
            "type": "list[str]",
            "description": "允许执行的命令名白名单；解释器和嵌套 shell 无论如何都不会放行。",
            "default": sorted(_READ_ONLY_COMMANDS),
            "group": "权限",
        },
        "allow_write_commands": {
            "type": "bool",
            "description": "是否额外允许 cp/mkdir/mv/tee/touch，路径仍限制在人格工作区。",
            "default": False,
            "group": "权限",
        },
        "allow_destructive_commands": {
            "type": "bool",
            "description": "是否允许 rm/rmdir；默认关闭。",
            "default": False,
            "group": "权限",
        },
        "max_timeout_seconds": {
            "type": "float",
            "description": "单次 Bash 的最大执行时间，范围 1 到 60 秒。",
            "default": _DEFAULT_MAX_TIMEOUT,
            "group": "限制",
        },
        "max_output_chars": {
            "type": "int",
            "description": "单次 Bash 返回的最大字符数，范围 256 到 50000。",
            "default": _DEFAULT_MAX_OUTPUT,
            "group": "限制",
        },
    },
}


def run(
    command: str,
    cwd: str = ".",
    timeout_seconds: float = 10.0,
    max_output_chars: int = 8_000,
    data_store: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Execute one validated command or a validated pipeline."""
    if data_store is not None and data_store.get("_enabled", True) is False:
        return {"success": False, "error": "bash Skill 已被当前人格禁用"}

    policy = _load_policy(data_store)
    try:
        command_text, segments = _validate_command(command, policy["allowed_commands"])
        root = _workspace_root(data_store)
        cwd_path = _resolve_cwd(root, cwd)
        timeout = _bounded_number(
            timeout_seconds, default=10.0, minimum=0.1, maximum=policy["max_timeout_seconds"]
        )
        output_limit = int(
            _bounded_number(
                max_output_chars,
                default=8_000,
                minimum=_MIN_OUTPUT,
                maximum=policy["max_output_chars"],
            )
        )
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    bash = _find_bash()
    if not bash:
        return {
            "success": False,
            "error": "系统未找到 Bash；请安装 Bash 或设置 SIRIUS_BASH_PATH。",
        }

    try:
        completed = subprocess.run(
            [bash, "-o", "pipefail", "-lc", command_text],
            cwd=str(cwd_path),
            env=_safe_environment(),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = _decode_output((exc.stdout or b"") + (exc.stderr or b""), output_limit)
        detail = f"命令执行超时（上限 {timeout:g} 秒）"
        if partial:
            detail += f"\n部分输出:\n{partial}"
        return {"success": False, "error": detail}
    except OSError as exc:
        return {"success": False, "error": f"启动 Bash 失败: {exc}"}

    output = _decode_output(completed.stdout + completed.stderr, output_limit)
    metadata = {
        "cwd": str(cwd_path),
        "returncode": completed.returncode,
        "commands": [segment[0] for segment in segments],
        "truncated": len(completed.stdout + completed.stderr) > output_limit,
    }
    if completed.returncode != 0:
        detail = f"命令退出码 {completed.returncode}"
        if output:
            detail += f"\n{output}"
        return {"success": False, "error": detail, "internal_metadata": metadata}

    return {
        "success": True,
        "summary": f"Bash 执行完成（退出码 0，工作目录 {cwd_path}）",
        "text_blocks": [output or "命令执行成功，但没有输出。"],
        "internal_metadata": metadata,
    }


def _load_policy(data_store: Any) -> dict[str, Any]:
    allowed = (
        data_store.get("allowed_commands", sorted(_READ_ONLY_COMMANDS)) if data_store else None
    )
    if not isinstance(allowed, list):
        allowed = sorted(_READ_ONLY_COMMANDS)
    commands = {str(item).strip().lower() for item in allowed if str(item).strip()}
    if data_store and data_store.get("allow_write_commands", False):
        commands.update(_WRITE_COMMANDS)
    if data_store and data_store.get("allow_destructive_commands", False):
        commands.update(_DESTRUCTIVE_COMMANDS)

    max_timeout = _bounded_number(
        (
            data_store.get("max_timeout_seconds", _DEFAULT_MAX_TIMEOUT)
            if data_store
            else _DEFAULT_MAX_TIMEOUT
        ),
        default=_DEFAULT_MAX_TIMEOUT,
        minimum=1.0,
        maximum=60.0,
    )
    max_output = int(
        _bounded_number(
            (
                data_store.get("max_output_chars", _DEFAULT_MAX_OUTPUT)
                if data_store
                else _DEFAULT_MAX_OUTPUT
            ),
            default=_DEFAULT_MAX_OUTPUT,
            minimum=_MIN_OUTPUT,
            maximum=50_000,
        )
    )
    return {
        "allowed_commands": commands,
        "max_timeout_seconds": max_timeout,
        "max_output_chars": max_output,
    }


def _validate_command(command: str, allowed_commands: set[str]) -> tuple[str, list[list[str]]]:
    text = str(command or "").strip()
    if not text:
        raise ValueError("command 不能为空")
    if len(text) > _MAX_COMMAND_LENGTH:
        raise ValueError(f"command 过长，最多 {_MAX_COMMAND_LENGTH} 个字符")
    if _FORBIDDEN_SYNTAX.search(text):
        raise ValueError("只允许简单命令和管道，禁止命令串联、重定向、变量/子命令替换和后台运行")

    segments: list[list[str]] = []
    for raw_segment in text.split("|"):
        try:
            tokens = shlex.split(raw_segment, posix=True)
        except ValueError as exc:
            raise ValueError(f"command 解析失败: {exc}") from exc
        if not tokens:
            raise ValueError("管道两侧都必须有命令")
        executable = Path(tokens[0]).name.lower()
        if "/" in tokens[0] or "\\" in tokens[0]:
            raise ValueError("命令必须使用白名单中的命令名，不能指定可执行文件路径")
        if executable in _ALWAYS_BLOCKED_COMMANDS:
            raise ValueError(f"禁止执行嵌套 shell 或解释器: {executable}")
        if executable not in allowed_commands:
            raise ValueError(f"命令不在允许列表中: {executable}")
        _validate_arguments(executable, tokens[1:])
        segments.append(tokens)
    return text, segments


def _validate_arguments(executable: str, arguments: list[str]) -> None:
    if executable == "find" and _DANGEROUS_FIND_FLAGS.intersection(arguments):
        raise ValueError("find 的执行/删除参数被禁止")
    if executable == "git":
        subcommand = next((item.lower() for item in arguments if not item.startswith("-")), "")
        if subcommand in _DANGEROUS_GIT_COMMANDS:
            raise ValueError(f"git {subcommand} 会修改外部状态，已禁止")
    if executable == "sed" and any(arg == "-i" or arg.startswith("-i") for arg in arguments):
        raise ValueError("sed 原地写入被禁止，请在 Bash 配置中使用受控写入命令")
    for argument in arguments:
        if _ABSOLUTE_PATH.match(argument) or _PARENT_PATH.search(argument):
            raise ValueError("命令参数只能使用人格工作区内的相对路径")
        if _ABSOLUTE_OPTION_VALUE.search(argument) or argument == "~" or argument.startswith("~/"):
            raise ValueError("命令参数不能引用工作区外的绝对路径")


def _workspace_root(data_store: Any) -> Path:
    store_path = getattr(data_store, "store_path", None)
    if store_path:
        return Path(store_path).resolve().parent.parent
    return Path.cwd().resolve()


def _resolve_cwd(root: Path, cwd: str) -> Path:
    requested = str(cwd or ".").strip() or "."
    if _ABSOLUTE_PATH.match(requested) or _PARENT_PATH.search(requested):
        raise ValueError("cwd 只能是人格工作区内的相对路径")
    resolved = (root / requested).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("cwd 超出人格工作区范围") from exc
    if not resolved.is_dir():
        raise ValueError(f"cwd 不是目录: {cwd}")
    return resolved


def _find_bash() -> str | None:
    configured = os.environ.get("SIRIUS_BASH_PATH", "").strip()
    return configured or shutil.which("bash")


def _safe_environment() -> dict[str, str]:
    keep = {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
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


def _bounded_number(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _decode_output(raw: bytes, limit: int) -> str:
    text = raw.decode("utf-8", errors="replace")
    if len(text) > limit:
        return f"{text[:limit]}\n[输出已截断]"
    return text
