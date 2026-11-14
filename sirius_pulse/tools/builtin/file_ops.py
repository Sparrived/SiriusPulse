"""读、写、改、列、搜她自己的文件。

线上她做自己的项目时，改文件靠 ``python3 -c`` 把整份读进来再 ``s.replace(...)`` 写回去，
看文件靠 ``cat`` / ``sed -n`` / ``nl``，内容一长就撞上 bash 的命令长度上限（两个月 357 次）。
这里把这些动作做成专门的工具：

- ``read``：带行号读一段，长文件用 ``offset`` 接着读；
- ``write``：整份写入（不存在就建，连同上级目录）；
- ``edit``：把一段原文精确替换成新文字，原文必须唯一出现，或者显式 ``replace_all``；
- ``list``：列目录树；
- ``grep``：按正则搜内容。

能读哪里、能写哪里见 ``_persona_paths``：写只能写家目录。工具在线程里执行，不占事件循环。
"""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from sirius_pulse.tools.builtin._internal import _persona_paths
from sirius_pulse.tools.models import ToolInvocationContext

#: 工具结果给模型时整体最多约 6000 字，留出标题与续读提示的余量。
_MAX_RESULT_CHARS = 5_000
_MAX_READ_BYTES = 5 * 1024 * 1024
_MAX_WRITE_CHARS = 500_000
_MAX_LINE_CHARS = 500
_DEFAULT_READ_LINES = 400
_MAX_READ_LINES = 2_000
_MAX_LIST_ENTRIES = 300
_MAX_LIST_DEPTH = 4
_MAX_GREP_MATCHES = 100
_MAX_GREP_FILES = 5_000
_MAX_GREP_FILE_BYTES = 2 * 1024 * 1024
_SKIPPED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache"}
_TEXT_ENCODINGS = ("utf-8", "gb18030")
_DOCUMENT_SUFFIXES = {".pdf", ".docx", ".pptx"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

TOOL_META = {
    "name": "file_ops",
    "description": (
        "读、写、改、列出、搜索你自己的文件（笔记、代码、项目文件），"
        "别用 bash 的 cat / sed / python -c。edit 前先 read 看清原文。只能写你的家目录（~）。"
        "PDF/Word/PPT 用 read_document，图片用 read_image。"
    ),
    "version": "1.0.0",
    "side_effect": "read_only",
    "retry_safe": True,
    "work_mode_only": True,
    "actions": {
        # 写自己家目录里的文件：不是对外发送，自主回合里也能用。
        "write": {"side_effect": "unknown", "retry_safe": True},
        # 第二次执行时原文已经不在了，不能自动重试。
        "edit": {"side_effect": "unknown", "retry_safe": False},
    },
    "tags": ["file", "read", "write", "edit", "grep", "workspace"],
    "parameters": {
        "action": {
            "type": "str",
            "description": "read 读；write 整份写；edit 精确替换；list 列目录；grep 搜内容。",
            "required": True,
            "choices": ["read", "write", "edit", "list", "grep"],
        },
        "path": {
            "type": "str",
            "description": "文件或目录路径，~ 表示你的家目录；list/grep 不填时为家目录。",
            "required": False,
            "default": "",
        },
        "offset": {
            "type": "integer",
            "description": "action=read 时从第几行开始（从 1 数），接着读时填上次结果给的 offset。",
            "required": False,
            "default": 1,
        },
        "limit": {
            "type": "integer",
            "description": f"action=read 时最多读多少行，默认 {_DEFAULT_READ_LINES}，最多 {_MAX_READ_LINES}。",
            "required": False,
            "default": _DEFAULT_READ_LINES,
        },
        "content": {
            "type": "str",
            "description": "action=write 时写入的完整内容。",
            "required": False,
        },
        "old_text": {
            "type": "str",
            "description": "action=edit 时要替换的原文，从 read 的结果里原样复制（不要带行号）。",
            "required": False,
        },
        "new_text": {
            "type": "str",
            "description": "action=edit 时替换成的新文字；留空表示删掉 old_text。",
            "required": False,
            "default": "",
        },
        "replace_all": {
            "type": "bool",
            "description": "action=edit 时 old_text 出现多次也全部替换。",
            "required": False,
            "default": False,
        },
        "pattern": {
            "type": "str",
            "description": "action=grep 时的正则表达式。",
            "required": False,
        },
        "glob": {
            "type": "str",
            "description": "action=list/grep 时只看文件名匹配的文件，例如 *.py、*.md。",
            "required": False,
            "default": "",
        },
        "ignore_case": {
            "type": "bool",
            "description": "action=grep 时忽略大小写。",
            "required": False,
            "default": False,
        },
        "depth": {
            "type": "integer",
            "description": f"action=list 时往下列几层，默认 2，最多 {_MAX_LIST_DEPTH}。",
            "required": False,
            "default": 2,
        },
    },
}


class FileOpError(ValueError):
    """能说清原因的失败，原样交给她。"""


def run(
    action: str,
    path: str = "",
    offset: int = 1,
    limit: int = _DEFAULT_READ_LINES,
    content: str | None = None,
    old_text: str | None = None,
    new_text: str = "",
    replace_all: bool = False,
    pattern: str = "",
    glob: str = "",
    ignore_case: bool = False,
    depth: int = 2,
    *,
    data_store: Any = None,
    engine_context: Any = None,
    invocation_context: ToolInvocationContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    action_key = str(action or "").strip().lower()
    developer = bool(invocation_context and invocation_context.caller_is_developer)
    access = _Access(data_store, engine_context, developer)
    try:
        if action_key == "read":
            return _read(access, path, offset, limit)
        if action_key == "write":
            return _write(access, path, content)
        if action_key == "edit":
            return _edit(access, path, old_text, new_text, _flag(replace_all))
        if action_key == "list":
            return _list(access, path, glob, depth)
        if action_key == "grep":
            return _grep(access, path, pattern, glob, _flag(ignore_case))
    except FileOpError as exc:
        return {"success": False, "error": str(exc)}
    except OSError as exc:
        return {"success": False, "error": f"文件操作失败：{exc.strerror or exc}"}
    return {"success": False, "error": f"不支持的 action: {action}"}


class _Access:
    """一次调用的路径解析与权限判断。"""

    def __init__(self, data_store: Any, engine_context: Any, developer: bool) -> None:
        self.data_store = data_store
        self.engine_context = engine_context
        self.developer = developer

    def resolve(self, path: str, *, default_home: bool = False) -> Path:
        text = str(path or "").strip()
        if not text:
            if not default_home:
                raise FileOpError("path 不能为空")
            text = "~"
        try:
            return _persona_paths.resolve(text, self.data_store)
        except (OSError, ValueError) as exc:
            raise FileOpError(f"路径无效：{exc}") from exc

    def readable(self, path: str, *, default_home: bool = False) -> Path:
        target = self.resolve(path, default_home=default_home)
        if not _persona_paths.can_read(
            target,
            engine_context=self.engine_context,
            data_store=self.data_store,
            developer=self.developer,
        ):
            raise FileOpError("只能读你的家目录、下载下来的聊天文件和工具产物目录里的东西")
        return target

    def writable(self, path: str) -> Path:
        target = self.resolve(path)
        if not _persona_paths.can_write(target, data_store=self.data_store):
            raise FileOpError("只能写你家目录（~）里的文件")
        return target

    def show(self, path: Path) -> str:
        return _persona_paths.display(path, self.data_store)


# ── read ────────────────────────────────────────────────────────────


def _read(access: _Access, path: str, offset: Any, limit: Any) -> dict[str, Any]:
    target = access.readable(path)
    text, _encoding = _load_text(target)
    lines = text.splitlines()
    total = len(lines)
    start = max(1, _int(offset, 1))
    count = max(1, min(_MAX_READ_LINES, _int(limit, _DEFAULT_READ_LINES)))
    if total == 0:
        return _ok(f"{access.show(target)} 是空文件", data={"path": str(target), "total_lines": 0})
    if start > total:
        raise FileOpError(f"offset 超出范围：文件共 {total} 行")

    shown: list[str] = []
    used = 0
    end = start - 1
    for number in range(start, min(total, start + count - 1) + 1):
        line = lines[number - 1]
        if len(line) > _MAX_LINE_CHARS:
            line = line[:_MAX_LINE_CHARS] + f"…（这一行共 {len(lines[number - 1])} 字，已截断）"
        rendered = f"{number:>6}\t{line}"
        if shown and used + len(rendered) + 1 > _MAX_RESULT_CHARS:
            break
        shown.append(rendered)
        used += len(rendered) + 1
        end = number
    done = end >= total
    header = f"{access.show(target)} 第 {start}–{end} 行，共 {total} 行"
    footer = "（已读完）" if done else f"（还没读完：接着读请传 offset={end + 1}）"
    return _ok(
        header,
        body="\n".join(shown),
        footer=footer,
        data={
            "path": str(target),
            "total_lines": total,
            "offset": start,
            "next_offset": None if done else end + 1,
        },
    )


# ── write / edit ────────────────────────────────────────────────────


def _write(access: _Access, path: str, content: Any) -> dict[str, Any]:
    if content is None:
        raise FileOpError("action=write 需要 content")
    text = str(content)
    if len(text) > _MAX_WRITE_CHARS:
        raise FileOpError(f"content 太长了（超过 {_MAX_WRITE_CHARS} 字），请分成几个文件")
    target = access.writable(path)
    if target.is_dir():
        raise FileOpError("这是一个目录，不能当文件写")
    existed = target.exists()
    _atomic_write(target, text, "utf-8")
    lines = len(text.splitlines())
    verb = "覆盖写入" if existed else "新建"
    return _ok(
        f"已{verb} {access.show(target)}（{lines} 行，{len(text)} 字）",
        data={"path": str(target), "created": not existed, "lines": lines},
    )


def _edit(
    access: _Access, path: str, old_text: Any, new_text: Any, replace_all: bool
) -> dict[str, Any]:
    if not old_text:
        raise FileOpError("action=edit 需要 old_text；要整份重写请用 write")
    target = access.writable(path)
    original, encoding = _load_text(target)
    old, new = str(old_text), str(new_text or "")
    if "\r\n" in original and "\r\n" not in old:
        # 文件是 CRLF 换行，她从读出来的结果里复制的是 LF。
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    count = original.count(old)
    if count == 0:
        raise FileOpError("在文件里没找到 old_text；先用 read 看原文，从结果里原样复制（不要带行号，注意空格和缩进）")
    if count > 1 and not replace_all:
        raise FileOpError(f"old_text 在文件里出现了 {count} 次；多带几行上下文让它唯一，或者加 replace_all=true")
    if old == new:
        raise FileOpError("old_text 和 new_text 一样，没有要改的")
    updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
    _atomic_write(target, updated, encoding)

    first_line = original[: original.index(old)].count("\n") + 1
    snippet = _snippet(updated, first_line, new.count("\n") + 1)
    times = f"{count} 处" if replace_all else "1 处"
    return _ok(
        f"已修改 {access.show(target)}（替换了 {times}，从第 {first_line} 行开始）",
        body=snippet,
        data={"path": str(target), "replacements": count if replace_all else 1},
    )


def _snippet(text: str, first_line: int, changed_lines: int) -> str:
    lines = text.splitlines()
    start = max(1, first_line - 3)
    end = min(len(lines), first_line + changed_lines + 2)
    rendered = [f"{n:>6}\t{lines[n - 1][:_MAX_LINE_CHARS]}" for n in range(start, end + 1)]
    return "\n".join(rendered)[:_MAX_RESULT_CHARS]


def _atomic_write(target: Path, text: str, encoding: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding=encoding, newline="") as output:
            output.write(text)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


# ── list / grep ─────────────────────────────────────────────────────


def _list(access: _Access, path: str, glob: str, depth: Any) -> dict[str, Any]:
    root = access.readable(path, default_home=True)
    if not root.is_dir():
        raise FileOpError(f"不是目录：{path}；读文件用 action=read")
    max_depth = max(1, min(_MAX_LIST_DEPTH, _int(depth, 2)))
    pattern = str(glob or "").strip()
    lines: list[str] = []
    truncated = False

    def walk(directory: Path, level: int) -> None:
        nonlocal truncated
        try:
            children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            lines.append(f"{'  ' * level}（读不了：{exc.strerror or exc}）")
            return
        for child in children:
            if len(lines) >= _MAX_LIST_ENTRIES:
                truncated = True
                return
            indent = "  " * level
            if child.is_dir():
                if child.name in _SKIPPED_DIRS:
                    lines.append(f"{indent}{child.name}/（已略过）")
                    continue
                lines.append(f"{indent}{child.name}/")
                if level + 1 < max_depth:
                    walk(child, level + 1)
            elif not pattern or fnmatch.fnmatch(child.name, pattern):
                lines.append(f"{indent}{child.name}  {_size(child)}")

    walk(root, 0)
    header = f"{access.show(root)} 的内容（往下 {max_depth} 层）"
    footer = f"（条目太多，只列了前 {_MAX_LIST_ENTRIES} 个；可以换个更具体的 path）" if truncated else ""
    return _ok(
        header,
        body="\n".join(lines) or "（空目录）",
        footer=footer,
        data={"path": str(root), "entries": len(lines), "truncated": truncated},
    )


def _grep(access: _Access, path: str, pattern: str, glob: str, ignore_case: bool) -> dict[str, Any]:
    if not str(pattern or ""):
        raise FileOpError("action=grep 需要 pattern")
    try:
        regex = re.compile(str(pattern), re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise FileOpError(f"pattern 不是合法的正则：{exc}") from exc
    root = access.readable(path, default_home=True)
    name_filter = str(glob or "").strip()
    matches: list[str] = []
    scanned = 0
    truncated = False
    for file_path in _iter_files(root):
        if name_filter and not fnmatch.fnmatch(file_path.name, name_filter):
            continue
        scanned += 1
        if scanned > _MAX_GREP_FILES:
            truncated = True
            break
        try:
            if file_path.stat().st_size > _MAX_GREP_FILE_BYTES:
                continue
            text, _ = _load_text(file_path)
        except (FileOpError, OSError):
            continue
        label = access.show(file_path) if root.is_dir() else file_path.name
        for number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{label}:{number}: {line.strip()[:300]}")
                if len(matches) >= _MAX_GREP_MATCHES:
                    truncated = True
                    break
        if truncated:
            break
    body, used = [], 0
    for line in matches:
        if used + len(line) + 1 > _MAX_RESULT_CHARS:
            truncated = True
            break
        body.append(line)
        used += len(line) + 1
    header = f"在 {access.show(root)} 里搜 /{pattern}/：{len(matches)} 处匹配"
    footer = "（结果太多已截断；换更具体的 pattern、path 或 glob）" if truncated else ""
    return _ok(
        header,
        body="\n".join(body) or "（没有匹配）",
        footer=footer,
        data={"path": str(root), "matches": len(matches), "truncated": truncated},
    )


def _iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(d for d in subdirs if d not in _SKIPPED_DIRS)
        for name in sorted(files):
            yield Path(directory) / name


# ── helpers ─────────────────────────────────────────────────────────


def _load_text(path: Path) -> tuple[str, str]:
    """读成文字，返回（内容, 编码）；二进制、文档、图片给出能转告的原因。"""
    if not path.exists():
        raise FileOpError(f"找不到这个文件：{path.name}")
    if path.is_dir():
        raise FileOpError("这是一个目录；看目录用 action=list")
    suffix = path.suffix.lower()
    if suffix in _DOCUMENT_SUFFIXES:
        raise FileOpError("PDF / Word / PPT 请用 read_document 读")
    if suffix in _IMAGE_SUFFIXES:
        raise FileOpError("这是图片，用 read_image 看")
    if path.stat().st_size > _MAX_READ_BYTES:
        raise FileOpError("文件太大了（超过 5MB）")
    raw = path.read_bytes()
    if b"\x00" in raw[:8192]:
        raise FileOpError("这不是文本文件")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    for encoding in _TEXT_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def _ok(header: str, *, body: str = "", footer: str = "", data: dict[str, Any]) -> dict[str, Any]:
    parts = [header] + ([body] if body else []) + ([footer] if footer else [])
    return {"success": True, "summary": header, "text_blocks": ["\n\n".join(parts)], "data": data}


def _size(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    for unit, scale in (("MB", 1024**2), ("KB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f}{unit}"
    return f"{size}B"


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)
