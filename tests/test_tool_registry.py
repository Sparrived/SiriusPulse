"""工具注册中心面向工具作者和模型工具列表的业务行为测试。"""

from __future__ import annotations

from pathlib import Path

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.tools import (
    ToolDefinition,
    ToolInvocationContext,
    ToolRegistry,
    ToolSideEffect,
)


def _tool(
    name: str,
    *,
    description: str | None = None,
    developer_only: bool = False,
    admin_required: bool = False,
    adapter_types: list[str] | None = None,
    tags: list[str] | None = None,
    model_visible: bool = True,
) -> ToolDefinition:
    def run(**kwargs):
        return {"success": True, "text": name}

    return ToolDefinition(
        name=name,
        description=description or f"{name} tool",
        parameters=[],
        developer_only=developer_only,
        admin_required=admin_required,
        adapter_types=adapter_types or [],
        tags=tags or [],
        model_visible=model_visible,
        source_path=None,
        _run_func=run,
    )


def test_tool_registry_when_user_installs_tool_file_then_tool_becomes_callable(
    tmp_tool_dir: Path,
):
    registry = ToolRegistry()

    loaded = registry.load_from_directory(
        tmp_tool_dir,
        auto_install_deps=False,
        include_builtin=False,
    )
    tool = registry.get("test_hello")

    assert loaded == 1
    assert tool is not None
    assert tool.description == "给群友发送问候"
    assert tool.parameters[0].name == "name"
    assert tool.parameters[0].default == "世界"


def test_tool_registry_when_model_prompt_is_built_then_public_tools_are_described():
    registry = ToolRegistry()
    registry.register(_tool("search_web", description="搜索公开资料"))
    registry.register(_tool("read_file", description="读取工作区文件"))

    descriptions = registry.build_tool_descriptions(adapter_type="napcat")

    assert "search_web" in descriptions
    assert "搜索公开资料" in descriptions
    assert "read_file" in descriptions


def test_tool_registry_when_caller_is_not_developer_then_developer_tool_is_hidden():
    registry = ToolRegistry()
    registry.register(_tool("server_shell", developer_only=True))
    user_context = ToolInvocationContext(caller=UnifiedUser(user_id="u1", name="普通用户"))

    assert registry.build_tool_descriptions(invocation_context=user_context) == ""
    assert registry.build_tools_list(invocation_context=user_context) == []


def test_tool_registry_when_caller_is_developer_then_developer_tool_is_available():
    registry = ToolRegistry()
    registry.register(_tool("server_shell", developer_only=True))
    developer_context = ToolInvocationContext(
        caller=UnifiedUser(user_id="dev", name="开发者", metadata={"is_developer": True})
    )

    descriptions = registry.build_tool_descriptions(invocation_context=developer_context)
    tools = registry.build_tools_list(invocation_context=developer_context)

    assert "server_shell" in descriptions
    assert tools[0]["function"]["name"] == "server_shell"


def test_tool_registry_when_adapter_is_limited_then_only_matching_tools_are_visible():
    registry = ToolRegistry()
    registry.register(_tool("qq_image", adapter_types=["napcat"]))
    registry.register(_tool("discord_image", adapter_types=["discord"]))
    registry.register(_tool("plain_note"))

    descriptions = registry.build_tool_descriptions(adapter_type="napcat")

    assert "qq_image" in descriptions
    assert "plain_note" in descriptions
    assert "discord_image" not in descriptions


def test_tool_registry_when_adapter_is_unknown_then_adapter_limited_tools_are_hidden():
    registry = ToolRegistry()
    registry.register(_tool("qq_image", adapter_types=["napcat"]))
    registry.register(_tool("plain_note"))

    descriptions = registry.build_tool_descriptions()
    tools = registry.build_tools_list()

    assert "qq_image" not in descriptions
    assert "plain_note" in descriptions
    assert [tool["function"]["name"] for tool in tools] == ["plain_note"]


def test_tool_registry_when_tool_is_not_model_visible_then_tool_is_hidden():
    registry = ToolRegistry()
    registry.register(_tool("social_tool"))
    registry.register(_tool("list_stickers", model_visible=False))

    descriptions = registry.build_tool_descriptions()
    tools = registry.build_tools_list()

    assert "social_tool" in descriptions
    assert "list_stickers" not in descriptions
    assert [tool["function"]["name"] for tool in tools] == ["social_tool"]


def test_tool_registry_when_workspace_hot_reloads_then_removed_tools_disappear():
    registry = ToolRegistry()
    registry.replace_all([_tool("old_tool"), _tool("keep_tool")])

    registry.replace_all([_tool("new_tool")])

    assert registry.tool_names == ["new_tool"]
    assert registry.get("old_tool") is None
    assert registry.get("new_tool") is not None


def test_tool_registry_when_tool_requires_admin_then_visible_only_for_admin_group():
    registry = ToolRegistry()
    registry.register(_tool("group_management", admin_required=True, adapter_types=["napcat"]))

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


def test_tool_registry_when_builtin_tools_load_then_napcat_tools_are_visible(
    tmp_path: Path,
):
    registry = ToolRegistry()

    registry.load_from_directory(
        tmp_path / "tools",
        auto_install_deps=False,
        include_builtin=True,
    )
    tool = registry.get("interaction_with_master")
    bash = registry.get("bash")
    group_file_exec = registry.get("group_file_exec")
    group_management = registry.get("group_management")
    web_lookup = registry.get("web_lookup")
    read_skill = registry.get("read_skill")
    qq_like = registry.get("qq_like")
    workflow_state = registry.get("workflow_state")
    tools = registry.build_tools_list(adapter_type="napcat")
    regular_user_tools = registry.build_tools_list(
        adapter_type="napcat",
        invocation_context=ToolInvocationContext(caller=UnifiedUser(user_id="u1", name="普通用户")),
    )
    admin_tools = registry.build_tools_list(
        adapter_type="napcat", chat_type="group", admin_allowed=True
    )

    assert tool is not None
    assert tool.silent is False
    assert tool.adapter_types == []
    assert "主人" in tool.description
    assert bash is not None
    assert [param.name for param in bash.config_parameters] == [
        "max_timeout_seconds",
        "max_output_chars",
    ]
    assert bash.developer_only is False
    assert (
        "max_timeout_seconds" not in bash.to_tool_schema()["function"]["parameters"]["properties"]
    )
    assert any(tool["function"]["name"] == "bash" for tool in regular_user_tools)
    assert any(tool["function"]["name"] == "interaction_with_master" for tool in tools)
    assert registry.get("container_admin") is None
    assert [param.name for param in tool.config_parameters] == [
        "public_status_token",
        "base_url",
        "timeout_seconds",
    ]
    assert (
        "public_status_token" not in tool.to_tool_schema()["function"]["parameters"]["properties"]
    )
    assert [param.name for param in tool.parameters] == ["action", "message", "device_id"]
    assert registry.get("chat_with_developer") is None
    assert registry.get("developer_status") is None
    assert group_file_exec is not None
    assert group_management is not None
    assert web_lookup is not None
    assert read_skill is not None
    assert read_skill.retry_safe is True
    assert read_skill.side_effect is ToolSideEffect.READ_ONLY
    assert qq_like is not None
    assert qq_like.adapter_types == ["napcat"]
    assert qq_like.side_effect is ToolSideEffect.EXTERNAL_WRITE
    assert workflow_state is not None
    assert workflow_state.side_effect is ToolSideEffect.EXTERNAL_WRITE
    assert "list" in workflow_state.parameters[0].choices
    assert [param.name for param in workflow_state.parameters] == [
        "action",
        "key",
        "version",
        "step",
        "tool_name",
        "idempotency_key",
        "claim_token",
        "next_step",
        "lease_seconds",
        "expected_revision",
        "state_json",
        "summary",
        "error",
    ]
    assert workflow_state.parameters[1].required is False
    assert [param.name for param in web_lookup.config_parameters] == ["tavily_api_key"]
    assert (
        "tavily_api_key" not in web_lookup.to_tool_schema()["function"]["parameters"]["properties"]
    )
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
    assert registry.get("user_profile") is None
    assert web_lookup.retry_safe is True
    assert web_lookup.side_effect is ToolSideEffect.READ_ONLY
    assert group_management.side_effect is ToolSideEffect.DESTRUCTIVE
    assert not any(tool["function"]["name"] == "interaction" for tool in tools)
    assert [tool["function"]["name"] for tool in tools].count("group_file_exec") == 1
    assert not any(tool["function"]["name"] == "send_sticker" for tool in tools)
    assert not any(tool["function"]["name"] == "send_image" for tool in tools)
    assert not any(tool["function"]["name"] == "upload_file" for tool in tools)
    assert any(tool["function"]["name"] == "group_management" for tool in admin_tools)
    assert not any(tool["function"]["name"] == "kick_member" for tool in admin_tools)
