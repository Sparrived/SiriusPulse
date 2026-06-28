"""插件注册中心在用户消息入口上的业务行为测试。"""

from __future__ import annotations

import pytest

from sirius_pulse.plugins import PluginRegistry
from sirius_pulse.plugins.models import (
    PluginCommandDef,
    PluginDefinition,
    PluginPermissionDef,
    PluginRenderDef,
)


def _plugin(
    name: str,
    commands: list[PluginCommandDef],
    *,
    description: str = "test plugin",
    permissions: PluginPermissionDef | None = None,
) -> PluginDefinition:
    return PluginDefinition(
        name=name,
        display_name=name,
        description=description,
        version="1.0",
        commands=commands,
        events=[],
        parameters=[],
        permissions=permissions or PluginPermissionDef(),
        render=PluginRenderDef(),
        dependencies=[],
        source_path=None,
    )


def test_plugin_registry_when_plugin_is_registered_then_user_command_can_find_it():
    registry = PluginRegistry()
    definition = _plugin(
        "dice_plugin",
        [
            PluginCommandDef(
                name="dice",
                patterns=["/dice"],
                pattern_type="prefix",
                description="掷骰子",
            )
        ],
    )

    registry.register(definition, instance=None)
    match = registry.match_message("/dice 100")

    assert registry.get("dice_plugin") is definition
    assert match is not None
    assert match.plugin_name == "dice_plugin"
    assert match.command_name == "dice"
    assert match.lexed is not None
    assert match.lexed.positional_args == ["100"]


def test_plugin_registry_when_user_uses_unrelated_command_then_no_plugin_is_selected():
    registry = PluginRegistry()
    registry.register(
        _plugin(
            "dice_plugin",
            [PluginCommandDef(name="dice", patterns=["/dice"], pattern_type="prefix")],
        ),
        instance=None,
    )

    assert registry.match_message("/weather Beijing") is None


def test_plugin_registry_when_keyword_plugin_exists_then_natural_language_can_match_it():
    registry = PluginRegistry()
    registry.register(
        _plugin(
            "weather_plugin",
            [PluginCommandDef(name="weather", patterns=["天气"], pattern_type="keyword")],
        ),
        instance=None,
    )

    match = registry.match_message("帮我看看北京天气怎么样")

    assert match is not None
    assert match.plugin_name == "weather_plugin"
    assert match.confidence == 0.9


def test_plugin_registry_when_plugin_is_hidden_from_intent_then_description_excludes_it():
    registry = PluginRegistry()
    registry.register(
        _plugin(
            "admin_plugin",
            [PluginCommandDef(name="admin", patterns=["/admin"], pattern_type="prefix")],
            description="管理员工具",
            permissions=PluginPermissionDef(hidden_from_intent=True),
        ),
        instance=None,
    )

    assert registry.get_plugin_descriptions(caller_is_developer=True) == ""
    assert registry.match_message("/admin reload") is not None


def test_plugin_registry_when_plugin_is_developer_only_then_normal_users_do_not_see_it():
    registry = PluginRegistry()
    registry.register(
        _plugin(
            "debug_plugin",
            [PluginCommandDef(name="debug", patterns=["/debug"], pattern_type="prefix")],
            description="调试工具",
            permissions=PluginPermissionDef(developer_only=True),
        ),
        instance=None,
    )

    assert registry.get_plugin_descriptions(caller_is_developer=False) == ""
    assert "debug_plugin" in registry.get_plugin_descriptions(caller_is_developer=True)


def test_plugin_registry_when_duplicate_plugin_name_is_loaded_then_registry_rejects_it():
    registry = PluginRegistry()
    registry.register(_plugin("same_name", []), instance=None)

    with pytest.raises(ValueError):
        registry.register(_plugin("same_name", []), instance=None)


def test_plugin_registry_when_plugin_is_uninstalled_then_its_commands_stop_matching():
    registry = PluginRegistry()
    registry.register(
        _plugin(
            "temporary_plugin",
            [PluginCommandDef(name="temp", patterns=["/temp"], pattern_type="prefix")],
        ),
        instance=None,
    )

    registry.unregister("temporary_plugin")

    assert registry.get("temporary_plugin") is None
    assert registry.match_message("/temp") is None
    assert registry.plugin_count == 0


def test_plugin_registry_when_workspace_reloads_then_clear_removes_all_runtime_plugins():
    registry = PluginRegistry()
    registry.register(_plugin("a", []), instance=None)
    registry.register(_plugin("b", []), instance=None)

    registry.clear()

    assert registry.plugin_names == []
    assert registry.plugin_count == 0
