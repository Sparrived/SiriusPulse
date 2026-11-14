"""公共 JSON 文件读写工具。

提供原子写入（tmp + replace）和安全读取，统一全项目的 JSON I/O 模式。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Windows 上 os.replace 被索引/杀软短暂持有时会抛 WinError 5，通常在几十毫秒内自解。
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_SECONDS = 0.05


def replace_with_retry(tmp: Path | str, target: Path | str) -> None:
    """原子替换目标文件，遇到瞬时占用时退避重试。

    ``Path.replace`` 在 Windows 上并不总是瞬时成功：索引服务、杀毒软件或另一个
    刚读过该文件的进程会短暂持有句柄，于是抛出 ``PermissionError``
    （``WinError 5``）。这类冲突是可自愈的，退避重试即可，否则调用方会看到一次
    没有真实语义的写盘失败。
    """
    source = Path(tmp)
    destination = Path(target)
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            source.replace(destination)
            return
        except OSError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))


def write_bytes_durably(path: Path | str, data: bytes) -> None:
    """写文件并 fsync，保证 rename 之前内容已经落到磁盘。

    只做 tmp + rename 的话，断电后 rename 可能先于数据落盘，留下一个长度为零或
    内容残缺的「新」文件——原子替换挡住的是进程中途崩溃，挡不住掉电。
    """
    with Path(path).open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def atomic_write_json(
    path: Path | str, data: Any, *, indent: int | None = 2, durable: bool = False
) -> None:
    """原子写入 JSON 文件：先写临时文件，再 rename 替换。

    避免写入过程中断导致文件损坏。``durable=True`` 时额外 fsync：只给丢了就无法
    重建的数据用（记忆单元），每条消息都要写的快照不值得为它多等一次磁盘。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    if durable:
        write_bytes_durably(tmp, text.encode("utf-8"))
    else:
        tmp.write_text(text, encoding="utf-8")
    replace_with_retry(tmp, p)


def read_json(path: Path | str, default: Any = None) -> Any:
    """安全读取 JSON 文件，失败时返回 default。"""
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取 JSON 文件失败 %s: %s", p, exc)
        return default
