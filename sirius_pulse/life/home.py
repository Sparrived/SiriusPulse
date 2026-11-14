"""她家目录的一眼概览：开工前就知道自己的东西在哪、最近在弄什么。

线上她自己的回合里，bash 有一万七千多次以 ``cd $HOME`` / ``ls -la ~ ~/notes ~/projects`` 开头——
每次开工先把家目录翻一遍，因为没人告诉她上次做到哪、东西放在哪。这里把那一眼替她看好：

- 最近改过的文件（她上次在弄什么）；
- 顶层文件夹，按最近改动排序，带文件数；很久没动的只列名字；
- 根目录下的零散文件数。

隐藏目录（``.cache``、``.local`` 之类）、``node_modules``、``__pycache__``、``site-packages``
不算：线上她家里十万个文件，八万多个在 ``.cache``，那些不是她的东西。

纯函数、会读文件系统：调用方要放到线程里执行（见 ``core.life_bridge.home_overview``）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sirius_pulse.life.clock import to_local

_SKIPPED_NAMES = frozenset(
    {"node_modules", "__pycache__", "site-packages", "dist-packages", "venv", "env"}
)
#: 最多看这么多个文件；再多就只统计前面这些，免得一次概览花太久。
MAX_SCANNED_FILES = 20_000
_RECENT_FILES = 10
_LISTED_FOLDERS = 12
_NAMED_STALE_FOLDERS = 8
_NAMED_LOOSE_FILES = 4


@dataclass
class _Folder:
    name: str
    files: int = 0
    latest: float = 0.0


@dataclass
class _Scan:
    total: int = 0
    truncated: bool = False
    recent: list[tuple[float, str]] = field(default_factory=list)
    folders: dict[str, _Folder] = field(default_factory=dict)
    loose: list[tuple[float, str]] = field(default_factory=list)


def summarize_home(home: Path, *, now: datetime) -> str:
    """渲染家目录概览；家目录不存在或空着时返回空串。"""
    if not home.is_dir():
        return ""
    scan = _scan(home)
    if scan.total == 0 and not scan.folders:
        return ""

    counted = f"{scan.total} 个文件" + ("以上（太多了，只数了一部分）" if scan.truncated else "")
    lines = [
        "【你的家目录 ~】",
        f"开工前替你看的一眼（不含隐藏目录和缓存）：共 {counted}。" "要细看用 file_ops 的 list / grep，不用先 ls 一遍。",
    ]
    if scan.recent:
        lines.append("最近改过的文件：")
        for mtime, path in sorted(scan.recent, reverse=True)[:_RECENT_FILES]:
            lines.append(f"- ~/{path}（{_when(mtime, now)}）")

    folders = sorted(scan.folders.values(), key=lambda f: f.latest, reverse=True)
    if folders:
        lines.append("文件夹（最近有改动的在前）：")
        for folder in folders[:_LISTED_FOLDERS]:
            when = _when(folder.latest, now) if folder.latest else "空的"
            lines.append(f"- {folder.name}/（{folder.files} 个文件，{when}）")
        stale = folders[_LISTED_FOLDERS:]
        if stale:
            names = "、".join(f.name for f in stale[:_NAMED_STALE_FOLDERS])
            more = f" 等 {len(stale)} 个" if len(stale) > _NAMED_STALE_FOLDERS else ""
            lines.append(f"- 很久没动的：{names}{more}")

    if scan.loose:
        newest = max(mtime for mtime, _ in scan.loose)
        names = "、".join(name for _, name in sorted(scan.loose, reverse=True)[:_NAMED_LOOSE_FILES])
        lines.append(f"~ 根目录下还有 {len(scan.loose)} 个零散文件（最近 {_when(newest, now)}），比如 {names}")
    return "\n".join(lines)


def _scan(home: Path) -> _Scan:
    scan = _Scan()
    try:
        top_entries = list(os.scandir(home))
    except OSError:
        return scan
    for entry in top_entries:
        if _skipped(entry.name):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                folder = scan.folders.setdefault(entry.name, _Folder(entry.name))
                _walk_folder(Path(entry.path), entry.name, folder, scan)
            elif entry.is_file(follow_symlinks=False):
                mtime = entry.stat(follow_symlinks=False).st_mtime
                scan.loose.append((mtime, entry.name))
                _count(scan, mtime, entry.name)
        except OSError:
            continue
        if scan.truncated:
            break
    return scan


def _walk_folder(root: Path, label: str, folder: _Folder, scan: _Scan) -> None:
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = [d for d in subdirs if not _skipped(d)]
        relative_dir = Path(label) / Path(directory).relative_to(root)
        for name in files:
            try:
                mtime = os.stat(os.path.join(directory, name), follow_symlinks=False).st_mtime
            except OSError:
                continue
            folder.files += 1
            folder.latest = max(folder.latest, mtime)
            _count(scan, mtime, (relative_dir / name).as_posix())
            if scan.truncated:
                return


def _count(scan: _Scan, mtime: float, relative: str) -> None:
    scan.total += 1
    scan.recent.append((mtime, relative))
    if len(scan.recent) > _RECENT_FILES * 4:
        scan.recent = sorted(scan.recent, reverse=True)[:_RECENT_FILES]
    if scan.total >= MAX_SCANNED_FILES:
        scan.truncated = True


def _skipped(name: str) -> bool:
    return name.startswith(".") or name in _SKIPPED_NAMES


def _when(mtime: float, now: datetime) -> str:
    moment = to_local(datetime.fromtimestamp(mtime, tz=timezone.utc))
    today = to_local(now).date()
    if moment.date() == today:
        return f"今天 {moment:%H:%M}"
    if moment.date() == today - timedelta(days=1):
        return f"昨天 {moment:%H:%M}"
    return f"{moment:%m-%d}"
