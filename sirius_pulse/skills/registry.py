"""Skill registry — discovers, loads, and manages skill definitions.

Skills are Python files residing in {work_path}/skills/ that expose:
- SKILL_META: dict with name, description, parameters, version (optional)
- run(**kwargs) -> Any: the callable entry point
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from sirius_pulse.skills.dependency_resolver import resolve_skill_dependencies
from sirius_pulse.skills.models import (
    SkillDefinition,
    SkillInvocationContext,
    SkillParameter,
    SkillPassiveType,
)

logger = logging.getLogger(__name__)

_SKILLS_README = """# skills 目录说明

此目录用于存放 Sirius Chat 在当前 work_path 下可自动发现的外部 SKILL 文件。

- 每个 SKILL 使用单独的 Python 文件。
- 文件需导出 SKILL_META 字典和 run() 函数。
- 文件名建议使用英文、数字、下划线，避免以下划线开头。
- 当会话启用 SKILL 系统时，框架会自动扫描此目录。

最小示例：

```python
SKILL_META = {
    "name": "hello_skill",
    "description": "返回简单问候语",
    "parameters": {
        "name": {
            "type": "str",
            "description": "要问候的名字",
            "required": True,
        }
    },
}


def run(name: str, **kwargs):
    return {"message": f"你好，{name}"}
```
"""


class SkillRegistry:
    """Discovers and manages skill definitions from a directory."""

    def __init__(self) -> None:
        self._skills: dict[str, SkillDefinition] = {}

    @property
    def skill_names(self) -> list[str]:
        return list(self._skills.keys())

    def get(self, name: str) -> SkillDefinition | None:
        return self._skills.get(name)

    def all_skills(self) -> list[SkillDefinition]:
        return list(self._skills.values())

    def passive_skills(self) -> list[SkillDefinition]:
        """Return all passive skills (those with background tasks or triggers)."""
        return [s for s in self._skills.values() if s.is_passive]

    def passive_skills_by_type(self, passive_type: SkillPassiveType) -> list[SkillDefinition]:
        """Return passive skills matching the given type."""
        return [s for s in self._skills.values() if s.passive_type == passive_type]

    def register(self, skill: SkillDefinition) -> None:
        """Manually register a skill definition."""
        self._skills[skill.name] = skill

    def replace_all(self, skills: list[SkillDefinition]) -> None:
        """Replace the whole registry atomically."""
        self._skills = {skill.name: skill for skill in skills}

    @staticmethod
    def ensure_skills_directory(skills_dir: Path) -> None:
        """Ensure the skills directory and its README bootstrap file exist."""
        skills_dir.mkdir(parents=True, exist_ok=True)
        readme_path = skills_dir / "README.md"
        if not readme_path.exists():
            readme_path.write_text(_SKILLS_README, encoding="utf-8")

    @staticmethod
    def builtin_skills_dir() -> Path:
        """Return the package directory containing built-in skills."""
        return Path(__file__).resolve().parent / "builtin"

    def _load_builtin_skills(self, *, auto_install_deps: bool) -> int:
        """Load package-provided built-in skills into the registry."""
        loaded = 0
        builtin_dir = self.builtin_skills_dir()
        if not builtin_dir.exists():
            return loaded
        for py_file in sorted(builtin_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                self._install_skill_dependencies(py_file, auto_install_deps=auto_install_deps)
                skill = self._load_skill_file(py_file)
                if skill is not None:
                    self._skills[skill.name] = skill
                    loaded += 1
            except Exception as exc:
                logger.warning("加载内置SKILL失败 (%s): %s", py_file.name, exc)
        return loaded

    def load_from_directory(
        self,
        skills_dir: Path,
        *,
        auto_install_deps: bool = True,
        include_builtin: bool = False,
    ) -> int:
        """Load all *.py skill files from a directory.

        Args:
            skills_dir: Directory containing SKILL Python files.
            auto_install_deps: If True, automatically install missing
                third-party dependencies declared in SKILL_META or
                detected from import statements (uses ``uv`` / ``pip``).
            include_builtin: If True, pre-load package-provided built-in skills
                before scanning the workspace directory. Workspace skills with the
                same name override built-ins.

        Returns the number of skills successfully loaded.
        """
        self.ensure_skills_directory(skills_dir)

        baseline = len(self._skills)
        if include_builtin:
            self._load_builtin_skills(auto_install_deps=auto_install_deps)

        for py_file in sorted(skills_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                self._install_skill_dependencies(py_file, auto_install_deps=auto_install_deps)

                skill = self._load_skill_file(py_file)
                if skill is not None:
                    self._skills[skill.name] = skill
                    logger.info(
                        "新技能到手！%s v%s（从 %s 学来的）",
                        skill.name,
                        skill.version,
                        py_file.name,
                    )
            except Exception as exc:
                logger.warning("加载SKILL文件失败 (%s): %s", py_file.name, exc)
        return max(0, len(self._skills) - baseline)

    def reload_from_directory(
        self,
        skills_dir: Path,
        *,
        auto_install_deps: bool = True,
        include_builtin: bool = False,
    ) -> int:
        """Reload all skill files from a directory, replacing removed entries too."""
        self.ensure_skills_directory(skills_dir)

        loaded_skills: list[SkillDefinition] = []
        if include_builtin:
            builtin_dir = self.builtin_skills_dir()
            if builtin_dir.exists():
                for py_file in sorted(builtin_dir.glob("*.py")):
                    if py_file.name.startswith("_"):
                        continue
                    try:
                        self._install_skill_dependencies(
                            py_file, auto_install_deps=auto_install_deps
                        )
                        skill = self._load_skill_file(py_file)
                        if skill is not None:
                            loaded_skills.append(skill)
                            logger.info(
                                "内置技能 %s v%s 已重新加载（来源：%s）",
                                skill.name,
                                skill.version,
                                py_file.name,
                            )
                    except Exception as exc:
                        logger.warning("重载内置SKILL失败 (%s): %s", py_file.name, exc)

        for py_file in sorted(skills_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                self._install_skill_dependencies(py_file, auto_install_deps=auto_install_deps)

                skill = self._load_skill_file(py_file)
                if skill is not None:
                    loaded_skills.append(skill)
                    logger.info(
                        "技能 %s v%s 刷新完毕（来源：%s）", skill.name, skill.version, py_file.name
                    )
            except Exception as exc:
                logger.warning("重载SKILL文件失败 (%s): %s", py_file.name, exc)

        self.replace_all(loaded_skills)
        return len(self._skills)

    @staticmethod
    def _load_skill_file(file_path: Path) -> SkillDefinition | None:
        """Load a single skill from a Python file."""
        module_name = f"_sirius_skill_{file_path.stem}"

        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            logger.warning("无法创建模块规格: %s", file_path)
            return None

        module = importlib.util.module_from_spec(spec)
        # Temporarily add to sys.modules so relative imports work if needed
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise RuntimeError(f"执行SKILL模块失败 ({file_path.name}): {exc}") from exc

        meta: dict[str, Any] | None = getattr(module, "SKILL_META", None)
        if not isinstance(meta, dict):
            sys.modules.pop(module_name, None)
            logger.warning("SKILL文件缺少 SKILL_META 字典: %s", file_path.name)
            return None

        run_func = getattr(module, "run", None)
        bg_task_factory = getattr(module, "create_background_tasks", None)
        trigger_factory = getattr(module, "create_triggers", None)
        on_load_factory = getattr(module, "create_on_load", None)
        on_unload_factory = getattr(module, "create_on_unload", None)
        has_active = callable(run_func)
        has_passive = callable(bg_task_factory) or callable(trigger_factory)

        if not has_active and not has_passive:
            sys.modules.pop(module_name, None)
            logger.warning(
                "SKILL文件缺少 run()/create_background_tasks()/create_triggers(): %s",
                file_path.name,
            )
            return None

        name = str(meta.get("name", file_path.stem)).strip()
        description = str(meta.get("description", "")).strip()
        version = str(meta.get("version", "1.0.0")).strip()
        developer_only = bool(meta.get("developer_only", False))
        silent = bool(meta.get("silent", False))
        tags: list[str] = []
        raw_tags = meta.get("tags", [])
        if isinstance(raw_tags, list):
            tags = [str(t).strip() for t in raw_tags if t is not None]
        adapter_types: list[str] = []
        raw_adapter_types = meta.get("adapter_types", [])
        if isinstance(raw_adapter_types, list):
            adapter_types = [str(t).strip() for t in raw_adapter_types if t is not None]
        if not name:
            name = file_path.stem
        if not description:
            logger.warning("SKILL '%s' 缺少描述", name)

        # Parse parameters
        raw_params = meta.get("parameters", {})
        parameters: list[SkillParameter] = []
        if isinstance(raw_params, dict):
            for param_name, param_def in raw_params.items():
                if isinstance(param_def, dict):
                    parameters.append(
                        SkillParameter(
                            name=param_name,
                            type=str(param_def.get("type", "str")),
                            description=str(param_def.get("description", "")),
                            required=bool(param_def.get("required", False)),
                            default=param_def.get("default"),
                            choices=param_def.get("choices"),
                            fields=param_def.get("fields"),
                            group=str(param_def.get("group", "")),
                        )
                    )
        elif isinstance(raw_params, list):
            for item in raw_params:
                if isinstance(item, dict):
                    parameters.append(
                        SkillParameter(
                            name=str(item.get("name", "")),
                            type=str(item.get("type", "str")),
                            description=str(item.get("description", "")),
                            required=bool(item.get("required", False)),
                            default=item.get("default"),
                            choices=item.get("choices"),
                            fields=item.get("fields"),
                            group=str(item.get("group", "")),
                        )
                    )

        return SkillDefinition(
            name=name,
            description=description,
            parameters=parameters,
            version=version,
            developer_only=developer_only,
            silent=silent,
            tags=tags,
            adapter_types=adapter_types,
            source_path=file_path,
            _run_func=run_func if has_active else None,
            _background_task_factory=bg_task_factory if callable(bg_task_factory) else None,
            _trigger_factory=trigger_factory if callable(trigger_factory) else None,
            _on_load_factory=on_load_factory if callable(on_load_factory) else None,
            _on_unload_factory=on_unload_factory if callable(on_unload_factory) else None,
        )

    def build_tool_descriptions(
        self,
        *,
        invocation_context: SkillInvocationContext | None = None,
        compact: bool = False,
        adapter_type: str | None = None,
    ) -> str:
        """Build a formatted text block describing all available skills.

        This is injected into the system prompt so the AI knows what tools
        are available and how to call them.

        Args:
            invocation_context: Optional context for developer-only filtering.
            compact: If True, use a condensed one-line-per-skill format to
                save tokens when many skills are registered.
            adapter_type: If provided, only include skills whose adapter_types
                is empty or contains this adapter type.
        """
        if not self._skills:
            return ""

        lines: list[str] = []
        for skill in self._skills.values():
            # Passive-only skills (has factories but no run func) are not callable by the model
            if skill._run_func is None and skill.is_passive:
                continue
            if (
                skill.developer_only
                and invocation_context is not None
                and not invocation_context.caller_is_developer
            ):
                continue

            # Adapter filtering: skip skills that are locked to other adapters
            if skill.adapter_types and adapter_type is not None:
                if adapter_type not in skill.adapter_types:
                    continue

            security_note = "（仅 developer 可调用）" if skill.developer_only else ""
            if compact:
                param_sig = _build_compact_param_signature(skill.parameters)
                sig = f"{skill.name}{param_sig}" if param_sig else skill.name
                lines.append(f"- {sig}: {skill.description}{security_note}")
            else:
                lines.append(f"- {skill.name}: {skill.description}{security_note}")
                if skill.parameters:
                    param_parts: list[str] = []
                    for p in skill.parameters:
                        required_tag = "必填" if p.required else "可选"
                        default_tag = (
                            f", 默认={p.default}"
                            if not p.required and p.default is not None
                            else ""
                        )
                        param_parts.append(
                            f"    - {p.name} ({p.type}, {required_tag}{default_tag}): {p.description}"
                        )
                    lines.extend(param_parts)
        return "\n".join(lines)

    @staticmethod
    def _install_skill_dependencies(py_file: Path, *, auto_install_deps: bool) -> None:
        if not auto_install_deps:
            resolve_skill_dependencies(py_file, auto_install=False)
            return

        installed = resolve_skill_dependencies(py_file, auto_install=True)
        if installed:
            logger.info("顺手帮 '%s' 把依赖 %s 装好啦", py_file.stem, ", ".join(installed))


def _build_compact_param_signature(parameters: list[SkillParameter]) -> str:
    """Build a compact `(name:type=default desc, ...)` signature string.

    Keeps parameter descriptions so the model still understands semantics,
    while dropping redundant Chinese labels like "必填/可选/默认=".
    """
    if not parameters:
        return ""
    parts: list[str] = []
    for p in parameters:
        piece = f"{p.name}:{p.type}"
        if not p.required and p.default is not None:
            piece += f"={p.default}"
        if p.description:
            piece += f" {p.description.strip()}"
        parts.append(piece)
    return f"({', '.join(parts)})"
