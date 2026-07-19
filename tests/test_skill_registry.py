"""技能注册中心面向技能作者和模型工具列表的业务行为测试。"""

from __future__ import annotations

from pathlib import Path

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.skills import (
    SkillDefinition,
    SkillInvocationContext,
    SkillRegistry,
    SkillSideEffect,
)


def _skill(
    name: str,
    *,
    description: str | None = None,
    developer_only: bool = False,
    admin_required: bool = False,
    adapter_types: list[str] | None = None,
    tags: list[str] | None = None,
    model_visible: bool = True,
) -> SkillDefinition:
    def run(**kwargs):
        return {"success": True, "text": name}

    return SkillDefinition(
        name=name,
        description=description or f"{name} skill",
        parameters=[],
        developer_only=developer_only,
        admin_required=admin_required,
        adapter_types=adapter_types or [],
        tags=tags or [],
        model_visible=model_visible,
        source_path=None,
        _run_func=run,
    )


def test_skill_registry_when_user_installs_skill_file_then_skill_becomes_callable(
    tmp_skill_dir: Path,
):
    registry = SkillRegistry()

    loaded = registry.load_from_directory(
        tmp_skill_dir,
        auto_install_deps=False,
        include_builtin=False,
    )
    skill = registry.get("test_hello")

    assert loaded == 1
    assert skill is not None
    assert skill.description == "给群友发送问候"
    assert skill.parameters[0].name == "name"
    assert skill.parameters[0].default == "世界"


def test_skill_registry_when_model_prompt_is_built_then_public_skills_are_described():
    registry = SkillRegistry()
    registry.register(_skill("search_web", description="搜索公开资料"))
    registry.register(_skill("read_file", description="读取工作区文件"))

    descriptions = registry.build_tool_descriptions(adapter_type="napcat")

    assert "search_web" in descriptions
    assert "搜索公开资料" in descriptions
    assert "read_file" in descriptions


def test_skill_registry_when_caller_is_not_developer_then_developer_skill_is_hidden():
    registry = SkillRegistry()
    registry.register(_skill("server_shell", developer_only=True))
    user_context = SkillInvocationContext(caller=UnifiedUser(user_id="u1", name="普通用户"))

    assert registry.build_tool_descriptions(invocation_context=user_context) == ""
    assert registry.build_tools_list(invocation_context=user_context) == []


def test_skill_registry_when_caller_is_developer_then_developer_skill_is_available():
    registry = SkillRegistry()
    registry.register(_skill("server_shell", developer_only=True))
    developer_context = SkillInvocationContext(
        caller=UnifiedUser(user_id="dev", name="开发者", metadata={"is_developer": True})
    )

    descriptions = registry.build_tool_descriptions(invocation_context=developer_context)
    tools = registry.build_tools_list(invocation_context=developer_context)

    assert "server_shell" in descriptions
    assert tools[0]["function"]["name"] == "server_shell"


def test_skill_registry_when_adapter_is_limited_then_only_matching_skills_are_visible():
    registry = SkillRegistry()
    registry.register(_skill("qq_image", adapter_types=["napcat"]))
    registry.register(_skill("discord_image", adapter_types=["discord"]))
    registry.register(_skill("plain_note"))

    descriptions = registry.build_tool_descriptions(adapter_type="napcat")

    assert "qq_image" in descriptions
    assert "plain_note" in descriptions
    assert "discord_image" not in descriptions


def test_skill_registry_when_adapter_is_unknown_then_adapter_limited_skills_are_hidden():
    registry = SkillRegistry()
    registry.register(_skill("qq_image", adapter_types=["napcat"]))
    registry.register(_skill("plain_note"))

    descriptions = registry.build_tool_descriptions()
    tools = registry.build_tools_list()

    assert "qq_image" not in descriptions
    assert "plain_note" in descriptions
    assert [tool["function"]["name"] for tool in tools] == ["plain_note"]


def test_skill_registry_when_skill_is_not_model_visible_then_tool_is_hidden():
    registry = SkillRegistry()
    registry.register(_skill("interaction"))
    registry.register(_skill("list_stickers", model_visible=False))

    descriptions = registry.build_tool_descriptions()
    tools = registry.build_tools_list()

    assert "interaction" in descriptions
    assert "list_stickers" not in descriptions
    assert [tool["function"]["name"] for tool in tools] == ["interaction"]


def test_skill_registry_when_workspace_hot_reloads_then_removed_skills_disappear():
    registry = SkillRegistry()
    registry.replace_all([_skill("old_skill"), _skill("keep_skill")])

    registry.replace_all([_skill("new_skill")])

    assert registry.skill_names == ["new_skill"]
    assert registry.get("old_skill") is None
    assert registry.get("new_skill") is not None


def test_skill_registry_when_skill_requires_admin_then_visible_only_for_admin_group():
    registry = SkillRegistry()
    registry.register(_skill("group_management", admin_required=True, adapter_types=["napcat"]))

    assert registry.build_tools_list(adapter_type="napcat", chat_type="group") == []
    assert (
        registry.build_tools_list(
            adapter_type="napcat",
            chat_type="private",
            admin_allowed=True,
        )
        == []
    )

    tools = registry.build_tools_list(
        adapter_type="napcat",
        chat_type="group",
        admin_allowed=True,
    )

    assert [tool["function"]["name"] for tool in tools] == ["group_management"]


def test_skill_registry_when_query_scopes_tools_then_ranked_results_are_bounded():
    registry = SkillRegistry()
    registry.register(_skill("weather_lookup", description="查询实时预报"))
    registry.register(_skill("forecast", description="查询天气", tags=["weather", "forecast"]))
    registry.register(_skill("web_search", description="搜索网页", tags=["search"]))

    tools = registry.build_tools_for_query("weather forecast", limit=2)

    assert [tool["function"]["name"] for tool in tools] == [
        "forecast",
        "weather_lookup",
    ]
    assert registry.build_tools_for_query("unrelated", limit=2) == []


def test_skill_registry_when_query_scopes_tools_then_existing_access_filters_apply():
    registry = SkillRegistry()
    registry.register(_skill("public_weather", description="天气查询"))
    registry.register(_skill("developer_weather", developer_only=True, description="天气查询"))
    registry.register(_skill("admin_weather", admin_required=True, description="天气查询"))
    registry.register(_skill("discord_weather", adapter_types=["discord"], description="天气查询"))
    registry.register(_skill("hidden_weather", description="天气查询", model_visible=False))
    user_context = SkillInvocationContext(caller=UnifiedUser(user_id="u1", name="普通用户"))

    tools = registry.build_tools_for_query(
        "天气",
        limit=5,
        invocation_context=user_context,
        adapter_type="napcat",
        chat_type="group",
    )

    assert [tool["function"]["name"] for tool in tools] == ["public_weather"]


def test_skill_registry_when_builtin_skills_load_then_composite_napcat_tools_are_visible(
    tmp_path: Path,
):
    registry = SkillRegistry()

    registry.load_from_directory(
        tmp_path / "skills",
        auto_install_deps=False,
        include_builtin=True,
    )
    skill = registry.get("chat_with_developer")
    interaction = registry.get("interaction")
    bash = registry.get("bash")
    file_upload = registry.get("file_upload")
    group_management = registry.get("group_management")
    tools = registry.build_tools_list(adapter_type="napcat")
    admin_tools = registry.build_tools_list(
        adapter_type="napcat", chat_type="group", admin_allowed=True
    )

    assert skill is not None
    assert skill.silent is True
    assert skill.adapter_types == ["napcat"]
    assert "私聊" in skill.description
    assert interaction is not None
    assert bash is not None
    assert [param.name for param in bash.config_parameters] == [
        "allowed_commands",
        "allow_write_commands",
        "allow_destructive_commands",
        "max_timeout_seconds",
        "max_output_chars",
    ]
    assert "allowed_commands" not in bash.to_tool_schema()["function"]["parameters"]["properties"]
    assert file_upload is not None
    assert group_management is not None
    for old_name in (
        "poke",
        "send_sticker",
        "send_image",
        "upload_file",
        "kick_member",
        "mute_member",
        "mute_all",
        "set_group_card",
    ):
        assert registry.get(old_name) is None
    assert registry.get("web_lookup").retry_safe is True
    assert registry.get("web_lookup").side_effect is SkillSideEffect.READ_ONLY
    assert group_management.side_effect is SkillSideEffect.DESTRUCTIVE
    assert [tool["function"]["name"] for tool in tools].count("interaction") == 1
    assert [tool["function"]["name"] for tool in tools].count("file_upload") == 1
    assert not any(tool["function"]["name"] == "send_sticker" for tool in tools)
    assert not any(tool["function"]["name"] == "send_image" for tool in tools)
    assert not any(tool["function"]["name"] == "upload_file" for tool in tools)
    assert any(tool["function"]["name"] == "group_management" for tool in admin_tools)
    assert not any(tool["function"]["name"] == "kick_member" for tool in admin_tools)
