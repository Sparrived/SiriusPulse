"""人格可调用的 Bash 工具。"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.tools import cron_tasks
from sirius_pulse.tools.builtin._internal import (
    _bash_cron,
    _bash_runtime,
    _container_status_card,
)
from sirius_pulse.tools.models import ToolInvocationContext

_DEFAULT_MAX_TIMEOUT = 15.0
_DEFAULT_MAX_OUTPUT = 12_000
_MAX_COMMAND_LENGTH = 4_000
_MIN_OUTPUT = 256

_config = ConfigBuilder()
_config.group("Bash 执行").add(
    "command",
    type="str",
    description=(
        "要执行的 Bash 命令，支持管道、重定向、here-document、变量和命令替换。"
        "也支持项目级 crontab：crontab -l 查看、crontab -r 删除，"
        "以及 printf '%s\\n' '分 时 日 月 周 命令' | crontab - 注册内部定时任务。"
        "cron 只接受 Linux crontab 的五个字段，不支持秒字段。"
    ),
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

TOOL_META = {
    "name": "bash",
    "description": (
        "在当前容器中启动 Bash，用于文件处理、系统状态查询和自动化。优先使用该工具操作系统。"
        "支持标准 Bash 语法与容器内任意工作目录。访问其他容器时，docker/docker-compose 仍经宿主机 Unix Socket 代理，支持常规 Docker 命令和完整 docker exec；"
        "Bash 不在宿主机，不能使用 systemctl 或宿主机 /var/log；"
        "docker inspect 会在当前 QQ 会话发送容器状态卡片。"
        "Bash 还提供项目级 crontab 兼容调度：crontab -l 查看当前聊天任务，"
        "crontab -r 删除当前聊天任务，使用 echo/printf '五字段 cron + 命令' | crontab - 注册任务。"
        "表达式严格按 Linux crontab 五字段解释，不支持秒字段；"
        "它不会修改操作系统 crontab；任务由 Sirius 内部调度器持久化并执行，"
        "命令输出会作为上下文在原聊天中生成主动消息。"
    ),
    "version": "1.5.0",
    "side_effect": "unknown",
    "tags": ["bash", "shell", "file", "system", "container", "cron", "schedule"],
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
            "description": "单次 Bash 返回的最大输出字符数，范围 256 到 50000。",
            "default": _DEFAULT_MAX_OUTPUT,
            "group": "限制",
        },
    },
}


def create_background_tasks(ctx: Any) -> list[Any]:
    return _bash_cron.create_background_tasks(ctx, run)


async def run(
    command: str,
    cwd: str = ".",
    timeout_seconds: float = 10.0,
    max_output_chars: int = 8_000,
    data_store: Any = None,
    chat_context: dict[str, Any] | None = None,
    engine_context: Any = None,
    invocation_context: ToolInvocationContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Execute one Bash command with the Sirius Docker bridge."""
    if data_store is not None and data_store.get("_enabled", True) is False:
        return {"success": False, "error": "bash Tool 已被当前人格禁用"}

    policy = _bash_runtime.load_policy(
        data_store,
        default_timeout=_DEFAULT_MAX_TIMEOUT,
        default_output=_DEFAULT_MAX_OUTPUT,
        minimum_output=_MIN_OUTPUT,
    )
    try:
        command_text = _bash_runtime.validate_command(command, max_length=_MAX_COMMAND_LENGTH)
        cwd_path = _bash_runtime.resolve_cwd(cwd)
        timeout = _bash_runtime.bounded_number(
            timeout_seconds, default=10.0, minimum=0.1, maximum=policy["max_timeout_seconds"]
        )
        output_limit = int(
            _bash_runtime.bounded_number(
                max_output_chars,
                default=8_000,
                minimum=_MIN_OUTPUT,
                maximum=policy["max_output_chars"],
            )
        )
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    if not kwargs.get("_skip_crontab", False):
        try:
            cron_request = cron_tasks.parse_crontab_command(command_text)
        except cron_tasks.CronParseError as exc:
            return {"success": False, "error": str(exc)}
        if cron_request is not None:
            if data_store is None:
                return {"success": False, "error": "crontab 需要可持久化的 Bash 数据存储"}
            return _bash_cron.handle_request(
                cron_request,
                cwd=cwd_path,
                data_store=data_store,
                chat_context=chat_context,
                engine_context=engine_context,
                invocation_context=invocation_context,
            )

    bash = _bash_runtime.find_bash()
    if not bash:
        return {
            "success": False,
            "error": "系统未找到 Bash；请安装 Bash 或设置 SIRIUS_BASH_PATH。",
        }

    try:
        environment = _bash_runtime.runtime_environment(data_store)
    except OSError as exc:
        return {"success": False, "error": f"准备人格运行时目录失败: {exc}"}

    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            [
                bash,
                "-o",
                "pipefail",
                "-lc",
                f"{_bash_runtime.docker_function()}\n{command_text}",
            ],
            cwd=str(cwd_path),
            env=environment,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = _bash_runtime.decode_output(
            (exc.stdout or b"") + (exc.stderr or b""), output_limit
        )
        detail = f"命令执行超时（上限 {timeout:g} 秒）"
        if partial:
            detail += f"\n部分输出:\n{partial}"
        return {"success": False, "error": detail}
    except OSError as exc:
        return {"success": False, "error": f"启动 Bash 失败: {exc}"}

    output, inspect_statuses, truncated = _bash_runtime.decode_output_with_inspect_status(
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
        detail += _bash_runtime.container_recovery_hint(command_text)
        return {"success": False, "error": detail, "internal_metadata": metadata}

    cards = await _bash_runtime.send_inspect_status_cards(
        inspect_statuses,
        data_store=data_store,
        chat_context=chat_context,
        engine_context=engine_context,
        invocation_context=invocation_context,
        status_card=_container_status_card,
    )
    metadata["inspect_statuses"] = [item["status"] for item in cards if item.get("status")]
    metadata["status_cards"] = cards
    sent_count = sum(1 for item in cards if item["sent"])
    card_errors = [str(item["error"]) for item in cards if item["error"]]
    text_parts = [output or "命令执行成功，但没有输出。"]
    text_parts.extend(
        _container_status_card.status_summary(item["status"])
        for item in cards
        if item.get("status")
    )
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
