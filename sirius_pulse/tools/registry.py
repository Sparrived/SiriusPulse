"""Tool registry — discovers, loads, and manages tool definitions.

Tools are Python files residing in {work_path}/tools/ that expose:
- TOOL_META: dict with name, description, parameters, version (optional)
- run(**kwargs) -> Any: the callable entry point
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Collection

from sirius_pulse.tools.dependency_resolver import resolve_tool_dependencies
from sirius_pulse.tools.models import (
    ToolDefinition,
    ToolInvocationContext,
    ToolParameter,
    ToolPassiveType,
    ToolSideEffect,
)
from sirius_pulse.tools.security import developer_gate_applies

logger = logging.getLogger(__name__)

_TOOLS_README = """# tools 目录说明

此目录用于存放 Sirius Chat 在当前 work_path 下可自动发现的外部 TOOL 文件。

- 每个 TOOL 使用单独的 Python 文件。
- 文件需导出 TOOL_META 字典和 run() 函数。
- 文件名建议使用英文、数字、下划线，避免以下划线开头。
- 当会话启用 TOOL 系统时，框架会自动扫描此目录。

最小示例：

```python
TOOL_META = {
    "name": "hello_tool",
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


class ToolRegistry:
    """Discovers and manages tool definitions from a directory."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def all_tools(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def passive_tools(self) -> list[ToolDefinition]:
        """Return all passive tools (those with background tasks or triggers)."""
        return [s for s in self._tools.values() if s.is_passive]

    def passive_tools_by_type(self, passive_type: ToolPassiveType) -> list[ToolDefinition]:
        """Return passive tools matching the given type."""
        return [s for s in self._tools.values() if s.passive_type == passive_type]

    def register(self, tool: ToolDefinition) -> None:
        """Manually register a tool definition."""
        self._tools[tool.name] = tool

    def replace_all(self, tools: list[ToolDefinition]) -> None:
        """Replace the whole registry atomically."""
        self._tools = {tool.name: tool for tool in tools}

    @staticmethod
    def ensure_tools_directory(tools_dir: Path) -> None:
        """Ensure the tools directory and its README bootstrap file exist."""
        tools_dir.mkdir(parents=True, exist_ok=True)
        readme_path = tools_dir / "README.md"
        if not readme_path.exists():
            readme_path.write_text(_TOOLS_README, encoding="utf-8")

    @staticmethod
    def builtin_tools_dir() -> Path:
        """Return the package directory containing built-in tools."""
        return Path(__file__).resolve().parent / "builtin"

    def _load_builtin_tools(self, *, auto_install_deps: bool) -> int:
        """Load package-provided built-in tools into the registry."""
        loaded = 0
        builtin_dir = self.builtin_tools_dir()
        if not builtin_dir.exists():
            return loaded
        for py_file in sorted(builtin_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                self._install_tool_dependencies(py_file, auto_install_deps=auto_install_deps)
                tool = self._load_tool_file(py_file)
                if tool is not None:
                    self._tools[tool.name] = tool
                    loaded += 1
            except Exception as exc:
                logger.warning("加载内置TOOL失败 (%s): %s", py_file.name, exc)
        return loaded

    def load_from_directory(
        self,
        tools_dir: Path,
        *,
        auto_install_deps: bool = True,
        include_builtin: bool = False,
    ) -> int:
        """Load all *.py tool files from a directory.

        Args:
            tools_dir: Directory containing TOOL Python files.
            auto_install_deps: If True, automatically install missing
                third-party dependencies declared in TOOL_META or
                detected from import statements (uses ``uv`` / ``pip``).
            include_builtin: If True, pre-load package-provided built-in tools
                before scanning the workspace directory. Workspace tools with the
                same name override built-ins.

        Returns the number of tools successfully loaded.
        """
        self.ensure_tools_directory(tools_dir)

        baseline = len(self._tools)
        if include_builtin:
            self._load_builtin_tools(auto_install_deps=auto_install_deps)

        for py_file in sorted(tools_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                self._install_tool_dependencies(py_file, auto_install_deps=auto_install_deps)

                tool = self._load_tool_file(py_file)
                if tool is not None:
                    self._tools[tool.name] = tool
                    logger.info(
                        "新工具到手！%s v%s（从 %s 学来的）",
                        tool.name,
                        tool.version,
                        py_file.name,
                    )
            except Exception as exc:
                logger.warning("加载TOOL文件失败 (%s): %s", py_file.name, exc)
        return max(0, len(self._tools) - baseline)

    def reload_from_directory(
        self,
        tools_dir: Path,
        *,
        auto_install_deps: bool = True,
        include_builtin: bool = False,
    ) -> int:
        """Reload all tool files from a directory, replacing removed entries too."""
        self.ensure_tools_directory(tools_dir)

        loaded_tools: list[ToolDefinition] = []
        if include_builtin:
            builtin_dir = self.builtin_tools_dir()
            if builtin_dir.exists():
                for py_file in sorted(builtin_dir.glob("*.py")):
                    if py_file.name.startswith("_"):
                        continue
                    try:
                        self._install_tool_dependencies(
                            py_file, auto_install_deps=auto_install_deps
                        )
                        tool = self._load_tool_file(py_file)
                        if tool is not None:
                            loaded_tools.append(tool)
                            logger.info(
                                "内置工具 %s v%s 已重新加载（来源：%s）",
                                tool.name,
                                tool.version,
                                py_file.name,
                            )
                    except Exception as exc:
                        logger.warning("重载内置TOOL失败 (%s): %s", py_file.name, exc)

        for py_file in sorted(tools_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                self._install_tool_dependencies(py_file, auto_install_deps=auto_install_deps)

                tool = self._load_tool_file(py_file)
                if tool is not None:
                    loaded_tools.append(tool)
                    logger.info("工具 %s v%s 刷新完毕（来源：%s）", tool.name, tool.version, py_file.name)
            except Exception as exc:
                logger.warning("重载TOOL文件失败 (%s): %s", py_file.name, exc)

        self.replace_all(loaded_tools)
        return len(self._tools)

    @staticmethod
    def _load_tool_file(file_path: Path) -> ToolDefinition | None:
        """Load a single tool from a Python file."""
        module_name = f"_sirius_tool_{file_path.stem}"

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
            raise RuntimeError(f"执行TOOL模块失败 ({file_path.name}): {exc}") from exc

        meta: dict[str, Any] | None = getattr(module, "TOOL_META", None)
        if not isinstance(meta, dict):
            sys.modules.pop(module_name, None)
            logger.warning("TOOL文件缺少 TOOL_META 字典: %s", file_path.name)
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
                "TOOL文件缺少 run()/create_background_tasks()/create_triggers(): %s",
                file_path.name,
            )
            return None

        name = str(meta.get("name", file_path.stem)).strip()
        description = str(meta.get("description", "")).strip()
        version = str(meta.get("version", "1.0.0")).strip()
        developer_only = bool(meta.get("developer_only", False))
        admin_required = bool(meta.get("admin_required", False))
        silent = bool(meta.get("silent", False))
        retry_safe = bool(meta.get("retry_safe", False))
        raw_side_effect = str(meta.get("side_effect", "unknown")).strip().lower()
        try:
            side_effect = ToolSideEffect(raw_side_effect)
        except ValueError:
            side_effect = ToolSideEffect.UNKNOWN
        model_visible = bool(meta.get("model_visible", True))
        allowed_when_self_initiated = bool(meta.get("allowed_when_self_initiated", False))
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
            logger.warning("TOOL '%s' 缺少描述", name)

        parameters = ToolRegistry._parse_parameters(meta.get("parameters", {}))
        config_parameters = ToolRegistry._parse_parameters(meta.get("config", {}))

        return ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            config_parameters=config_parameters,
            version=version,
            developer_only=developer_only,
            admin_required=admin_required,
            silent=silent,
            retry_safe=retry_safe,
            side_effect=side_effect,
            model_visible=model_visible,
            allowed_when_self_initiated=allowed_when_self_initiated,
            tags=tags,
            adapter_types=adapter_types,
            source_path=file_path,
            _run_func=run_func if has_active else None,
            _background_task_factory=bg_task_factory if callable(bg_task_factory) else None,
            _trigger_factory=trigger_factory if callable(trigger_factory) else None,
            _on_load_factory=on_load_factory if callable(on_load_factory) else None,
            _on_unload_factory=on_unload_factory if callable(on_unload_factory) else None,
        )

    @staticmethod
    def _parse_parameters(raw_params: Any) -> list[ToolParameter]:
        """Parse model parameters or private per-persona config parameters."""
        parameters: list[ToolParameter] = []
        items = raw_params.items() if isinstance(raw_params, dict) else enumerate(raw_params or [])
        for key, raw in items:
            if not isinstance(raw, dict):
                continue
            name = str(key) if isinstance(raw_params, dict) else str(raw.get("name", ""))
            if not name:
                continue
            parameters.append(
                ToolParameter(
                    name=name,
                    type=str(raw.get("type", "str")),
                    description=str(raw.get("description", "")),
                    required=bool(raw.get("required", False)),
                    default=raw.get("default"),
                    choices=raw.get("choices"),
                    fields=raw.get("fields"),
                    group=str(raw.get("group", "")),
                )
            )
        return parameters

    def build_tools_list(
        self,
        *,
        invocation_context: ToolInvocationContext | None = None,
        adapter_type: str | None = None,
        chat_type: str | None = None,
        admin_allowed: bool = False,
        exclude_names: Collection[str] | None = None,
    ) -> list[dict[str, Any]]:
        """构建 OpenAI tools 列表，用于原生 function_call。

        Args:
            invocation_context: 可选的调用上下文，用于 developer_only 过滤。
            adapter_type: 如果提供，只包含 adapter_types 为空或包含此类型的工具。
            exclude_names: 本次调用要隐藏的工具名（如工作模式外的重工具）。

        Returns:
            OpenAI tools 格式的列表。
        """
        return [
            tool.to_tool_schema()
            for tool in self._available_model_tools(
                invocation_context=invocation_context,
                adapter_type=adapter_type,
                chat_type=chat_type,
                admin_allowed=admin_allowed,
                exclude_names=exclude_names,
            )
        ]

    def build_tool_descriptions(
        self,
        *,
        invocation_context: ToolInvocationContext | None = None,
        compact: bool = False,
        adapter_type: str | None = None,
        chat_type: str | None = None,
        admin_allowed: bool = False,
        exclude_names: Collection[str] | None = None,
    ) -> str:
        """Build a formatted text block describing all available tools.

        This is injected into the system prompt so the AI knows what tools
        are available and how to call them.

        Args:
            invocation_context: Optional context for developer-only filtering.
            compact: If True, use a condensed one-line-per-tool format to
                save tokens when many tools are registered.
            adapter_type: If provided, only include tools whose adapter_types
                is empty or contains this adapter type.
            exclude_names: Tool names to leave out of the description block.
        """
        if not self._tools:
            return ""

        lines: list[str] = []
        for tool in self._available_model_tools(
            invocation_context=invocation_context,
            adapter_type=adapter_type,
            chat_type=chat_type,
            admin_allowed=admin_allowed,
            exclude_names=exclude_names,
        ):
            security_notes: list[str] = []
            if tool.developer_only:
                security_notes.append("仅 developer 可调用")
            if tool.admin_required:
                security_notes.append("需要 Bot 是当前群管理员")
            security_note = f"（{'，'.join(security_notes)}）" if security_notes else ""
            if compact:
                param_sig = _build_compact_param_signature(tool.parameters)
                sig = f"{tool.name}{param_sig}" if param_sig else tool.name
                lines.append(f"- {sig}: {tool.description}{security_note}")
            else:
                lines.append(f"- {tool.name}: {tool.description}{security_note}")
                if tool.parameters:
                    param_parts: list[str] = []
                    for p in tool.parameters:
                        required_tag = "必填" if p.required else "可选"
                        default_tag = (
                            f", 默认={p.default}" if not p.required and p.default is not None else ""
                        )
                        param_parts.append(
                            f"    - {p.name} ({p.type}, {required_tag}{default_tag}): {p.description}"
                        )
                    lines.extend(param_parts)
        return "\n".join(lines)

    def _available_model_tools(
        self,
        *,
        invocation_context: ToolInvocationContext | None,
        adapter_type: str | None,
        chat_type: str | None,
        admin_allowed: bool,
        exclude_names: Collection[str] | None = None,
    ) -> list[ToolDefinition]:
        """Return model-callable tools after applying the shared access filters."""
        hidden = set(exclude_names or ())
        tools: list[ToolDefinition] = []
        for tool in self._tools.values():
            if not tool.model_visible:
                continue
            if tool.name in hidden:
                continue
            if tool._run_func is None and tool.is_passive:
                continue
            if (
                tool.developer_only
                and invocation_context is not None
                and developer_gate_applies(invocation_context)
                and not invocation_context.caller_is_developer
            ):
                continue
            if tool.adapter_types and adapter_type not in tool.adapter_types:
                continue
            if tool.admin_required and not (chat_type == "group" and admin_allowed):
                continue
            tools.append(tool)
        return tools

    @staticmethod
    def _install_tool_dependencies(py_file: Path, *, auto_install_deps: bool) -> None:
        if not auto_install_deps:
            resolve_tool_dependencies(py_file, auto_install=False)
            return

        installed = resolve_tool_dependencies(py_file, auto_install=True)
        if installed:
            logger.info("顺手帮 '%s' 把依赖 %s 装好啦", py_file.stem, ", ".join(installed))


def _build_compact_param_signature(parameters: list[ToolParameter]) -> str:
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
