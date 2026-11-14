"""Read persona-local and packaged default Agent Skills without execution."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_MAX_CHARS = 4_500
_MAX_CHARS = 5_000
_MAX_FILE_BYTES = 512 * 1024


TOOL_META = {
    "name": "read_skill",
    "description": (
        "做专门的任务前，先读对应 Skill 的操作说明和附带文件。不填 skill_name 会列出有哪些 Skill。"
        "只读；要跑 Skill 里的脚本用 bash。"
    ),
    "version": "1.0.0",
    "retry_safe": True,
    "side_effect": "read_only",
    "work_mode_only": True,
    "tags": ["skill", "read", "reference"],
    "parameters": [
        {
            "name": "skill_name",
            "type": "str",
            "description": "Skill 名称或相对于 skills 目录的目录名；留空列出可用 Skill。",
            "required": False,
            "default": "",
        },
        {
            "name": "relative_path",
            "type": "str",
            "description": "Skill 目录内的相对文件路径，默认 SKILL.md。禁止跳出 Skill 目录。",
            "required": False,
            "default": "SKILL.md",
        },
        {
            "name": "offset",
            "type": "int",
            "description": "从文件字符偏移量开始读取，用于继续读取大文件。",
            "required": False,
            "default": 0,
        },
        {
            "name": "max_chars",
            "type": "int",
            "description": "本次最多返回的字符数，范围 256-5000，默认 4500。",
            "required": False,
            "default": _DEFAULT_MAX_CHARS,
        },
    ],
}


@dataclass(frozen=True, slots=True)
class _SkillEntry:
    identifier: str
    display_name: str
    description: str
    skill_file: Path

    @property
    def directory(self) -> Path:
        return self.skill_file.parent


def run(
    skill_name: str = "",
    relative_path: str = "SKILL.md",
    offset: int = 0,
    max_chars: int = _DEFAULT_MAX_CHARS,
    data_store: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """List or read a persona-local Skill file."""
    skills_dir = _skills_dir(data_store)
    if skills_dir is None:
        return {"success": False, "error": "无法确定当前人格的 skills 目录"}

    entries = _discover_available_skills(skills_dir)
    requested_name = str(skill_name or "").strip()
    if not requested_name:
        return _list_skills(entries)

    entry = _find_skill(entries, requested_name)
    if entry is None:
        return {
            "success": False,
            "error": f"未找到 Skill: {requested_name}。请先调用 read_skill 列出可用 Skill。",
        }

    try:
        start = int(offset)
        limit = int(max_chars)
    except (TypeError, ValueError):
        return {"success": False, "error": "offset 和 max_chars 必须是整数"}
    if start < 0:
        return {"success": False, "error": "offset 不能小于 0"}
    if not 256 <= limit <= _MAX_CHARS:
        return {"success": False, "error": f"max_chars 必须在 256-{_MAX_CHARS} 之间"}

    target = _resolve_skill_file(entry, relative_path)
    if target is None:
        return {"success": False, "error": "relative_path 必须位于当前 Skill 目录内"}
    if not target.is_file():
        return {"success": False, "error": f"Skill 文件不存在: {relative_path}"}
    try:
        if target.stat().st_size > _MAX_FILE_BYTES:
            return {"success": False, "error": "Skill 文件超过 512 KiB，拒绝读取"}
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"success": False, "error": "Skill 文件不是 UTF-8 文本，无法读取"}
    except OSError as exc:
        return {"success": False, "error": f"读取 Skill 文件失败: {exc}"}

    chunk = content[start : start + limit]
    next_offset = start + len(chunk)
    has_more = next_offset < len(content)
    relative_target = target.relative_to(entry.directory).as_posix()
    display = (
        f"Skill: {entry.display_name} ({entry.identifier})\n"
        f"File: {target}\n"
        f"Chunk: offset={start}, chars={len(chunk)}, total={len(content)}, "
        f"has_more={str(has_more).lower()}\n\n"
        f"{chunk}"
    )
    return {
        "success": True,
        "data": {
            "skill_name": entry.identifier,
            "display_name": entry.display_name,
            "relative_path": relative_target,
            "path": str(target),
            "offset": start,
            "next_offset": next_offset if has_more else None,
            "total_chars": len(content),
            "has_more": has_more,
            "content": chunk,
        },
        "text_blocks": [display],
        "internal_metadata": {
            "model_content_kind": "skill",
            "skill_name": entry.identifier,
            "relative_path": relative_target,
        },
    }


def _skills_dir(data_store: Any) -> Path | None:
    store_path = getattr(data_store, "store_path", None)
    if not store_path:
        return None
    try:
        # ToolDataStore lives at <persona>/tool_data/read_skill.json.
        return Path(store_path).resolve().parent.parent / "skills"
    except (OSError, TypeError, ValueError):
        return None


def _bundled_skills_dir() -> Path:
    return Path(__file__).resolve().parent / "_default_skills"


def _discover_available_skills(persona_skills_dir: Path) -> list[_SkillEntry]:
    """Merge packaged defaults with persona overrides of the same directory name."""
    entries_by_identifier = {
        _canonical_skill_identifier(entry.identifier): entry
        for entry in _discover_skills(_bundled_skills_dir())
    }
    for entry in _discover_skills(persona_skills_dir):
        entries_by_identifier[_canonical_skill_identifier(entry.identifier)] = entry
    return [entries_by_identifier[key] for key in sorted(entries_by_identifier)]


def _canonical_skill_identifier(identifier: str) -> str:
    return identifier.replace("_", "-")


def _discover_skills(skills_dir: Path) -> list[_SkillEntry]:
    if not skills_dir.is_dir():
        return []
    root = skills_dir.resolve()
    entries: list[_SkillEntry] = []
    for skill_file in sorted(skills_dir.rglob("SKILL.md")):
        try:
            resolved_file = skill_file.resolve()
            if not resolved_file.is_relative_to(root) or not resolved_file.is_file():
                continue
            if resolved_file.stat().st_size > _MAX_FILE_BYTES:
                continue
            relative_dir = resolved_file.parent.relative_to(root).as_posix()
            frontmatter = _read_frontmatter(resolved_file)
            display_name = frontmatter.get("name") or relative_dir
            entries.append(
                _SkillEntry(
                    identifier=relative_dir,
                    display_name=display_name,
                    description=frontmatter.get("description", ""),
                    skill_file=resolved_file,
                )
            )
        except (OSError, ValueError):
            continue
    return entries


def _find_skill(entries: list[_SkillEntry], requested_name: str) -> _SkillEntry | None:
    normalized = requested_name.replace("\\", "/").strip("/")
    aliases = {normalized, normalized.replace("_", "-")}
    for entry in entries:
        if aliases.intersection({entry.identifier, entry.display_name}):
            return entry
    return None


def _resolve_skill_file(entry: _SkillEntry, relative_path: str) -> Path | None:
    requested = str(relative_path or "SKILL.md").strip()
    if not requested:
        requested = "SKILL.md"
    try:
        skill_dir = entry.directory.resolve()
        target = (skill_dir / requested).resolve()
        if not target.is_relative_to(skill_dir):
            return None
        return target
    except (OSError, ValueError):
        return None


def _list_skills(entries: list[_SkillEntry]) -> dict[str, Any]:
    if not entries:
        return {
            "success": True,
            "data": {"skills": []},
            "text_blocks": ["当前人格没有可用的 Agent Skill。"],
        }
    skills = [
        {
            "name": entry.identifier,
            "display_name": entry.display_name,
            "description": entry.description,
        }
        for entry in entries
    ]
    lines = ["当前人格可用的 Agent Skill："]
    for item in skills:
        description = item["description"] or "未提供 description"
        lines.append(f"- {item['name']} ({item['display_name']}): {description}")
    return {
        "success": True,
        "data": {"skills": skills},
        "text_blocks": ["\n".join(lines)],
    }


def _read_frontmatter(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        return {}

    result: dict[str, str] = {}
    index = 1
    while index < end:
        line = lines[index]
        if ":" not in line:
            index += 1
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if key == "description" and value in {">", "|-", ">-", "|"}:
            parts: list[str] = []
            index += 1
            while index < end and (
                not lines[index].strip() or lines[index].startswith((" ", "\t"))
            ):
                parts.append(lines[index].strip())
                index += 1
            result[key] = " ".join(part for part in parts if part)
            continue
        result[key] = _strip_yaml_scalar(value)
        index += 1
    return {key: value for key, value in result.items() if value}


def _strip_yaml_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
        try:
            parsed = ast.literal_eval(value)
            return str(parsed)
        except (SyntaxError, ValueError):
            return value[1:-1]
    return value
