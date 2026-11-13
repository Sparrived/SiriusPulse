"""人物与视觉资产：统一人物服务、受管视觉资产服务，以及按调用来源创建的权限上下文。

实现 :class:`sirius_pulse.tools.engine_context.PeopleAccess`。
"""

from __future__ import annotations

from typing import Any

from sirius_pulse.tools.models import ToolInvocationContext


class PeopleMixin:
    """人物与视觉资产：统一人物服务、受管视觉资产服务，以及按调用来源创建的权限上下文。"""

    _engine: Any

    def get_person_service(self) -> Any:
        return self._engine.person_service

    def get_visual_service(self) -> Any:
        return self._engine.visual_service

    def get_person_access(self, invocation_context: ToolInvocationContext | None) -> Any:
        from sirius_pulse.people import PersonAccess

        people = self.get_person_service()
        if invocation_context is None:
            return PersonAccess()
        autonomous = invocation_context.self_initiated
        actor_id = (
            people.persona_id
            if autonomous
            else people.person_id_for_user(invocation_context.caller_user_id) or ""
        )
        authors = {user.user_id for _, user in invocation_context.evidence_callers or ()}
        ambiguous = len(invocation_context.evidence_refs) > 1 and len(authors) != 1
        return PersonAccess(
            actor_id="" if ambiguous else actor_id,
            group_id=invocation_context.group_id,
            evidence_refs=() if autonomous else tuple(invocation_context.evidence_refs),
            is_admin=not autonomous and not ambiguous and invocation_context.caller_is_developer,
            self_initiated=autonomous,
            self_service=autonomous,
        )
