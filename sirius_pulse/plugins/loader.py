"""Plugin 加载器 —— 扫描插件目录、导入 Python 模块。

负责：
    1. 扫描 plugins/ 目录下的文件夹级插件包
    2. 自动安装插件的 pip/uv 依赖
    3. 动态导入 .py 文件中的 PluginBase 子类
    4. 从类属性 + @command 构建 PluginDefinition
    5. 处理加载错误并记录日志
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any

from sirius_pulse.plugins.models import PluginDefinition, normalize_plugin_ui_schema

logger = logging.getLogger(__name__)

# The project currently publishes stable semantic versions.  Keep comparison
# dependency-free so PluginLoader remains usable in minimal installations.
_VERSION_PREFIX = re.compile(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[+\-].*)?\s*$")
_MAX_PLUGIN_METADATA_SOURCE_BYTES = 4 * 1024 * 1024
_MAX_PLUGIN_MANIFEST_BYTES = 1024 * 1024


def _numeric_version(value: object) -> tuple[int, int, int] | None:
    """Return the comparable numeric portion of a semantic version."""
    if not isinstance(value, str):
        return None
    match = _VERSION_PREFIX.match(value)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor or 0), int(patch or 0)


def _installed_framework_version() -> str | None:
    """Read the installed distribution version without importing plugin code."""
    try:
        return distribution_version("sirius-pulse")
    except PackageNotFoundError:
        # Source-tree development can intentionally run without an installed
        # distribution.  Do not make that mode claim a fictitious version.
        return None


def _literal_dict_has_duplicate_key(node: ast.AST) -> bool:
    """Return whether any literal dictionary below *node* repeats a key."""
    for child in ast.walk(node):
        if not isinstance(child, ast.Dict):
            continue
        seen: set[Any] = set()
        for key_node in child.keys:
            if key_node is None:
                continue
            try:
                key = ast.literal_eval(key_node)
                hash(key)
                if key in seen:
                    return True
                seen.add(key)
            except (TypeError, ValueError, SyntaxError, MemoryError):
                # ``ast.literal_eval`` below remains the authority for whether
                # the complete declaration is static. This helper only closes
                # Python's otherwise silent duplicate-key overwrite behavior.
                continue
    return False


def _json_object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting ambiguous duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 对象包含重复字段：{key}")
        result[key] = value
    return result


class PluginLoadError(Exception):
    """Plugin 加载错误。"""

    def __init__(self, plugin_path: Path, reason: str) -> None:
        self.plugin_path = plugin_path
        self.reason = reason
        super().__init__(f"加载 Plugin 失败 [{plugin_path.name}]: {reason}")


class PluginLoader:
    """Plugin 加载器。

    扫描目录中任意 .py 文件，导入后寻找 PluginBase 子类。
    """

    def __init__(self, plugins_dir: Path) -> None:
        self._plugins_dir = plugins_dir

    @property
    def plugins_dir(self) -> Path:
        return self._plugins_dir

    @staticmethod
    def _python_files(plugin_path: Path) -> list[Path]:
        """Return regular in-directory Python files and reject link escapes."""
        root = plugin_path.resolve()
        files: list[Path] = []
        for py_file in plugin_path.glob("*.py"):
            try:
                resolved = py_file.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError) as exc:
                raise PluginLoadError(
                    plugin_path,
                    f"Python 源码必须位于插件目录内: {py_file.name}",
                ) from exc
            if py_file.is_symlink() or not resolved.is_file():
                raise PluginLoadError(
                    plugin_path,
                    f"不允许符号链接或非普通 Python 源码: {py_file.name}",
                )
            files.append(py_file)
        return files

    @staticmethod
    def _safe_manifest_path(plugin_path: Path) -> Path:
        """Return a regular in-directory plugin.json path."""
        config_file = plugin_path / "plugin.json"
        try:
            resolved = config_file.resolve(strict=True)
            resolved.relative_to(plugin_path.resolve())
        except (OSError, ValueError) as exc:
            raise PluginLoadError(plugin_path, "plugin.json 必须位于插件目录内") from exc
        if config_file.is_symlink() or not resolved.is_file():
            raise PluginLoadError(plugin_path, "plugin.json 不得是符号链接或非普通文件")
        return config_file

    @staticmethod
    def _validate_framework_compatibility(
        definition: PluginDefinition,
        plugin_path: Path,
    ) -> None:
        """Reject a Plugin that requires a newer framework release.

        The source-tree fallback intentionally logs and permits loading when no
        package metadata exists.  Released wheels always have distribution
        metadata, so an unsupported Plugin cannot silently register there.
        """
        required_text = definition.min_framework_version
        required = _numeric_version(required_text)
        if required is None:
            raise PluginLoadError(plugin_path, "min_framework_version 必须是语义版本号")

        current_text = _installed_framework_version()
        if current_text is None:
            logger.warning(
                "无法读取 sirius-pulse 已安装版本，开发模式跳过 Plugin 兼容性校验: %s",
                plugin_path.name,
            )
            return
        current = _numeric_version(current_text)
        if current is None:
            raise PluginLoadError(plugin_path, "当前 Sirius Pulse 版本格式无效")
        if current < required:
            raise PluginLoadError(
                plugin_path,
                "需要 Sirius Pulse >= %s，当前为 %s" % (required_text, current_text),
            )

    def discover(self) -> list[Path]:
        """扫描 plugins/ 目录，发现所有有效插件文件夹。

        Returns:
            插件文件夹路径列表
        """
        if not self._plugins_dir.exists():
            logger.info("插件目录不存在: %s", self._plugins_dir)
            return []

        discovered: list[Path] = []
        root = self._plugins_dir.resolve()
        for entry in sorted(self._plugins_dir.iterdir()):
            if entry.is_symlink():
                logger.warning("跳过符号链接 Plugin 入口: %s", entry)
                continue
            if not entry.is_dir():
                continue
            try:
                entry.resolve().relative_to(root)
            except ValueError:
                logger.warning("跳过 plugins 根目录外的入口: %s", entry)
                continue
            if entry.name.startswith("_") or entry.name.startswith("."):
                continue
            # 插件仓库可以携带 tests/docs/examples，这些目录不是插件入口。
            if entry.name.casefold() in {"test", "tests", "__tests__", "docs", "examples"}:
                continue
            # 仅接受插件目录内的普通 .py 文件；源码符号链接会让导入或
            # 元数据扫描越过 plugins 信任边界，因此整个入口都拒绝。
            try:
                has_py = bool(self._python_files(entry))
            except PluginLoadError as exc:
                logger.warning("%s", exc)
                continue
            if has_py:
                discovered.append(entry)
            else:
                logger.debug("跳过非插件目录: %s（无 .py 文件）", entry.name)

        logger.info("发现 %d 个插件目录", len(discovered))
        return discovered

    def load_all_definitions(
        self,
        *,
        install_dependencies: bool = False,
        metadata_only: bool = False,
    ) -> list[PluginDefinition]:
        """Discover Plugin definitions.

        ``metadata_only=True`` reads only ``plugin.json`` and literal Python AST
        metadata; it never imports Plugin code or executes a package installer.
        WebUI/listing paths must use this mode.  Dependency installation is an
        explicit trusted lifecycle action and is intentionally rejected for
        metadata-only discovery.
        """
        if metadata_only and install_dependencies:
            raise ValueError("metadata-only Plugin discovery cannot install dependencies")

        definitions: list[PluginDefinition] = []
        for plugin_path in self.discover():
            try:
                definition = (
                    self.load_metadata_definition(plugin_path)
                    if metadata_only
                    else self.load_definition(plugin_path)
                )
                if definition is None:
                    continue
                if install_dependencies and definition.dependencies:
                    logger.info(
                        "插件 [%s] 需要依赖: %s",
                        plugin_path.name,
                        ", ".join(definition.dependencies),
                    )
                    _ok, fail = self.install_dependencies(definition.dependencies)
                    if fail > 0:
                        logger.warning("插件 [%s] 有 %d 个依赖安装失败", plugin_path.name, fail)
                definitions.append(definition)
                logger.info("加载插件元数据: %s v%s", definition.name, definition.version)
            except PluginLoadError as exc:
                logger.error("%s", exc)
            except Exception as exc:
                logger.error("加载插件失败 [%s]: %s", plugin_path.name, exc)

        return definitions

    def load_metadata_definition(self, plugin_path: Path) -> PluginDefinition | None:
        """Load non-executing metadata for listing and enablement decisions."""
        config_file = plugin_path / "plugin.json"
        definition: PluginDefinition | None
        if config_file.exists() or config_file.is_symlink():
            self._safe_manifest_path(plugin_path)
            definition = self._load_definition_from_json(plugin_path)
            ast_definition = self._load_definition_from_ast(plugin_path)
            if ast_definition is not None:
                # plugin.json is authoritative metadata, while literal class
                # settings fill optional UI schema omitted by older manifests.
                if not definition.commands:
                    definition.commands = ast_definition.commands
                if not definition.command_groups:
                    definition.command_groups = ast_definition.command_groups
                if not definition.parameters:
                    definition.parameters = ast_definition.parameters
                if not definition.dependencies:
                    definition.dependencies = ast_definition.dependencies
                if not definition.events:
                    definition.events = ast_definition.events
                if not definition.prompt_inject:
                    definition.prompt_inject = ast_definition.prompt_inject
                if not definition.ui_schema:
                    definition.ui_schema = ast_definition.ui_schema
                try:
                    definition.seal_parameter_contract()
                    definition.ui_schema = normalize_plugin_ui_schema(
                        definition.ui_schema,
                        definition.parameters,
                    )
                except (TypeError, ValueError, OverflowError, RecursionError) as exc:
                    logger.warning(
                        "插件 [%s] 的合并 ui_schema 无效，回退通用表单: %s",
                        plugin_path.name,
                        exc,
                    )
                    definition.ui_schema = {}
        else:
            definition = self._load_definition_from_ast(plugin_path)
        if definition is not None:
            self._validate_framework_compatibility(definition, plugin_path)
        return definition

    @staticmethod
    def _commands_from_ast_class(
        plugin_path: Path,
        class_node: ast.ClassDef,
    ) -> list[dict[str, Any]]:
        """Extract literal ``@command`` declarations without executing decorators."""
        commands: list[dict[str, Any]] = []
        for child in class_node.body:
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in child.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                decorator_name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else ""
                )
                if decorator_name != "command":
                    continue
                if not decorator.args:
                    raise PluginLoadError(plugin_path, "@command 必须声明字面量命令名")
                if _literal_dict_has_duplicate_key(decorator):
                    raise PluginLoadError(plugin_path, "@command 元数据包含重复字段")
                try:
                    command_name = ast.literal_eval(decorator.args[0])
                    keyword_values = {
                        keyword.arg: ast.literal_eval(keyword.value)
                        for keyword in decorator.keywords
                        if keyword.arg is not None
                    }
                except (ValueError, TypeError, SyntaxError, MemoryError) as exc:
                    raise PluginLoadError(
                        plugin_path,
                        "@command 元数据必须使用字面量",
                    ) from exc
                if not isinstance(command_name, str) or not command_name:
                    raise PluginLoadError(plugin_path, "@command 命令名必须是非空字符串")
                prefix = keyword_values.get("prefix", "")
                patterns = keyword_values.get("patterns") or [command_name]
                if (
                    not isinstance(prefix, str)
                    or not isinstance(patterns, list)
                    or any(not isinstance(pattern, str) for pattern in patterns)
                ):
                    raise PluginLoadError(plugin_path, "@command prefix/patterns 声明无效")
                if any(not pattern.strip() for pattern in patterns):
                    raise PluginLoadError(plugin_path, "@command pattern 不能为空或仅包含空白")
                commands.append(
                    {
                        "name": command_name,
                        "patterns": [f"{prefix}{pattern}" for pattern in patterns],
                        "pattern_type": keyword_values.get("pattern_type", "prefix"),
                        "description": keyword_values.get("description", ""),
                        "examples": keyword_values.get("examples", []),
                        "hidden_from_intent": keyword_values.get(
                            "hidden_from_intent",
                            False,
                        ),
                    }
                )
        return commands

    def _load_definition_from_ast(self, plugin_path: Path) -> PluginDefinition | None:
        """Read literal ``_plugin_*`` class/decorator metadata without importing code."""
        supported = {
            "_plugin_name": "name",
            "_plugin_display_name": "display_name",
            "_plugin_description": "description",
            "_plugin_version": "version",
            "_plugin_author": "author",
            "_plugin_min_framework_version": "min_framework_version",
            "_plugin_dependencies": "dependencies",
            "_plugin_prompt_inject": "prompt_inject",
            "_plugin_parameters": "parameters",
            "_plugin_ui_schema": "ui_schema",
            "_plugin_permissions": "permissions",
            "_plugin_events": "events",
        }
        metadata: dict[str, Any] = {}
        source_found = False
        for py_file in sorted(
            self._python_files(plugin_path),
            key=lambda path: (path.name != "__init__.py", path.name),
        ):
            if py_file.name.startswith("_") and py_file.name != "__init__.py":
                continue
            try:
                if py_file.stat().st_size > _MAX_PLUGIN_METADATA_SOURCE_BYTES:
                    raise PluginLoadError(
                        plugin_path,
                        f"Plugin 元数据源码超过 4 MiB 安全上限: {py_file.name}",
                    )
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except PluginLoadError:
                raise
            except (OSError, UnicodeError, SyntaxError) as exc:
                raise PluginLoadError(
                    plugin_path, f"无法解析 Plugin 元数据: {type(exc).__name__}"
                ) from exc
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                class_values: dict[str, Any] = {}
                declared_targets: set[str] = set()
                for child in node.body:
                    target_name = ""
                    value_node: ast.expr | None = None
                    if isinstance(child, ast.Assign) and len(child.targets) == 1:
                        target = child.targets[0]
                        if isinstance(target, ast.Name):
                            target_name = target.id
                            value_node = child.value
                    elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                        target_name = child.target.id
                        value_node = child.value
                    if target_name not in supported or value_node is None:
                        continue
                    if target_name in declared_targets:
                        raise PluginLoadError(
                            plugin_path,
                            f"Plugin 元数据 {target_name} 在同一类中重复声明",
                        )
                    declared_targets.add(target_name)
                    if _literal_dict_has_duplicate_key(value_node):
                        raise PluginLoadError(
                            plugin_path,
                            f"Plugin 元数据 {target_name} 包含重复字段",
                        )
                    try:
                        class_values[supported[target_name]] = ast.literal_eval(value_node)
                    except (ValueError, TypeError, SyntaxError, MemoryError) as exc:
                        raise PluginLoadError(
                            plugin_path,
                            f"Plugin 元数据 {target_name} 必须是字面量",
                        ) from exc
                if class_values.get("name"):
                    class_values["commands"] = self._commands_from_ast_class(
                        plugin_path,
                        node,
                    )
                    metadata.update(class_values)
                    source_found = True
                    break
            if source_found:
                break
        if not source_found:
            logger.warning("插件 %s 中未找到可安全读取的 Plugin 元数据", plugin_path.name)
            return None
        dependencies = metadata.get("dependencies", [])
        if not isinstance(dependencies, list) or any(
            not isinstance(item, str) for item in dependencies
        ):
            raise PluginLoadError(plugin_path, "_plugin_dependencies 必须是字符串列表")
        data: dict[str, Any] = {
            "name": str(metadata.get("name", "")),
            "display_name": str(metadata.get("display_name", "")),
            "description": str(metadata.get("description", "")),
            "version": str(metadata.get("version", "1.0.0")),
            "author": str(metadata.get("author", "")),
            "min_framework_version": str(metadata.get("min_framework_version", "1.2.0")),
            "dependencies": list(dependencies),
            "prompt_inject": str(metadata.get("prompt_inject", "")),
        }
        raw_parameters = metadata.get("parameters")
        if isinstance(raw_parameters, list):
            parameter_map: dict[str, dict[str, Any]] = {}
            for raw_parameter in raw_parameters:
                if not isinstance(raw_parameter, dict):
                    raise PluginLoadError(plugin_path, "_plugin_parameters 必须是对象列表")
                parameter_name = raw_parameter.get("name")
                if not isinstance(parameter_name, str) or not parameter_name.strip():
                    raise PluginLoadError(plugin_path, "Plugin 参数缺少有效名称")
                if parameter_name in parameter_map:
                    raise PluginLoadError(
                        plugin_path,
                        f"Plugin 参数声明包含重复名称：{parameter_name}",
                    )
                parameter_map[parameter_name] = {
                    key: value for key, value in raw_parameter.items() if key != "name"
                }
            data["parameters"] = parameter_map
        elif raw_parameters is not None:
            raise PluginLoadError(plugin_path, "_plugin_parameters 必须是对象列表")
        if "ui_schema" in metadata:
            data["ui_schema"] = metadata["ui_schema"]
        if "permissions" in metadata:
            data["permissions"] = metadata["permissions"]
        commands = metadata.get("commands", [])
        events = metadata.get("events", [])
        if commands or events:
            data["triggers"] = {
                "commands": commands,
                "events": events,
            }
        try:
            return PluginDefinition.from_dict(data, source_path=plugin_path)
        except (AttributeError, TypeError, ValueError) as exc:
            raise PluginLoadError(plugin_path, "Plugin 字面量元数据结构无效") from exc

    def load_definition(self, plugin_path: Path) -> PluginDefinition | None:
        """加载插件的 PluginDefinition。

        优先从 Python 类的类属性 + @command 构建。
        兼容旧的 plugin.json。

        Args:
            plugin_path: 插件文件夹路径

        Returns:
            PluginDefinition 实例或 None
        """
        # Validate static metadata before importing executable Plugin code.
        # An incompatible package must not execute merely so the host can learn
        # that it requires a newer framework version.
        metadata = self.load_metadata_definition(plugin_path)
        if metadata is None:
            return None

        plugin_cls = self.import_plugin_class(plugin_path)
        if plugin_cls is not None:
            definition = PluginDefinition.from_class(plugin_cls, source_path=plugin_path)
            self._validate_framework_compatibility(definition, plugin_path)
            definition._plugin_class = plugin_cls
            return definition

        # Legacy manifests remain listable even when no executable class is
        # available; runtime registration rejects that case explicitly.
        return metadata

    def _load_definition_from_json(self, plugin_path: Path) -> PluginDefinition:
        """从 plugin.json 加载定义（兼容旧格式）。"""
        config_file = self._safe_manifest_path(plugin_path)
        try:
            if config_file.stat().st_size > _MAX_PLUGIN_MANIFEST_BYTES:
                raise PluginLoadError(plugin_path, "plugin.json 超过 1 MiB 安全上限")
            raw_text = config_file.read_text(encoding="utf-8")
            data = json.loads(
                raw_text,
                object_pairs_hook=_json_object_without_duplicate_keys,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise PluginLoadError(plugin_path, f"plugin.json 格式错误: {exc}") from exc

        if not isinstance(data, dict):
            raise PluginLoadError(plugin_path, "plugin.json 必须是 JSON 对象")

        try:
            return PluginDefinition.from_dict(data, source_path=plugin_path)
        except (AttributeError, TypeError, ValueError) as exc:
            raise PluginLoadError(plugin_path, "Plugin 元数据结构无效") from exc

    def import_plugin_class(self, plugin_path: Path) -> type | None:
        """从插件目录的 .py 文件中导入 PluginBase 子类。

        扫描所有 .py 文件，优先从 __init__.py 寻找 PluginBase 子类。
        以 _ 开头的非 __init__.py 文件视为私有辅助模块，跳过不导入。

        Args:
            plugin_path: 插件文件夹路径

        Returns:
            PluginBase 子类，找不到则返回 None
        """
        # __init__.py 排在首位，其余按字母序；拒绝指向目录外的源码链接。
        py_files = sorted(
            self._python_files(plugin_path),
            key=lambda path: (path.name != "__init__.py", path.name),
        )

        for py_file in py_files:
            # 跳过 _private.py 等辅助文件，但保留 __init__.py 作为合法入口
            if py_file.name.startswith("_") and py_file.name != "__init__.py":
                continue
            cls = self._try_import_class(py_file)
            if cls is not None:
                return cls

        logger.warning("插件 %s 中未找到 PluginBase 子类", plugin_path.name)
        return None

    def _try_import_class(self, py_file: Path) -> type | None:
        """从单个 .py 文件导入，查找 PluginBase 子类。

        自动设置 __package__ 以支持插件内部的相对导入（from .xxx import ...）。
        """
        from sirius_pulse.plugins.base import PluginBase

        package_name = f"_plugin_pkg_{py_file.parent.name}"
        is_package = py_file.name == "__init__.py"
        module_name = package_name if is_package else f"{package_name}.{py_file.stem}"

        try:
            if is_package:
                for loaded_name in list(sys.modules):
                    if loaded_name == package_name or loaded_name.startswith(f"{package_name}."):
                        sys.modules.pop(loaded_name, None)
            spec = importlib.util.spec_from_file_location(
                module_name,
                py_file,
                submodule_search_locations=[str(py_file.parent)] if is_package else None,
            )
            if spec is None or spec.loader is None:
                return None

            if not is_package and package_name not in sys.modules:
                pkg = type(sys)(package_name)
                pkg.__path__ = [str(py_file.parent)]  # type: ignore[attr-defined]
                pkg.__package__ = package_name
                sys.modules[package_name] = pkg

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            logger.debug("导入 %s 失败: %s", py_file.name, exc)
            return None

        for attr_name in dir(module):
            attr = getattr(module, attr_name, None)
            if isinstance(attr, type) and issubclass(attr, PluginBase) and attr is not PluginBase:
                return attr

        return None

    # ── 依赖安装 ──

    @staticmethod
    def install_dependencies(dependencies: list[str]) -> tuple[int, int]:
        """自动安装插件依赖，优先使用 uv pip install，回退到 pip install。

        特殊处理：
            - playwright: 安装后自动执行 playwright install chromium

        Returns:
            (成功数, 失败数)
        """
        if not dependencies:
            return 0, 0

        success_count = 0
        fail_count = 0

        # 检测 uv 是否可用
        uv_available = False
        try:
            result = subprocess.run(
                ["uv", "--version"],
                capture_output=True,
                text=True,
                timeout=5.0,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            uv_available = result.returncode == 0
        except Exception:
            logger.warning("检查 uv 是否可用失败", exc_info=True)
            pass

        needs_chromium = any(
            dep.strip().lower().split("[", 1)[0].startswith("playwright") for dep in dependencies
        )

        for dep in dependencies:
            dep = dep.strip()
            if not dep:
                continue

            if uv_available:
                cmd = ["uv", "pip", "install", dep]
            else:
                cmd = [sys.executable, "-m", "pip", "install", dep]

            logger.info("安装插件依赖: %s", " ".join(cmd))
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=120.0,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                if result.returncode == 0:
                    logger.info("依赖安装成功: %s", dep)
                    success_count += 1
                else:
                    logger.warning("依赖安装失败 [%s]: %s", dep, result.stderr.strip()[-200:])
                    fail_count += 1
            except Exception as exc:
                logger.warning("依赖安装异常 [%s]: %s", dep, exc)
                fail_count += 1

        # playwright 需要额外安装 chromium 浏览器
        if needs_chromium:
            install_cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
            logger.info("安装 Chromium 浏览器: %s", " ".join(install_cmd))
            try:
                result = subprocess.run(
                    install_cmd,
                    capture_output=True,
                    text=True,
                    timeout=300.0,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                if result.returncode == 0:
                    logger.info("Chromium 安装成功")
                else:
                    logger.warning("Chromium 安装失败: %s", result.stderr.strip()[-200:])
            except Exception as exc:
                logger.warning("Chromium 安装异常: %s", exc)

        return success_count, fail_count

    @staticmethod
    def ensure_plugins_directory(plugins_dir: Path) -> None:
        """确保插件目录存在并包含 README。"""
        plugins_dir.mkdir(parents=True, exist_ok=True)
        readme_path = plugins_dir / "README.md"
        if not readme_path.exists():
            readme_path.write_text(_PLUGINS_README, encoding="utf-8")


_PLUGINS_README = """# plugins 目录说明

此目录用于存放 Sirius Chat 在当前人格下的 Plugin 插件包。

- 每个 Plugin 使用独立的文件夹。
- 文件夹内至少包含一个 `.py` 文件，其中定义继承自 `PluginBase` 的类。
- 通过类属性和 `@command` 装饰器声明插件元数据和指令。

最小示例：

```python
# hello_plugin.py
from sirius_pulse.plugins import PluginBase, PluginResponse
from sirius_pulse.plugins.decorators import command

class HelloPlugin(PluginBase):
    _plugin_name = "hello"
    _plugin_display_name = "问候插件"

    @command("hello", patterns=["/hello"], render_mode="direct")
    def hello(self) -> PluginResponse:
        return PluginResponse.ok(text="你好呀！")
```
"""
