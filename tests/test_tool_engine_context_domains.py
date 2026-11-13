"""业务视角：工具拿到的引擎上下文按领域拆成小接口，但对工具作者来说什么都没少。

- 工作区里的被动工具按旧的完整上下文写的，拆分后照常能用；
- 工具只依赖某个领域的小接口时，真实上下文同样满足它；
- 以前只能用 getattr 试探的能力（表情包、群管理员判断、用户查询）现在是正式接口。
"""

from __future__ import annotations

import typing

import pytest

from sirius_pulse.core.tool_engine_context import ToolEngineContextImpl
from sirius_pulse.tools import engine_context as domains
from sirius_pulse.tools.models import ToolEngineContext

DOMAIN_INTERFACES = [
    domains.WorkspaceAccess,
    domains.ToolingAccess,
    domains.GenerationAccess,
    domains.AgentTurnAccess,
    domains.DeliveryAccess,
    domains.ConversationAccess,
    domains.LifeAccess,
    domains.PeopleAccess,
    domains.UserLookupAccess,
]

#: 拆分前 ToolEngineContext 声明过的全部能力：工作区里的被动工具可能用到其中任何一个。
LEGACY_CAPABILITIES = {
    "tool_registry",
    "tool_executor",
    "get_data_store",
    "generate_text",
    "generate_scheduled_message",
    "generate_image",
    "handle_background_tool_result",
    "queue_pending_message",
    "emit_event",
    "dispatch_proactive_message",
    "get_active_groups",
    "get_recent_messages",
    "run_autonomous_turn",
    "run_life_work_turn",
    "nudge_mood",
    "settle_mood",
    "mood_label",
    "list_audiences",
    "list_acquaintances",
    "deliver_share",
    "add_memory_unit",
    "get_config_value",
    "get_work_path",
    "get_expressiveness",
    "get_persona",
    "get_person_service",
    "get_visual_service",
    "get_person_access",
    "log_inner_thought",
    "schedule_notebook_maintenance",
    "add_memory_entry",
    "record_reply_timestamp",
    "persist_group_state",
    "get_tool_descriptions",
    "get_current_adapter_type",
    "activate_private_group",
}


def _capabilities(interface: type) -> set[str]:
    return {name for name in vars(interface) if not name.startswith("_")}


def test_tools_written_against_the_full_context_keep_working() -> None:
    full = {
        name
        for interface in ToolEngineContext.__mro__
        if interface not in (object, typing.Protocol, typing.Generic)
        for name in vars(interface)
        if not name.startswith("_")
    }

    assert LEGACY_CAPABILITIES <= full
    # 完整上下文恰好是各领域的合集：没有游离在领域之外的能力。
    assert full == set().union(*(_capabilities(i) for i in DOMAIN_INTERFACES))


@pytest.mark.parametrize("interface", DOMAIN_INTERFACES, ids=lambda i: i.__name__)
def test_the_real_context_satisfies_every_domain_interface(interface: type) -> None:
    missing = {
        name for name in _capabilities(interface) if not hasattr(ToolEngineContextImpl, name)
    }

    assert missing == set()


def test_domains_do_not_overlap() -> None:
    seen: dict[str, str] = {}
    for interface in DOMAIN_INTERFACES:
        for name in _capabilities(interface):
            assert name not in seen, f"{name} 同时属于 {seen[name]} 和 {interface.__name__}"
            seen[name] = interface.__name__


@pytest.mark.parametrize(
    ("capability", "interface"),
    [
        ("send_sticker_by_names", domains.DeliveryAccess),
        ("list_sticker_names", domains.DeliveryAccess),
        ("is_qq_bot_group_admin", domains.DeliveryAccess),
        ("find_user_by_platform_uid", domains.UserLookupAccess),
        ("get_bot_platform_uids", domains.UserLookupAccess),
    ],
)
def test_capabilities_once_reached_by_probing_are_now_declared(
    capability: str, interface: type
) -> None:
    assert capability in _capabilities(interface)
