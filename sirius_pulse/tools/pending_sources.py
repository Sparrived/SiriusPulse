"""Resolve framework-admitted pending instructions without trusting prompt labels.

The queue owns which evidence refs entered this turn.  This module only resolves
those exact platform accounts and narrows a tool invocation to one real author.
It deliberately cannot mint evidence from arbitrary history or XML content.
"""

from __future__ import annotations

import logging
from typing import Any

from sirius_pulse.memory.user.unified_models import UnifiedUser

logger = logging.getLogger(__name__)


def pending_evidence_callers(
    engine: Any, group_id: str, evidence_refs: list[str]
) -> tuple[tuple[str, UnifiedUser], ...]:
    """Resolve admitted refs to copied users using exact native-account mappings."""
    people = getattr(engine, "person_service", None)
    users = getattr(engine, "user_manager", None)
    if people is None or users is None:
        return ()
    result: list[tuple[str, UnifiedUser]] = []
    for ref in evidence_refs:
        try:
            evidence = people.get_evidence(ref)
            if evidence.get("group_id") != group_id:
                continue
            platform, uid = evidence.get("platform"), evidence.get("platform_uid")
            if not platform or not uid:
                continue
            user_id = users.resolve_user_id(platform=platform, external_uid=uid)
            if not user_id or people.person_id_for_user(user_id) != evidence.get("actor_id"):
                continue
            profile = users.get_user(user_id, group_id)
            if profile is not None:
                result.append(
                    (
                        ref,
                        UnifiedUser(
                            user_id=profile.user_id,
                            name=profile.name,
                            identities=dict(profile.identities),
                            metadata=dict(profile.metadata),
                        ),
                    )
                )
        except Exception:
            logger.warning("待处理人物指令来源不可用；不授予操作权限", exc_info=True)
    return tuple(result)
