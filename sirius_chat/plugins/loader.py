"""Plugin 加载器 —— 扫描插件目录、校验 plugin.json、导入 Python 模块。

负责：
    1. 扫描 plugins/ 目录下的文件夹级插件包
    2. 校验 plugin.json  Schema
    3. 动态导入 __init__.py 中的 PluginBase 子类
    4. 处理加载错误并记录日志
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

from sirius_chat.plugins.models import PluginDefinition

logger = logging.getLogger(__name__)

# plugin.json 必需字段
_REQUIRED_FIELDS = {"name"}
# plugin.json 字符串字段
_STRING_FIELDS = {
    "name", "display_name", "description", "version", "author",
    "min_framework_version",
}


class PluginLoadError(Exception):
    """Plugin 加载错误。"""

    def __init__(self, plugin_path: Path, reason: str) -> None:
        self.plugin_path = plugin_path
        self.reason = reason
        super().__init__(f"加载 Plugin 失败 [{plugin_path.name}]: {reason}")


class PluginLoader:
    """Plugin 加载器。

    负责从 plugins/ 目录发现、校验、导入插件包。
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
            # 检查是否有 plugin.json
            if (entry / "plugin.json").exists():
                discovered.append(entry)
            else:
                logger.debug("跳过非插件目录: %s（缺少 plugin.json）", entry.name)

        logger.info("发现 %d 个插件目录", len(discovered))
        return discovered

    def load_metadata(self, plugin_path: Path) -> dict[str, Any]:
        """从 plugin.json 加载元数据。

        Args:
            plugin_path: 插件文件夹路径

        Returns:
            解析后的 plugin.json 字典

        Raises:
            PluginLoadError: 元数据加载或校验失败
        """
        config_file = plugin_path / "plugin.json"
        if not config_file.exists():
            raise PluginLoadError(plugin_path, "缺少 plugin.json")

        try:
            raw_text = config_file.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise PluginLoadError(plugin_path, f"plugin.json 格式错误: {exc}") from exc
        except OSError as exc:
            raise PluginLoadError(plugin_path, f"无法读取 plugin.json: {exc}") from exc

        if not isinstance(data, dict):
            raise PluginLoadError(plugin_path, "plugin.json 必须是 JSON 对象（dict）")

        # 校验必需字段
        missing = _REQUIRED_FIELDS - set(data.keys())
        if missing:
            raise PluginLoadError(plugin_path, f"缺少必需字段: {', '.join(sorted(missing))}")

        # 校验字段类型
        for field in _STRING_FIELDS:
            if field in data and not isinstance(data[field], str):
                raise PluginLoadError(plugin_path, f"字段 '{field}' 必须是字符串")

        return data

    def load_definition(self, plugin_path: Path) -> PluginDefinition:
        """加载完整的 PluginDefinition。

        Args:
            plugin_path: 插件文件夹路径

        Returns:
            PluginDefinition 实例

        Raises:
            PluginLoadError: 加载失败
        """
        metadata = self.load_metadata(plugin_path)
        return PluginDefinition.from_dict(metadata, source_path=plugin_path)

    def import_plugin_class(self, plugin_path: Path) -> type | None:
        """从插件的 __init__.py 中导入 PluginBase 子类。

        Args:
            plugin_path: 插件文件夹路径

        Returns:
            PluginBase 子类，如果找不到则返回 None

        Raises:
            PluginLoadError: 导入失败
        """
        init_file = plugin_path / "__init__.py"
        if not init_file.exists():
            raise PluginLoadError(plugin_path, "缺少 __init__.py")

        module_name = f"_plugin_{plugin_path.name}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, init_file)
            if spec is None or spec.loader is None:
                raise PluginLoadError(plugin_path, "无法创建模块规格")

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            raise PluginLoadError(plugin_path, f"导入 __init__.py 失败: {exc}") from exc

        # 查找 PluginBase 子类
        from sirius_chat.plugins.base import PluginBase

        for attr_name in dir(module):
            attr = getattr(module, attr_name, None)
            if (
                isinstance(attr, type)
                and issubclass(attr, PluginBase)
                and attr is not PluginBase
            ):
                return attr

        logger.warning("插件 %s 中未找到 PluginBase 子类", plugin_path.name)
        return None

    def load_all_definitions(self) -> list[PluginDefinition]:
        """发现并加载所有插件的 PluginDefinition（不导入 Python 类）。

        用于快速索引构建阶段（Registry 需要先知道有哪些插件）。

        Returns:
            PluginDefinition 列表
        """
        definitions: list[PluginDefinition] = []
        for plugin_path in self.discover():
            try:
                definition = self.load_definition(plugin_path)
                definitions.append(definition)
                logger.info("加载插件元数据: %s v%s", definition.name, definition.version)
            except PluginLoadError as exc:
                logger.error("%s", exc)
            except Exception as exc:
                logger.error("加载插件元数据失败 [%s]: %s", plugin_path.name, exc)

        return definitions

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
- 文件夹内必须包含 `plugin.json`（元数据）和 `__init__.py`（入口）。
- `__init__.py` 中需要定义一个继承自 `PluginBase` 的类。
- 文件夹名建议使用英文、数字、下划线，避免以下划线或点号开头。

最小示例：

```python
# plugin.json
{
  "name": "hello",
  "display_name": "问候插件",
  "version": "1.0.0",
  "triggers": {
    "commands": [{
      "name": "hello",
      "patterns": ["/hello"],
      "pattern_type": "prefix"
    }]
  },
  "render": {"mode": "direct"}
}

# __init__.py
from sirius_chat.plugins import PluginBase, PluginContext, PluginResult

class HelloPlugin(PluginBase):
    def execute(self, cmd):
        return PluginResult.ok(text="你好呀！")
```
"""
