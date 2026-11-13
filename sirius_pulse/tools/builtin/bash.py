"""人格可调用的 Bash 工具。"""

from __future__ import annotations

import asyncio
import subprocess
import time
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.extension_runtime import BackgroundTaskSpec
from sirius_pulse.tools import cron_tasks
from sirius_pulse.tools.builtin._internal import (
    _bash_background,
    _bash_cron,
    _bash_runtime,
    _container_status_card,
)
from sirius_pulse.tools.models import ToolInvocationContext
from sirius_pulse.visual.tooling import normalise_bool

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
    description=("容器内工作目录；可使用绝对路径。留空或填 . 表示你自己的家目录" "（$HOME，位于人格数据目录内，容器重建后依然存在）。"),
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
_config.group("Bash 执行").add(
    "background_on_timeout",
    type="bool",
    description=("超时后是否转入后台继续执行（默认是）。转入后台时立即返回任务 ID，" "命令结束后会收到结果回调；填 false 则超时直接结束命令。"),
    default=True,
)

TOOL_META = {
    "name": "bash",
    "description": (
        "【适用场景】执行容器内文件处理、系统状态探测、脚本自动化，或管理项目级定时任务。\n"
        "【使用方法】传入 command 执行标准 Bash 命令，支持常规 Linux 命令及代理访问外部容器的 docker 命令；"
        "支持标准五字段 crontab 调度命令（crontab -l 查看任务、crontab -r 删除任务、"
        "printf '五字段cron 命令' | crontab - 注册任务）。\n"
        "【注意事项】命令运行在容器内而非宿主机，不支持 systemctl 或直接访问宿主机文件；"
        "定时任务由内部调度器托管（不支持秒级字段）；执行 docker inspect 会向当前会话发送容器状态卡片；"
        "超时的命令默认转入后台并返回任务 ID（bash_xxxxxxxx），结束后会收到结果回调，"
        "期间不要重复执行同一条命令，可用 background_task 查看进度或停止。"
    ),
    "version": "1.6.0",
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
        "max_background_jobs": {
            "type": "int",
            "description": "同时在后台运行的超时命令上限，范围 0 到 10；0 表示超时直接结束、不转后台。",
            "default": _bash_background.DEFAULT_MAX_BACKGROUND_JOBS,
            "group": "后台",
        },
        "max_background_seconds": {
            "type": "number",
            "description": "转入后台的命令最长运行多久（秒），到点整组结束，范围 60 到 86400。",
            "default": _bash_background.DEFAULT_MAX_BACKGROUND_SECONDS,
            "group": "后台",
        },
    },
}


def create_background_tasks(ctx: Any) -> list[Any]:
    owner = _bash_background.owner_key(ctx.get_data_store("bash"))

    async def _background_tick() -> None:
        await _bash_background.process_once(ctx, owner)

    return [
        *_bash_cron.create_background_tasks(ctx, run),
        BackgroundTaskSpec(
            name="bash_background_jobs",
            interval_seconds=_bash_background.TICK_SECONDS,
            task_func=_background_tick,
        ),
    ]


def create_on_unload(ctx: Any) -> None:
    """人格停下时结束她名下仍在后台跑的命令，不留孤儿进程。"""
    _bash_background.kill_all(_bash_background.owner_key(ctx.get_data_store("bash")))


async def run(
    command: str,
    cwd: str = ".",
    timeout_seconds: float = 10.0,
    max_output_chars: int = 8_000,
    background_on_timeout: bool = True,
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
        environment = _bash_runtime.runtime_environment(data_store)
        # 人格自己的服务凭据（如 GH_TOKEN）只经 env= 传下去，不进命令行。
        environment.update(_bash_runtime.account_environment(data_store))
        cwd_path = _bash_runtime.resolve_cwd(cwd, home=_bash_runtime.home_root_path(data_store))
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
    except OSError as exc:
        return {"success": False, "error": f"准备人格运行时目录失败: {exc}"}

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

    task_id = _bash_background.new_task_id()
    try:
        log_path = _bash_background.log_dir(data_store) / f"{task_id}.log"
        process = _bash_background.spawn(
            [
                bash,
                "-o",
                "pipefail",
                "-lc",
                # ``-l`` has already sourced /etc/profile by the time this runs,
                # and that file resets PATH, dropping the runtime directories.
                # Re-exporting here is the only assignment that survives.
                f"{_bash_runtime.runtime_exports(environment)}"
                f"{_bash_runtime.docker_function()}\n{command_text}",
            ],
            cwd=str(cwd_path),
            env=environment,
            log_path=log_path,
            popen=subprocess.Popen,
        )
    except OSError as exc:
        return {"success": False, "error": f"启动 Bash 失败: {exc}"}

    try:
        returncode = await asyncio.to_thread(process.wait, timeout)
    except subprocess.TimeoutExpired:
        return await _handle_timeout(
            task_id=task_id,
            process=process,
            log_path=log_path,
            command_text=command_text,
            cwd=str(cwd_path),
            timeout=timeout,
            output_limit=output_limit,
            background=normalise_bool(background_on_timeout),
            data_store=data_store,
            chat_context=chat_context,
            invocation_context=invocation_context,
        )
    except asyncio.CancelledError:
        # 外层（工具执行超时、关停）取消了这次调用：结果已经没人接，别留孤儿进程。
        asyncio.get_running_loop().run_in_executor(None, _bash_background.terminate_tree, process)
        raise

    raw_output = _read_and_remove(log_path)
    output, inspect_statuses, truncated = _bash_runtime.decode_output_with_inspect_status(
        raw_output, output_limit
    )
    metadata = {
        "cwd": str(cwd_path),
        "returncode": returncode,
        "command_length": len(command_text),
        "docker_bridge_enabled": True,
        "truncated": truncated,
    }
    if returncode != 0:
        detail = f"命令退出码 {returncode}"
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


def _read_and_remove(log_path: Any) -> bytes:
    try:
        raw = log_path.read_bytes()
    except OSError:
        return b""
    try:
        log_path.unlink()
    except OSError:
        pass
    return raw


def _store_number(
    data_store: Any, key: str, default: float, minimum: float, maximum: float
) -> float:
    value = data_store.get(key, default) if data_store is not None else default
    return _bash_runtime.bounded_number(value, default=default, minimum=minimum, maximum=maximum)


async def _handle_timeout(
    *,
    task_id: str,
    process: Any,
    log_path: Any,
    command_text: str,
    cwd: str,
    timeout: float,
    output_limit: int,
    background: bool,
    data_store: Any,
    chat_context: dict[str, Any] | None,
    invocation_context: ToolInvocationContext | None,
) -> dict[str, Any]:
    """前台等不完：能转后台就转后台并给出任务 ID，否则结束命令并带回部分输出。"""
    owner = _bash_background.owner_key(data_store)
    max_jobs = int(
        _store_number(
            data_store, "max_background_jobs", _bash_background.DEFAULT_MAX_BACKGROUND_JOBS, 0, 10
        )
    )
    full = background and max_jobs > 0 and _bash_background.running_count(owner) >= max_jobs
    if background and max_jobs > 0 and not full:
        lifetime = _store_number(
            data_store,
            "max_background_seconds",
            _bash_background.DEFAULT_MAX_BACKGROUND_SECONDS,
            60.0,
            86_400.0,
        )
        context = dict(chat_context or {})
        if not context and invocation_context is not None:
            context = dict(invocation_context.chat_context)
        caller = invocation_context
        job = _bash_background.BashJob(
            id=task_id,
            owner=owner,
            command=command_text,
            cwd=cwd,
            log_path=log_path,
            process=process,
            deadline=time.monotonic() + lifetime,
            output_limit=output_limit,
            group_id=str(context.get("group_id") or ""),
            user_id=(caller.caller_user_id if caller is not None else "")
            or str(context.get("user_id") or ""),
            user_name=caller.caller_name if caller is not None else "",
            adapter_type=str(context.get("adapter_type") or ""),
            caller_is_developer=bool(caller is not None and caller.caller_is_developer),
            self_initiated=bool(caller is not None and caller.self_initiated),
        )
        _bash_background.register(job)
        partial = job.output_tail(output_limit)
        text = (
            f"命令运行超过 {timeout:g} 秒，已转入后台继续执行，任务 ID：{task_id}。"
            "命令结束后你会收到结果回调；在那之前不要重复执行同一条命令，也不要假装已经有结果。"
            f"想看进度或中途停止，用 background_task（action=status 或 stop，task_id={task_id}）。"
        )
        if partial:
            text += f"\n目前的输出:\n{partial}"
        return {
            "success": True,
            "summary": f"命令已转入后台（{task_id}）",
            "text_blocks": [text],
            "data": {"task_id": task_id, "status": "running"},
            "internal_metadata": {"cwd": cwd, "background_task_id": task_id},
        }

    await asyncio.to_thread(_bash_background.terminate_tree, process)
    partial = _bash_runtime.decode_output(_read_and_remove(log_path), output_limit)
    detail = f"命令执行超时（上限 {timeout:g} 秒）"
    if full:
        detail += f"；后台任务已满（最多 {max_jobs} 个），没能转入后台"
    if partial:
        detail += f"\n部分输出:\n{partial}"
    return {"success": False, "error": detail}
