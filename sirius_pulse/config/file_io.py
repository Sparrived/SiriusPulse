"""原子文件 I/O 工具函数。

为 Plugin 和 Skill 系统提供统一的 JSON 持久化能力。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def atomic_json_save(path: Path, data: dict[str, Any], *, indent: int = 2) -> None:
    """原子保存 JSON 数据到指定路径。

    先写同名 .tmp 文件，再通过 Path.replace() 原子覆盖目标路径，
    避免并发写入时损坏数据。

    此函数被 PluginConfigManager、SkillDataStore 和
    _save_persona_skill_config 共同使用。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=indent),
        encoding="utf-8",
    )
    tmp.replace(path)
