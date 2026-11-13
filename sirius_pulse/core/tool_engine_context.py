"""``ToolEngineContext`` 的实现：把引擎按领域适配给工具。

实现按领域拆在 :mod:`sirius_pulse.core.tool_context` 里，每个领域一个 mixin，
对应 :mod:`sirius_pulse.tools.engine_context` 里的一个小接口；这里只负责组合。
"""

from __future__ import annotations

from typing import Any

from sirius_pulse.core.tool_context.agent_turns import AgentTurnMixin
from sirius_pulse.core.tool_context.conversation import ConversationMixin
from sirius_pulse.core.tool_context.delivery import DeliveryMixin
from sirius_pulse.core.tool_context.generation import GenerationMixin
from sirius_pulse.core.tool_context.life import LifeMixin
from sirius_pulse.core.tool_context.people import PeopleMixin
from sirius_pulse.core.tool_context.tooling import ToolingMixin
from sirius_pulse.core.tool_context.users import UserLookupMixin
from sirius_pulse.core.tool_context.workspace import WorkspaceMixin


class ToolEngineContextImpl(
    WorkspaceMixin,
    ToolingMixin,
    GenerationMixin,
    AgentTurnMixin,
    DeliveryMixin,
    ConversationMixin,
    LifeMixin,
    PeopleMixin,
    UserLookupMixin,
):
    """把 EmotionalGroupChatEngine 适配成 ``ToolEngineContext``。

    被动工具的 create_background_tasks / create_triggers、以及内置工具的
    ``engine_context`` 参数拿到的都是它；工具按需只依赖其中某个领域的小接口。
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        # 组合 UserLookupService
        from sirius_pulse.core.user_lookup import UserLookupService

        self._user_lookup = UserLookupService(
            identity_resolver=engine.identity_resolver,
            user_manager=engine.user_manager,
            engine=engine,
        )
