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
import subprocess
import sys
from pathlib import Path
from typing import Any

from sirius_pulse.plugins.models import PluginDefinition

logger = logging.getLogger(__name__)


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

    def discover(self) -> list[Path]:
        """扫描 plugins/ 目录，发现所有有效插件文件夹。

        Returns:
            插件文件夹路径列表
        """
        if not self._plugins_dir.exists():
            logger.info("插件目录不存在: %s", self._plugins_dir)
            return []

        discovered: list[Path] = []
        for entry in sorted(self._plugins_dir.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith("_") or entry.name.startswith("."):
                continue
            # 检查目录下是否有 .py 文件
            has_py = any(entry.glob("*.py"))
            if has_py:
                discovered.append(entry)
            else:
                logger.debug("跳过非插件目录: %s（无 .py 文件）", entry.name)

        logger.info("发现 %d 个插件目录", len(discovered))
        return discovered

    def load_all_definitions(self) -> list[PluginDefinition]:
        """发现并加载所有插件的 PluginDefinition。

        加载前自动安装每个插件的 _plugin_dependencies 依赖。

        Returns:
            PluginDefinition 列表
        """
        definitions: list[PluginDefinition] = []
        for plugin_path in self.discover():
            # 1. 先安装插件依赖（AST 解析，无需导入）
            deps = self._parse_dependencies_from_source(plugin_path)
            if deps:
                logger.info("插件 [%s] 需要依赖: %s", plugin_path.name, ", ".join(deps))
                ok, fail = self.install_dependencies(deps)
                if fail > 0:
                    logger.warning("插件 [%s] 有 %d 个依赖安装失败", plugin_path.name, fail)

            # 2. 加载定义并导入
            try:
                definition = self.load_definition(plugin_path)
                if definition is not None:
                    definitions.append(definition)
                    logger.info("加载插件: %s v%s", definition.name, definition.version)
            except PluginLoadError as exc:
                logger.error("%s", exc)
            except Exception as exc:
                logger.error("加载插件失败 [%s]: %s", plugin_path.name, exc)

        return definitions

    def load_definition(self, plugin_path: Path) -> PluginDefinition | None:
        """加载插件的 PluginDefinition。

        优先从 Python 类的类属性 + @command 构建。
        兼容旧的 plugin.json。

        Args:
            plugin_path: 插件文件夹路径

        Returns:
            PluginDefinition 实例或 None
        """
        # 尝试导入 PluginBase 子类
        plugin_cls = self.import_plugin_class(plugin_path)
        if plugin_cls is not None:
            return PluginDefinition.from_class(plugin_cls, source_path=plugin_path)

        # 回退到 plugin.json（兼容旧格式）
        config_file = plugin_path / "plugin.json"
        if config_file.exists():
            return self._load_definition_from_json(plugin_path)
        return None

    def _load_definition_from_json(self, plugin_path: Path) -> PluginDefinition:
        """从 plugin.json 加载定义（兼容旧格式）。"""
        config_file = plugin_path / "plugin.json"
        try:
            raw_text = config_file.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise PluginLoadError(plugin_path, f"plugin.json 格式错误: {exc}") from exc

        if not isinstance(data, dict):
            raise PluginLoadError(plugin_path, "plugin.json 必须是 JSON 对象")

        return PluginDefinition.from_dict(data, source_path=plugin_path)

    def import_plugin_class(self, plugin_path: Path) -> type | None:
        """从插件目录的 .py 文件中导入 PluginBase 子类。

        扫描所有 .py 文件，优先从 __init__.py 寻找 PluginBase 子类。
        以 _ 开头的非 __init__.py 文件视为私有辅助模块，跳过不导入。

        Args:
            plugin_path: 插件文件夹路径

        Returns:
            PluginBase 子类，找不到则返回 None
        """
        from sirius_pulse.plugins.base import PluginBase

        # __init__.py 排在首位，其余按字母序
        py_files = sorted(plugin_path.glob("*.py"), key=lambda p: (p.name != "__init__.py", p.name))

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

        module_name = f"_plugin_{py_file.parent.name}_{py_file.stem}"
        # 设置包名以支持相对导入
        package_name = f"_plugin_pkg_{py_file.parent.name}"

        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                return None

            module = importlib.util.module_from_spec(spec)
            # 启用相对导入支持
            module.__package__ = package_name
            # 注册包命名空间
            if package_name not in sys.modules:
                pkg = type(sys)(package_name)
                pkg.__path__ = [str(py_file.parent)]  # type: ignore[attr-defined]
                sys.modules[package_name] = pkg
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            logger.debug("导入 %s 失败: %s", py_file.name, exc)
            return None

        for attr_name in dir(module):
            attr = getattr(module, attr_name, None)
            if (
                isinstance(attr, type)
                and issubclass(attr, PluginBase)
                and attr is not PluginBase
            ):
                return attr

        return None

    # ── 依赖安装 ──

    @staticmethod
    def _parse_dependencies_from_source(plugin_path: Path) -> list[str]:
        """从 .py 文件 AST 中提取 _plugin_dependencies，无需导入代码。

        这样可以在依赖安装之前就发现插件需要哪些库。
        """
        for py_file in sorted(plugin_path.glob("*.py"), key=lambda p: p.name):
            if py_file.name.startswith("_"):
                continue
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assign):
                        for target in node.targets:
                            if (
                                isinstance(target, ast.Name)
                                and target.id == "_plugin_dependencies"
                            ):
                                if isinstance(node.value, ast.List):
                                    return [
                                        elt.value
                                        for elt in node.value.elts
                                        if isinstance(elt, ast.Constant)
                                        and isinstance(elt.value, str)
                                    ]
            except Exception:
                pass
        return []

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
                ["uv", "--version"], capture_output=True, text=True, timeout=5.0
            )
            uv_available = result.returncode == 0
        except Exception:
            pass

        needs_chromium = "playwright" in dependencies

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
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120.0)
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
                    install_cmd, capture_output=True, text=True, timeout=300.0
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
