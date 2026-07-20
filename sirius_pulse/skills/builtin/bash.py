"""人格可调用的 Bash 工具。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.skills.builtin import _container_status_card, _docker_cli
from sirius_pulse.skills.models import SkillInvocationContext

_SENSITIVE_ENV = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION)", re.IGNORECASE
)
_DEFAULT_MAX_TIMEOUT = 15.0
_DEFAULT_MAX_OUTPUT = 12_000
_MAX_COMMAND_LENGTH = 4_000
_MIN_OUTPUT = 256
_DOCKER_FUNCTION_TEMPLATE = """docker() {{
    {python_executable} -m sirius_pulse.skills.builtin._docker_cli \"$@\"
}}
docker-compose() {{
    docker compose \"$@\"
}}
"""

_config = ConfigBuilder()
_config.group("Bash 执行").add(
    "command",
    type="str",
    description="要执行的 Bash 命令，支持管道、重定向、here-document、变量和命令替换。",
    required=True,
)
_config.group("Bash 执行").add(
    "cwd",
    type="str",
    description="容器内工作目录；可使用绝对路径，默认为当前进程目录。",
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
        "在容器中启动 Bash，用于文件处理、系统状态查询和自动化。"
        "支持标准 Bash 语法与容器内任意工作目录，也支持受控的原生 Docker 命令："
        "docker ps、inspect、logs、start、stop、restart。Docker 删除、清理、重建及镜像、卷、网络、exec 操作会被拒绝；"
        "docker inspect 会在当前 QQ 会话发送容器状态卡片；"
        "每个人格可在技能配置中调整执行时限和输出上限。"
    ),
    "version": "1.2.0",
    "side_effect": "unknown",
    "tags": ["bash", "shell", "file", "system", "container"],
    "parameters": _config.build(),
    "config": {
        "max_timeout_seconds": {
            "type": "number",
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


async def run(
    command: str,
    cwd: str = ".",
    timeout_seconds: float = 10.0,
    max_output_chars: int = 8_000,
    data_store: Any = None,
    chat_context: dict[str, Any] | None = None,
    engine_context: Any = None,
    invocation_context: SkillInvocationContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Execute one Bash command with a restricted native Docker function."""
    if data_store is not None and data_store.get("_enabled", True) is False:
        return {"success": False, "error": "bash Skill 已被当前人格禁用"}

    policy = _load_policy(data_store)
    try:
        command_text = _validate_command(command)
        cwd_path = _resolve_cwd(cwd)
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
        completed = await asyncio.to_thread(
            subprocess.run,
            [bash, "-o", "pipefail", "-lc", f"{_docker_function()}\n{command_text}"],
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

    output, inspect_statuses, truncated = _decode_output_with_inspect_status(
        completed.stdout + completed.stderr, output_limit
    )
    metadata = {
        "cwd": str(cwd_path),
        "returncode": completed.returncode,
        "command_length": len(command_text),
        "docker_bridge_enabled": True,
        "truncated": truncated,
    }
    if completed.returncode != 0:
        detail = f"命令退出码 {completed.returncode}"
        if output:
            detail += f"\n{output}"
        return {"success": False, "error": detail, "internal_metadata": metadata}

    cards = await _send_inspect_status_cards(
        inspect_statuses,
        data_store=data_store,
        chat_context=chat_context,
        engine_context=engine_context,
        invocation_context=invocation_context,
    )
    metadata["inspect_statuses"] = [item["status"] for item in cards if item.get("status")]
    metadata["status_cards"] = cards
    sent_count = sum(1 for item in cards if item["sent"])
    card_errors = [str(item["error"]) for item in cards if item["error"]]
    text_parts = [output or "命令执行成功，但没有输出。"]
    text_parts.extend(_container_status_card.status_summary(item["status"]) for item in cards if item.get("status"))
    if card_errors:
        text_parts.append("状态卡片未发送：" + "；".join(card_errors))
    summary = f"Bash 执行完成（退出码 0，工作目录 {cwd_path}）"
    if sent_count:
        summary += f"；已发送 {sent_count} 张容器状态卡片"

    return {
        "success": True,
        "summary": summary,
        "text_blocks": ["\n".join(text_parts)],
        "internal_metadata": metadata,
    }


def _load_policy(data_store: Any) -> dict[str, Any]:
    reload_store = getattr(data_store, "reload", None)
    if callable(reload_store):
        reload_store()

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
        "max_timeout_seconds": max_timeout,
        "max_output_chars": max_output,
    }


def _validate_command(command: str) -> str:
    text = str(command or "").strip()
    if not text:
        raise ValueError("command 不能为空")
    if len(text) > _MAX_COMMAND_LENGTH:
        raise ValueError(f"command 过长，最多 {_MAX_COMMAND_LENGTH} 个字符")
    if "\0" in text:
        raise ValueError("command 不能包含空字节")
    return text


def _resolve_cwd(cwd: str) -> Path:
    requested = str(cwd or ".").strip() or "."
    resolved = Path(requested).expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"cwd 不是目录: {cwd}")
    return resolved


def _find_bash() -> str | None:
    configured = os.environ.get("SIRIUS_BASH_PATH", "").strip()
    return configured or shutil.which("bash")


def _docker_function() -> str:
    """Build the Docker shell function with the active Sirius interpreter."""
    return _DOCKER_FUNCTION_TEMPLATE.format(python_executable=shlex.quote(sys.executable))


async def _send_inspect_status_cards(
    statuses: list[dict[str, Any]],
    *,
    data_store: Any,
    chat_context: dict[str, Any] | None,
    engine_context: Any,
    invocation_context: SkillInvocationContext | None,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for raw_status in statuses:
        status = _container_status_card.normalize_status(raw_status)
        record: dict[str, Any] = {"status": status, "sent": False, "error": "", "message_id": None}
        cards.append(record)
        if status is None:
            record["error"] = "Docker 代理未返回有效的容器状态"
            continue
        if not _chat_target(chat_context)[1]:
            record["error"] = "当前调用没有 QQ 会话上下文"
            continue
        registry = getattr(engine_context, "skill_registry", None)
        executor = getattr(engine_context, "skill_executor", None)
        if registry is None or executor is None:
            record["error"] = "Skill 运行上下文未就绪，无法发送状态卡片"
            continue
        file_upload = registry.get("file_upload")
        if file_upload is None:
            record["error"] = "未找到 file_upload Skill，无法发送状态卡片"
            continue
        try:
            image_path = await _container_status_card.render_status_card(status, data_store)
            record["card_path"] = str(image_path)
            sent = await executor.execute_async(
                file_upload,
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


def _chat_target(chat_context: dict[str, Any] | None) -> tuple[str, str]:
    context = chat_context or {}
    chat_type = str(context.get("chat_type") or "").strip()
    if chat_type not in {"group", "private"}:
        return "", ""
    return chat_type, str(context.get("chat_id") or context.get("group_id") or "").strip()


def _safe_environment() -> dict[str, str]:
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


def _bounded_number(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _decode_output(raw: bytes, limit: int) -> str:
    return _truncate_text(raw.decode("utf-8", errors="replace"), limit)[0]


def _decode_output_with_inspect_status(raw: bytes, limit: int) -> tuple[str, list[dict[str, Any]], bool]:
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
    output, truncated = _truncate_text("".join(kept_lines), limit)
    return output, statuses, truncated


def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) > limit:
        return f"{text[:limit]}\n[输出已截断]", True
    return text, False
