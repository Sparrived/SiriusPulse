from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sirius_pulse.plugins.base import PluginBase
from sirius_pulse.plugins.config import PluginConfigManager
from sirius_pulse.plugins.context import MessageContext, PluginContext, PluginDataStore
from sirius_pulse.plugins.models import (
    ArgNode,
    CommandAST,
    PluginDefinition,
    PluginResponse,
    RenderMode,
)


def test_plugin_command_ast_when_arguments_are_present_then_paths_getters_and_dict_are_stable():
    ast = CommandAST(
        command="tools",
        raw_text="/tools image resize file.png --count 3 --ratio 0.5 --dry-run",
        prefix="/",
        subcommands=["image", "resize"],
        args=[ArgNode("file.png", "file.png")],
        kwargs={
            "count": ArgNode("3", "3"),
            "ratio": ArgNode("0.5", "0.5"),
            "enabled": ArgNode("yes", "yes"),
            "bad": ArgNode("NaN?", "NaN?"),
        },
        flags={"dry-run"},
    )

    assert ast.command_path == ["tools", "image", "resize"]
    assert ast.full_command == "tools image resize"
    assert ast.leaf_command == "resize"
    assert ast.get_positional(0) == "file.png"
    assert ast.get_positional(9) is None
    assert ast.get_str("count") == "3"
    assert ast.get_int("count") == 3
    assert ast.get_int("bad", 7) == 7
    assert ast.get_float("ratio") == 0.5
    assert ast.get_bool("dry-run") is True
    assert ast.get_bool("enabled") is True
    assert ast.to_dict()["subcommands"] == ["image", "resize"]
    assert ast.to_dict()["flags"] == ["dry-run"]


def test_plugin_definition_when_loaded_from_dict_then_nested_contracts_are_parsed(tmp_path):
    definition = PluginDefinition.from_dict(
        {
            "name": "weather",
            "display_name": "Weather",
            "description": "forecast",
            "version": "2.0",
            "author": "tester",
            "min_framework_version": "1.4.0",
            "triggers": {
                "commands": [
                    {
                        "name": "forecast",
                        "patterns": ["/weather", "weather"],
                        "pattern_type": "prefix",
                        "description": "forecast command",
                        "examples": ["/weather Paris"],
                    }
                ],
                "command_groups": [{"name": "weather", "patterns": ["/weather"]}],
                "events": [{"type": "timer.daily", "cron": "0 8 * * *", "interval_seconds": 60}],
            },
            "parameters": {
                "city": {
                    "type": "str",
                    "description": "city",
                    "required": True,
                    "position": 1,
                    "choices": ["Paris"],
                }
            },
            "natural_language": {
                "examples": ["weather in {city}"],
                "slots": {"city": {"type": "str"}},
            },
            "permissions": {
                "developer_only": True,
                "hidden_from_intent": True,
                "adapter_types": ["napcat"],
                "group_blacklist": ["g1"],
                "rate_limit": {"calls_per_minute": 2, "calls_per_hour": 20},
            },
            "render": {
                "mode": "llm",
                "system_prompt_suffix": "suffix",
                "max_tokens": 50,
                "temperature": 0.2,
            },
            "dependencies": ["requests"],
            "resources": ["template.txt"],
            "prompt_inject": "can forecast",
        },
        source_path=tmp_path,
    )

    assert definition.name == "weather"
    assert definition.source_path == tmp_path
    assert definition.all_patterns == [
        ("forecast", "/weather", "prefix"),
        ("forecast", "weather", "prefix"),
    ]
    assert definition.is_passive is False
    assert definition.parameters[0].name == "city"
    assert definition.parameters[0].choices == ["Paris"]
    assert definition.natural_language is not None
    assert definition.permissions.developer_only is True
    assert definition.permissions.rate_limit_calls_per_hour == 20
    assert definition.render.system_prompt_suffix == "suffix"
    assert definition.get_render_mode() is RenderMode.LLM


def test_plugin_definition_when_built_from_class_then_metadata_and_schedule_are_collected(tmp_path):
    class DemoPlugin(PluginBase):
        _plugin_name = "demo"
        _plugin_display_name = "Demo"
        _plugin_description = "demo description"
        _plugin_version = "1.2.3"
        _plugin_author = "tester"
        _plugin_events = [{"type": "engine.started", "interval_seconds": 1}]
        _plugin_schedule = [{"time": "8:05", "duration": 30}, {"time": "bad"}]
        _plugin_nl_examples = ["do demo"]
        _plugin_nl_slots = {"topic": {"type": "str", "description": "topic"}}
        _plugin_permissions = {"developer_only": True, "rate_limit": {"calls_per_minute": 3}}
        _plugin_dependencies = ["dep"]
        _plugin_prompt_inject = "demo ability"

    definition = PluginDefinition.from_class(DemoPlugin, source_path=tmp_path)

    assert definition.name == "demo"
    assert definition.display_name == "Demo"
    assert [event.type for event in definition.events] == ["engine.started", "timer.schedule"]
    assert definition.events[1].cron == "05 08 * * *"
    assert definition.events[1].interval_seconds == 1800.0
    assert definition.natural_language is not None
    assert definition.parameters[0].name == "topic"
    assert definition.permissions.developer_only is True
    assert definition.permissions.rate_limit_calls_per_minute == 3
    assert definition.dependencies == ["dep"]
    assert definition.prompt_inject == "demo ability"


def test_plugin_response_when_using_factories_then_success_and_failure_payloads_are_created():
    ok = PluginResponse.ok("done", data={"x": 1}, render_mode="silent", metadata={"m": 2})
    failed = PluginResponse.fail("nope")

    assert ok.success is True
    assert ok.text == "done"
    assert ok.data == {"x": 1}
    assert ok.render_mode == "silent"
    assert ok.metadata == {"m": 2}
    assert failed.success is False
    assert failed.error == "nope"


def test_plugin_data_store_when_values_change_then_json_persists_and_invalid_files_fallback(
    tmp_path,
):
    store = PluginDataStore(tmp_path, "demo")
    store.set("count", 1)
    store.set("items", ["a"])

    reloaded = PluginDataStore(tmp_path, "demo")
    reloaded.delete("count")

    assert reloaded.get("items") == ["a"]
    assert reloaded.get("count", "missing") == "missing"
    assert reloaded.all() == {"items": ["a"]}

    broken_file = tmp_path / "_plugin_broken_data.json"
    broken_file.write_text("{broken", encoding="utf-8")
    assert PluginDataStore(tmp_path, "broken").all() == {}


def test_plugin_context_and_base_when_setup_is_missing_or_present_then_helpers_use_context(
    tmp_path,
):
    plugin = PluginBase()

    with pytest.raises(RuntimeError):
        _ = plugin.ctx
    with pytest.raises(RuntimeError):
        plugin.get_data_store()
    assert plugin.render_template("missing.txt", {}).startswith("[")

    data_store = PluginDataStore(tmp_path, "demo")
    adapter = object()
    message = MessageContext(group_id="g1", user_id="u1", content="hello")
    context = PluginContext.create(
        adapter=adapter,
        plugin_name="demo",
        message=message,
        data_store=data_store,
        config={"enabled": True},
    )
    plugin._setup("demo", context)
    source = tmp_path / "plugin"
    templates = source / "templates"
    templates.mkdir(parents=True)
    (templates / "hello.txt").write_text("Hello {name}", encoding="utf-8")
    plugin._set_source_path(source)

    assert plugin.name == "demo"
    assert plugin.ctx is context
    assert plugin.get_adapter() is adapter
    assert plugin.get_data_store() is data_store
    assert plugin.render_template("hello.txt", {"name": "Ada"}) == "Hello Ada"
    assert plugin.render_template("missing.txt", {}).startswith("[")
    assert plugin.source_path == source


def test_engine_proxy_when_unbound_or_bound_then_persona_and_lookup_defaults_are_stable():
    context = PluginContext.create(plugin_name="demo")

    assert context.logger.name == "plugin.demo"
    assert context.engine.get_engine() is None
    assert context.engine.get_persona_name() == ""
    assert context.engine.get_persona_info() == {}
    assert context.engine.find_user_by_platform_uid("qq", "1") is None
    assert context.engine.find_user_by_name("Ada") is None
    assert context.engine.get_user_info("u1") is None
    assert context.engine.list_users() == []
    assert context.engine.get_bot_id() == "assistant"
    assert context.engine.get_bot_info() is None
    assert context.engine.get_bot_platform_uid() is None
    assert context.engine.get_bot_platform_uids() == {}

    engine = SimpleNamespace(
        identity_resolver=None,
        user_manager=None,
        persona=SimpleNamespace(
            name="Yue",
            persona_summary="summary",
            personality_traits=["warm"],
            communication_style="casual",
        ),
    )
    bound = PluginContext.create(engine=engine, plugin_name="demo")

    assert bound.engine.get_engine() is engine
    assert bound.engine.get_persona_name() == "Yue"
    assert bound.engine.get_persona_info() == {
        "name": "Yue",
        "persona_summary": "summary",
        "personality_traits": ["warm"],
        "communication_style": "casual",
    }


def test_plugin_config_manager_when_old_config_is_loaded_then_updates_notify_and_persist(tmp_path):
    config_path = tmp_path / "_config.json"
    config_path.write_text(
        json.dumps(
            {
                "demo": {
                    "enabled": False,
                    "group_blacklist": ["g1"],
                    "developer_only": True,
                    "schedule": [{"time": "08:00"}],
                }
            }
        ),
        encoding="utf-8",
    )
    manager = PluginConfigManager(tmp_path)
    notifications: list[tuple[str, dict]] = []
    listener = manager.add_listener(lambda name, config: notifications.append((name, dict(config))))

    assert manager.get_enabled("demo") is False
    assert manager.get_permissions("demo") == {"group_blacklist": ["g1"], "developer_only": True}
    assert manager.get_settings("demo") == {"schedule": [{"time": "08:00"}]}

    manager.set_enabled("demo", True)
    manager.update_permissions("demo", {"developer_only": False})
    manager.update_settings("demo", {"schedule": [{"time": "09:00"}]})
    manager.set_setting("demo", "mode", "fast")
    manager.delete_setting("demo", "mode")
    manager.remove_listener(listener)
    manager.set_setting("demo", "quiet", True)

    assert [item[0] for item in notifications] == ["demo", "demo", "demo", "demo", "demo"]
    assert manager.get_enabled("demo") is True
    assert manager.get_permissions("demo")["developer_only"] is False
    assert manager.get_setting("demo", "quiet") is True
    assert "demo" in manager.get_all_plugins()
    assert "demo" in manager.get_all_configs()
    assert json.loads(config_path.read_text(encoding="utf-8"))["demo"]["settings"]["quiet"] is True

    config_path.write_text(
        json.dumps({"demo": {"enabled": False, "permissions": {}, "settings": {}}}),
        encoding="utf-8",
    )
    manager.add_listener(
        lambda name, config: notifications.append((f"reload:{name}", dict(config)))
    )
    manager.reload()
    assert manager.get_enabled("demo") is False
    assert notifications[-1][0] == "reload:demo"

    manager.remove_plugin("demo")
    assert manager.get_all_plugins() == []
    manager.reset()
    assert manager.get_all_configs() == {}
