"""插件注册中心在用户消息入口上的业务行为测试。"""

from __future__ import annotations

import json

import pytest

from sirius_pulse.plugins import PluginLoader, PluginLoadError, PluginRegistry
from sirius_pulse.plugins.models import (
    PluginCommandDef,
    PluginDefinition,
    PluginParameterDef,
    PluginPermissionDef,
    PluginRenderDef,
    normalize_plugin_ui_schema,
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


def test_plugin_loader_when_plugin_directory_contains_tests_then_skips_test_directory(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_plugin.py").write_text("class NotAPlugin: pass", encoding="utf-8")
    (tmp_path / "real_plugin").mkdir()
    (tmp_path / "real_plugin" / "plugin.py").write_text(
        "from sirius_pulse.plugins import PluginBase\n"
        "class RealPlugin(PluginBase):\n"
        "    _plugin_name = 'real_plugin'\n",
        encoding="utf-8",
    )

    discovered = PluginLoader(tmp_path).discover()

    assert [path.name for path in discovered] == ["real_plugin"]


def test_plugin_loader_rejects_symlinked_plugin_sources(tmp_path):
    external = tmp_path / "external.py"
    external.write_text("raise AssertionError('must not execute')", encoding="utf-8")
    plugin_path = tmp_path / "linked_plugin"
    plugin_path.mkdir()
    try:
        (plugin_path / "__init__.py").symlink_to(external)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    discovered = PluginLoader(tmp_path).discover()

    assert plugin_path not in discovered


def test_plugin_loader_metadata_listing_does_not_import_or_install(tmp_path, monkeypatch):
    plugin_path = tmp_path / "safe_metadata"
    plugin_path.mkdir()
    marker = plugin_path / "imported.txt"
    (plugin_path / "__init__.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_name('imported.txt').write_text('executed')\n"
        "class SafeMetadataPlugin:\n"
        "    _plugin_name = 'safe_metadata'\n"
        "    _plugin_display_name = 'Safe metadata'\n"
        "    _plugin_min_framework_version = '1.1.0'\n"
        "    _plugin_dependencies = ['example-package>=1']\n"
        "    _plugin_parameters = [{'name': 'interval', 'type': 'int'}]\n",
        encoding="utf-8",
    )
    install_calls: list[list[str]] = []
    monkeypatch.setattr(
        PluginLoader,
        "install_dependencies",
        staticmethod(lambda deps: (install_calls.append(list(deps)) or (len(deps), 0))),
    )
    monkeypatch.setattr(
        "sirius_pulse.plugins.loader._installed_framework_version",
        lambda: "1.1.0",
    )

    definitions = PluginLoader(tmp_path).load_all_definitions(metadata_only=True)

    assert [definition.name for definition in definitions] == ["safe_metadata"]
    assert definitions[0].dependencies == ["example-package>=1"]
    assert definitions[0].parameters[0].name == "interval"
    assert marker.exists() is False
    assert install_calls == []


def test_plugin_loader_metadata_rejects_duplicate_top_level_parameter_names(tmp_path):
    plugin_path = tmp_path / "duplicate_parameters"
    plugin_path.mkdir()
    (plugin_path / "__init__.py").write_text(
        "class DuplicateParametersPlugin:\n"
        "    _plugin_name = 'duplicate_parameters'\n"
        "    _plugin_parameters = [\n"
        "        {'name': 'endpoint', 'type': 'str'},\n"
        "        {'name': 'endpoint', 'type': 'password'},\n"
        "    ]\n",
        encoding="utf-8",
    )

    with pytest.raises(PluginLoadError, match="重复名称"):
        PluginLoader(tmp_path).load_metadata_definition(plugin_path)


def test_plugin_loader_metadata_mode_rejects_dependency_install_request(tmp_path):
    with pytest.raises(ValueError, match="metadata-only"):
        PluginLoader(tmp_path).load_all_definitions(metadata_only=True, install_dependencies=True)


@pytest.mark.parametrize("pattern", ["", " ", "\t\n"])
def test_plugin_metadata_rejects_empty_or_whitespace_command_patterns(tmp_path, pattern):
    plugin_path = tmp_path / "invalid_patterns"
    plugin_path.mkdir()
    (plugin_path / "__init__.py").write_text("", encoding="utf-8")
    (plugin_path / "plugin.json").write_text(
        json.dumps(
            {
                "name": "invalid_patterns",
                "triggers": {
                    "commands": [
                        {
                            "name": "invalid",
                            "patterns": [pattern],
                            "pattern_type": "prefix",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PluginLoadError, match="元数据结构无效"):
        PluginLoader(tmp_path).load_metadata_definition(plugin_path)


@pytest.mark.parametrize("pattern", ["", "\u3000"])
def test_plugin_command_definition_rejects_empty_patterns(pattern):
    with pytest.raises(ValueError, match="不能为空"):
        PluginCommandDef(name="invalid", patterns=[pattern], pattern_type="prefix")


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
    assert registry.match_message("/dicebox 100") is None


@pytest.mark.parametrize("reverse_registration", [False, True])
def test_plugin_registry_selects_longest_explicit_prefix_regardless_of_registration_order(
    reverse_registration,
):
    registry = PluginRegistry()
    definitions = [
        _plugin(
            "admin_plugin",
            [PluginCommandDef(name="admin", patterns=["/admin"], pattern_type="prefix")],
        ),
        _plugin(
            "admin_reset_plugin",
            [
                PluginCommandDef(
                    name="admin_reset",
                    patterns=["/admin reset"],
                    pattern_type="prefix",
                )
            ],
        ),
    ]
    if reverse_registration:
        definitions.reverse()
    for definition in definitions:
        registry.register(definition)

    match = registry.match_message("/admin reset now")

    assert match is not None
    assert match.plugin_name == "admin_reset_plugin"
    assert match.command_name == "admin_reset"
    assert match.pattern == "/admin reset"


@pytest.mark.parametrize(
    ("pattern", "text", "matches"),
    [
        ("#roll", "#roll", True),
        ("#roll", "#roll\u30002d6", True),
        ("#roll", "#roller", False),
        ("!roll", "!roll\u00a02d6", True),
        ("!roll", "!rollup", False),
        ("/dice", "/dice\u2003100", True),
        ("/dice", "/dicebox", False),
    ],
)
def test_plugin_registry_enforces_explicit_prefix_boundaries(pattern, text, matches):
    registry = PluginRegistry()
    registry.register(
        _plugin(
            "command_plugin",
            [PluginCommandDef(name="command", patterns=[pattern], pattern_type="prefix")],
        )
    )

    assert (registry.match_message(text) is not None) is matches


@pytest.mark.parametrize(
    "text",
    ["//dice", "/ dice", "/#dice", "#!roll", "# roll", "!!roll", "! roll"],
)
def test_plugin_registry_rejects_malformed_explicit_prefixes(text):
    registry = PluginRegistry()
    registry.register(
        _plugin(
            "commands",
            [
                PluginCommandDef(name="dice", patterns=["/dice"], pattern_type="prefix"),
                PluginCommandDef(name="hash_roll", patterns=["#roll"], pattern_type="prefix"),
                PluginCommandDef(name="bang_roll", patterns=["!roll"], pattern_type="prefix"),
            ],
        )
    )

    assert registry.match_message(text) is None


def test_plugin_registry_preserves_non_explicit_prefix_startswith_semantics():
    registry = PluginRegistry()
    registry.register(
        _plugin(
            "weather_plugin",
            [PluginCommandDef(name="weather", patterns=["weather"], pattern_type="prefix")],
        )
    )

    match = registry.match_message("weatherproof forecast")

    assert match is not None
    assert match.command_name == "weather"


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


def test_plugin_registry_when_plugin_is_hidden_from_intent_then_rule_still_matches():
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

    assert registry.match_message("/admin reload") is not None


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


@pytest.mark.parametrize(
    ("required_version", "accepted"),
    [("1.1.0", True), ("1.2.0", False), ("not-a-version", False)],
)
def test_plugin_loader_when_framework_requirement_is_checked_then_incompatible_plugin_is_rejected(
    tmp_path, monkeypatch, required_version, accepted
):
    plugin_path = tmp_path / "versioned_plugin"
    plugin_path.mkdir()
    (plugin_path / "__init__.py").write_text(
        "from sirius_pulse.plugins import PluginBase\n"
        "class VersionedPlugin(PluginBase):\n"
        "    _plugin_name = 'versioned_plugin'\n"
        f"    _plugin_min_framework_version = {required_version!r}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sirius_pulse.plugins.loader._installed_framework_version",
        lambda: "1.1.0",
    )

    loader = PluginLoader(tmp_path)
    if accepted:
        definition = loader.load_definition(plugin_path)
        assert definition is not None
        assert definition.min_framework_version == required_version
    else:
        with pytest.raises(PluginLoadError):
            loader.load_definition(plugin_path)


def _ui_schema_parameters() -> list[PluginParameterDef]:
    return [
        PluginParameterDef(
            name="sources",
            type="object_array",
            fields=[
                {"name": "id", "type": "str", "required": True, "identity": True},
                {"name": "display_name", "type": "str"},
                {"name": "enabled", "type": "bool", "default": True},
                {"name": "timeout", "type": "int", "minimum": 1, "maximum": 300},
                {"name": "api_token", "type": "password"},
            ],
        ),
        PluginParameterDef(name="poll_seconds", type="int", minimum=30),
    ]


def _valid_ui_schema() -> dict[str, object]:
    return {
        "version": 1,
        "layout": "wide",
        "title": "多站监控",
        "description": "站点 ID 决定状态与环境变量；显示名称只用于展示。",
        "sections": [
            {
                "id": "sites",
                "title": "站点",
                "parameters": ["sources"],
                "columns": 1,
                "collapsed": False,
                "tone": "accent",
            },
            {
                "id": "runtime",
                "title": "运行参数",
                "parameters": ["poll_seconds"],
                "columns": 2,
                "collapsed": False,
                "tone": "default",
            },
        ],
        "parameters": {
            "sources": {
                "label": "站点列表",
                "add_label": "添加站点",
                "item_title_field": "display_name",
                "item_fallback_field": "id",
                "item_badge_field": "id",
                "item_status_field": "enabled",
                "fields": {
                    "id": {"label": "站点 ID", "widget": "code", "span": 6},
                    "display_name": {"label": "显示名称", "span": 6},
                    "enabled": {
                        "label": "状态",
                        "widget": "switch",
                        "true_label": "监控中",
                        "false_label": "已停用",
                    },
                    "timeout": {"label": "超时", "unit": "秒"},
                    "api_token": {"label": "凭据由环境提供"},
                },
                "fieldsets": [
                    {
                        "id": "identity",
                        "title": "身份",
                        "fields": ["enabled", "id", "display_name"],
                        "collapsed": False,
                    },
                    {
                        "id": "network",
                        "title": "网络",
                        "fields": ["timeout", "api_token"],
                        "collapsed": True,
                    },
                ],
            },
            "poll_seconds": {"label": "轮询间隔", "unit": "秒", "span": 6},
        },
    }


def test_plugin_ui_schema_normalizes_presentation_without_redefining_parameters():
    parameters = _ui_schema_parameters()
    raw = _valid_ui_schema()

    normalized = normalize_plugin_ui_schema(raw, parameters)

    assert normalized["version"] == 1
    assert normalized["layout"] == "wide"
    assert normalized["parameters"]["sources"]["item_title_field"] == "display_name"
    assert normalized["parameters"]["sources"]["fields"]["api_token"] == {"label": "凭据由环境提供"}
    assert "identity" not in normalized["parameters"]["sources"]["fields"]["id"]
    assert "required" not in normalized["parameters"]["sources"]["fields"]["id"]
    assert "default" not in normalized["parameters"]["poll_seconds"]
    assert normalized is not raw
    assert normalized["sections"] is not raw["sections"]


def test_plugin_ui_schema_is_keyword_only_to_preserve_definition_positional_contract():
    import inspect

    ui_schema = inspect.signature(PluginDefinition).parameters["ui_schema"]

    assert ui_schema.kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda schema: schema["sections"][0]["parameters"].append("missing"),
            "未声明参数",
        ),
        (
            lambda schema: schema["sections"][1]["parameters"].append("sources"),
            "重复出现在分区",
        ),
        (
            lambda schema: schema["parameters"]["poll_seconds"].update({"widget": "text"}),
            "不支持字段",
        ),
        (
            lambda schema: schema["parameters"]["sources"].update(
                {"item_title_field": "api_token"}
            ),
            "无效子字段",
        ),
        (
            lambda schema: schema.update({"title": "<img src=x onerror=alert(1)>"}),
            "安全文本限制",
        ),
        (
            lambda schema: schema["parameters"]["sources"].update(
                {"help": "https://user:password@host.invalid/config"}
            ),
            "安全文本限制",
        ),
        (
            lambda schema: schema.update({"title": "Authorization: Bearer sk-live-example-123456"}),
            "安全文本限制",
        ),
        (
            lambda schema: schema["parameters"]["sources"]["fieldsets"][1]["fields"].append("id"),
            "重复出现在字段组",
        ),
    ],
)
def test_plugin_ui_schema_rejects_ambiguous_or_unsafe_presentation(mutate, message):
    schema = _valid_ui_schema()
    mutate(schema)

    with pytest.raises(ValueError, match=message):
        normalize_plugin_ui_schema(schema, _ui_schema_parameters())


def test_plugin_ui_schema_rejects_cycles_and_shared_containers():
    shared: dict[str, object] = {"label": "共享"}
    schema = _valid_ui_schema()
    schema["parameters"]["poll_seconds"] = shared
    schema["parameters"]["sources"]["fields"]["id"] = shared

    with pytest.raises(ValueError, match="循环或共享容器"):
        normalize_plugin_ui_schema(schema, _ui_schema_parameters())

    cyclic = _valid_ui_schema()
    cyclic["loop"] = cyclic
    with pytest.raises(ValueError, match="循环或共享容器"):
        normalize_plugin_ui_schema(cyclic, _ui_schema_parameters())


def test_plugin_loader_reads_literal_ui_schema_without_importing_plugin(tmp_path):
    plugin_path = tmp_path / "visual_metadata"
    plugin_path.mkdir()
    marker = plugin_path / "imported.txt"
    (plugin_path / "__init__.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_name('imported.txt').write_text('executed')\n"
        "class VisualMetadataPlugin:\n"
        "    _plugin_name = 'visual_metadata'\n"
        "    _plugin_parameters = [{'name': 'label', 'type': 'str'}]\n"
        "    _plugin_ui_schema = {\n"
        "        'version': 1,\n"
        "        'sections': [{'id': 'general', 'parameters': ['label']}],\n"
        "        'parameters': {'label': {'label': '中文名称'}},\n"
        "    }\n",
        encoding="utf-8",
    )

    definition = PluginLoader(tmp_path).load_metadata_definition(plugin_path)

    assert definition is not None
    assert definition.ui_schema["parameters"]["label"]["label"] == "中文名称"
    assert marker.exists() is False


def test_plugin_loader_rejects_duplicate_parameter_contract_keys(tmp_path):
    plugin_path = tmp_path / "duplicate_parameter_keys"
    plugin_path.mkdir()
    (plugin_path / "__init__.py").write_text(
        "class DuplicateParameterKeysPlugin:\n"
        "    _plugin_name = 'duplicate_parameter_keys'\n"
        "    _plugin_parameters = [\n"
        "        {'name': 'opaque', 'type': 'password', 'type': 'str', "
        "'default': 'hardcoded-secret'},\n"
        "    ]\n",
        encoding="utf-8",
    )

    with pytest.raises(PluginLoadError, match="_plugin_parameters 包含重复字段"):
        PluginLoader(tmp_path).load_metadata_definition(plugin_path)


def test_plugin_loader_rejects_repeated_metadata_assignment(tmp_path):
    plugin_path = tmp_path / "repeated_metadata"
    plugin_path.mkdir()
    (plugin_path / "__init__.py").write_text(
        "class RepeatedMetadataPlugin:\n"
        "    _plugin_name = 'first'\n"
        "    _plugin_name = 'second'\n",
        encoding="utf-8",
    )

    with pytest.raises(PluginLoadError, match="重复声明"):
        PluginLoader(tmp_path).load_metadata_definition(plugin_path)


def test_plugin_loader_rejects_duplicate_literal_ui_schema_keys(tmp_path):
    plugin_path = tmp_path / "duplicate_ui_keys"
    plugin_path.mkdir()
    (plugin_path / "__init__.py").write_text(
        "class DuplicateUiKeysPlugin:\n"
        "    _plugin_name = 'duplicate_ui_keys'\n"
        "    _plugin_parameters = [{'name': 'label', 'type': 'str'}]\n"
        "    _plugin_ui_schema = {\n"
        "        'version': 1,\n"
        "        'parameters': {'label': {'label': 'first', 'label': 'second'}},\n"
        "    }\n",
        encoding="utf-8",
    )

    with pytest.raises(PluginLoadError, match="ui_schema 包含重复字段"):
        PluginLoader(tmp_path).load_metadata_definition(plugin_path)


def test_plugin_loader_rejects_duplicate_nested_json_ui_schema_keys(tmp_path):
    plugin_path = tmp_path / "duplicate_json_ui_keys"
    plugin_path.mkdir()
    (plugin_path / "__init__.py").write_text("", encoding="utf-8")
    (plugin_path / "plugin.json").write_text(
        '{"name":"duplicate_json_ui_keys","parameters":{"label":{"type":"str"}},'
        '"ui_schema":{"version":1,"parameters":{"label":{"label":"first",'
        '"label":"second"}}}}',
        encoding="utf-8",
    )

    with pytest.raises(PluginLoadError, match="重复字段"):
        PluginLoader(tmp_path).load_metadata_definition(plugin_path)


def test_plugin_loader_merges_ast_ui_schema_against_manifest_parameters(tmp_path):
    plugin_path = tmp_path / "manifest_visual"
    plugin_path.mkdir()
    (plugin_path / "plugin.json").write_text(
        json.dumps(
            {
                "name": "manifest_visual",
                "parameters": {"label": {"type": "str"}},
            }
        ),
        encoding="utf-8",
    )
    (plugin_path / "__init__.py").write_text(
        "class ManifestVisualPlugin:\n"
        "    _plugin_name = 'manifest_visual'\n"
        "    _plugin_parameters = [{'name': 'label', 'type': 'str'}]\n"
        "    _plugin_ui_schema = {\n"
        "        'version': 1,\n"
        "        'sections': [{'id': 'general', 'parameters': ['label']}],\n"
        "        'parameters': {'label': {'label': '名称'}},\n"
        "    }\n",
        encoding="utf-8",
    )

    definition = PluginLoader(tmp_path).load_metadata_definition(plugin_path)

    assert definition is not None
    assert definition.ui_schema["parameters"]["label"]["label"] == "名称"
