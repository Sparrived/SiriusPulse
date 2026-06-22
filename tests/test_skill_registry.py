"""技能注册中心面向技能作者和模型工具列表的业务行为测试。"""

from __future__ import annotations

from pathlib import Path

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.skills import SkillDefinition, SkillInvocationContext, SkillRegistry


def _skill(
    name: str,
    *,
    description: str | None = None,
    developer_only: bool = False,
    admin_required: bool = False,
    adapter_types: list[str] | None = None,
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
    registry.register(_skill("send_sticker"))
    registry.register(_skill("list_stickers", model_visible=False))

    descriptions = registry.build_tool_descriptions()
    tools = registry.build_tools_list()

    assert "send_sticker" in descriptions
    assert "list_stickers" not in descriptions
    assert [tool["function"]["name"] for tool in tools] == ["send_sticker"]


def test_skill_registry_when_workspace_hot_reloads_then_removed_skills_disappear():
    registry = SkillRegistry()
    registry.replace_all([_skill("old_skill"), _skill("keep_skill")])

    registry.replace_all([_skill("new_skill")])

    assert registry.skill_names == ["new_skill"]
    assert registry.get("old_skill") is None
    assert registry.get("new_skill") is not None


def test_skill_registry_when_skill_requires_admin_then_visible_only_for_admin_group():
    registry = SkillRegistry()
    registry.register(_skill("mute_member", admin_required=True, adapter_types=["napcat"]))

    assert registry.build_tools_list(adapter_type="napcat", chat_type="group") == []
    assert registry.build_tools_list(
        adapter_type="napcat",
        chat_type="private",
        admin_allowed=True,
    ) == []

    tools = registry.build_tools_list(
        adapter_type="napcat",
        chat_type="group",
        admin_allowed=True,
    )

    assert [tool["function"]["name"] for tool in tools] == ["mute_member"]
