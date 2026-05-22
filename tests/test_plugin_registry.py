"""插件注册中心关键路径测试。"""
from __future__ import annotations

from sirius_pulse.plugins import PluginRegistry
from sirius_pulse.plugins.models import (
    PluginCommandDef,
    PluginDefinition,
    PluginPermissionDef,
    PluginRenderDef,
)


def _make_plugin_def(name: str, commands: list[PluginCommandDef]) -> PluginDefinition:
    return PluginDefinition(
        name=name,
        display_name=name,
        description="test plugin",
        version="1.0",
        commands=commands,
        events=[],
        parameters=[],
        permissions=PluginPermissionDef(),
        render=PluginRenderDef(),
        dependencies=[],
        source_path=None,
    )


def test_register_and_get():
    """注册插件并查找。"""
    registry = PluginRegistry()
    definition = _make_plugin_def("test_plugin", [])
    registry.register(definition, None)

    assert registry.get("test_plugin") is definition
    assert "test_plugin" in registry.plugin_names
    assert registry.plugin_count == 1


def test_match_prefix_command():
    """前缀匹配插件指令。"""
    registry = PluginRegistry()
    cmd_def = PluginCommandDef(
        name="dice",
        patterns=["/dice"],
        pattern_type="prefix",
        description="掷骰子",
    )
    definition = _make_plugin_def("dice_plugin", [cmd_def])
    registry.register(definition, None)

    result = registry.match_message("/dice 100")
    assert result is not None
    assert result.plugin_name == "dice_plugin"
    assert result.command_name == "dice"

    result = registry.match_message("/roll 100")
    assert result is None


def test_match_keyword_command():
    """关键词匹配插件指令。"""
    registry = PluginRegistry()
    cmd_def = PluginCommandDef(
        name="weather",
        patterns=["天气"],
        pattern_type="keyword",
        description="查天气",
    )
    definition = _make_plugin_def("weather_plugin", [cmd_def])
    registry.register(definition, None)

    result = registry.match_message("/北京天气怎么样")
    assert result is not None
    assert result.plugin_name == "weather_plugin"


def test_get_plugin_descriptions():
    """生成 LLM 用插件描述。"""
    registry = PluginRegistry()
    cmd_def = PluginCommandDef(
        name="dice",
        patterns=["/dice"],
        pattern_type="prefix",
        description="掷骰子：/dice [max]",
    )
    definition = _make_plugin_def("dice_plugin", [cmd_def])
    registry.register(definition, None)

    desc = registry.get_plugin_descriptions(caller_is_developer=False)
    assert isinstance(desc, str)
    assert "dice" in desc


def test_unregister():
    """注销插件。"""
    registry = PluginRegistry()
    definition = _make_plugin_def("temp_plugin", [])
    registry.register(definition, None)
    assert registry.plugin_count == 1

    registry.unregister("temp_plugin")
    assert registry.plugin_count == 0
    assert registry.get("temp_plugin") is None


def test_clear():
    """清空所有插件。"""
    registry = PluginRegistry()
    registry.register(_make_plugin_def("a", []), None)
    registry.register(_make_plugin_def("b", []), None)
    assert registry.plugin_count == 2

    registry.clear()
    assert registry.plugin_count == 0
