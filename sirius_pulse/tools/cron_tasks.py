"""Bash 工具的 crontab 命令解析；表达式的校验与匹配统一在 :mod:`sirius_pulse.scheduling.cron`。"""

from __future__ import annotations

import re
import shlex
from datetime import datetime
from typing import Any

from sirius_pulse.scheduling.cron import (
    CRON_FIELD_COUNT,
    CRON_TIMEZONE,
    CronParseError,
    cron_matches,
    next_fire,
    validate_expression,
)

__all__ = [
    "CRON_FIELD_COUNT",
    "CRON_TIMEZONE",
    "CronParseError",
    "contains_crontab",
    "cron_matches",
    "next_fire",
    "parse_crontab_command",
    "parse_crontab_entry",
    "task_is_due",
    "validate_expression",
]

_CRONTAB_RE = re.compile(r"(?:^|[|;&])\s*crontab(?:\s|$)")
_CRONTAB_LIST = r"crontab\s+-l(?:\s+2>/dev/null)?"
_CRONTAB_LIST_WITH_PROBE = _CRONTAB_LIST + r"(?:\s*;\s*echo\s+[\"']---exit:\s*\$\?\s*---[\"'])?"


def contains_crontab(command: str) -> bool:
    return bool(_CRONTAB_RE.search(command))


def parse_crontab_command(command: str) -> dict[str, Any] | None:
    """Parse the small, model-friendly crontab command surface.

    Supported forms intentionally mirror common Linux examples while keeping
    shell execution out of the registration path:
    ``crontab -l``, ``crontab -r``, and ``echo/printf ... | crontab -``.
    """
    text = command.strip()
    if not contains_crontab(text):
        return None
    if re.fullmatch(_CRONTAB_LIST_WITH_PROBE, text):
        return {"action": "list"}
    if re.fullmatch(r"crontab\s+-r", text):
        return {"action": "remove"}

    match = re.fullmatch(
        rf"(.+?)\s*\|\s*crontab\s+-(?:\s*(?:&&|;)\s*{_CRONTAB_LIST})?",
        text,
        flags=re.DOTALL,
    )
    if not match:
        raise CronParseError("仅支持 crontab -l、crontab -r，以及 echo/printf 'cron条目' | crontab -")

    try:
        producer = shlex.split(match.group(1), posix=True)
    except ValueError as exc:
        raise CronParseError(f"crontab 输入命令解析失败: {exc}") from exc
    if not producer or producer[0] not in {"echo", "printf"}:
        raise CronParseError("crontab - 只接受 echo 或 printf 生成的定时条目")

    if producer[0] == "echo":
        values = producer[1:]
        if not values:
            raise CronParseError("crontab 条目不能为空")
        payload = " ".join(values)
    else:
        if len(producer) >= 3 and "%s" in producer[1]:
            values = producer[2:]
            payload = "\n".join(values) if "\\n" in producer[1] else " ".join(values)
        elif len(producer) == 2 and producer[1]:
            payload = producer[1].replace("\\n", "\n")
        else:
            raise CronParseError("printf 形式应为 printf '%s\\n' 'cron条目' | crontab -")
        if not payload:
            raise CronParseError("crontab 条目不能为空")

    entries = []
    for line in payload.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        expression, scheduled_command = parse_crontab_entry(line)
        entries.append({"expression": expression, "command": scheduled_command})
    if not entries:
        raise CronParseError("crontab 条目不能为空")
    return {"action": "install", "entries": entries}


def parse_crontab_entry(line: str) -> tuple[str, str]:
    parts = line.split(None, 5)
    if len(parts) != 6:
        raise CronParseError("cron 条目必须是五个时间字段加一条命令")
    expression = " ".join(parts[:5])
    command = parts[5].strip()
    validate_expression(expression)
    if not command:
        raise CronParseError("cron 条目缺少要执行的命令")
    return expression, command


def task_is_due(job: dict[str, Any], now: datetime | None = None) -> tuple[bool, str]:
    now = (now or datetime.now(CRON_TIMEZONE)).astimezone(CRON_TIMEZONE)
    expression = str(job.get("expression", ""))
    key = f"cron:{now:%Y%m%d%H%M}"
    return cron_matches(expression, now), key
