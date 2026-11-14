"""她自己的地方：哪些路径能读、哪些能写。

``read_document`` 和 ``file_ops`` 共用这一份规则，免得两个工具对「她的地方」各说各话：

- 能读：家目录、下载下来的聊天文件（``group_files``）、工具产物（``tool_data/artifacts``）；
  开发者调用时任意路径都能读。
- 能写：只有家目录，谁调用都一样。配置、密钥、记忆数据都在家目录外面。

判断前先解析符号链接，家目录里指向外面的链接借不了道。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sirius_pulse.tools.builtin._internal import _bash_runtime


def persona_dir(engine_context: Any, data_store: Any) -> Path | None:
    """人格数据目录；引擎上下文优先，没有时从工具数据存储的位置推出来。"""
    if engine_context is not None:
        get_work_path = getattr(engine_context, "get_work_path", None)
        work_path = get_work_path() if callable(get_work_path) else None
        if work_path:
            return Path(work_path)
    runtime_root = _bash_runtime.runtime_root_path(data_store)
    return runtime_root.parent if runtime_root is not None else None


def home_dir(data_store: Any) -> Path | None:
    return _bash_runtime.home_root_path(data_store)


def resolve(path: str, data_store: Any) -> Path:
    """``~`` 展开到她的家目录，再解析成绝对路径（含符号链接）。"""
    return _bash_runtime.expand_persona_path(str(path or ""), home=home_dir(data_store))


def readable_roots(engine_context: Any, data_store: Any) -> list[Path]:
    base = persona_dir(engine_context, data_store)
    if base is None:
        return []
    return [base / "home", base / "group_files", base / "tool_data" / "artifacts"]


def is_within(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def can_read(path: Path, *, engine_context: Any, data_store: Any, developer: bool) -> bool:
    if developer:
        return True
    return any(is_within(path, root) for root in readable_roots(engine_context, data_store))


def can_write(path: Path, *, data_store: Any) -> bool:
    home = home_dir(data_store)
    return home is not None and is_within(path, home)


def display(path: Path, data_store: Any) -> str:
    """给她看的路径：家目录里的写成 ``~/…``，其他照原样。"""
    home = home_dir(data_store)
    if home is not None:
        try:
            relative = path.resolve().relative_to(home.resolve())
        except (OSError, ValueError):
            return str(path)
        text = relative.as_posix()
        return "~" if text == "." else f"~/{text}"
    return str(path)
