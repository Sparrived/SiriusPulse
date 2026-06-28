"""公共 JSON 文件读写工具。

提供原子写入（tmp + replace）和安全读取，统一全项目的 JSON I/O 模式。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def atomic_write_json(path: Path | str, data: Any, *, indent: int | None = 2) -> None:
    """原子写入 JSON 文件：先写临时文件，再 rename 替换。

    避免写入过程中断导致文件损坏。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=indent),
        encoding="utf-8",
    )
    tmp.replace(p)


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
