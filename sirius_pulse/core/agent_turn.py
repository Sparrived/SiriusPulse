"""Explicit lifecycle record for one group-chat agent turn."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentTurnPhase(Enum):
    OBSERVE = "observe"
    DECIDE = "decide"
    PLAN = "plan"
    ACT = "act"
    VERIFY = "verify"
    RESPOND = "respond"
    COMPLETE = "complete"


@dataclass(slots=True)
class AgentToolAttempt:
    tool_call_id: str
    skill_name: str
    side_effect: str
    status: str = "planned"
    summary: str = ""


@dataclass(slots=True)
class AgentTurn:
    """Small, in-memory audit trail for one delayed-response execution."""

    group_id: str
    item_ids: list[str]
    query: str
    turn_id: str = field(default_factory=lambda: f"turn_{uuid.uuid4().hex[:12]}")
    phase: AgentTurnPhase = AgentTurnPhase.OBSERVE
    phases: list[AgentTurnPhase] = field(default_factory=lambda: [AgentTurnPhase.OBSERVE])
    candidate_tool_names: list[str] = field(default_factory=list)
    tool_attempts: list[AgentToolAttempt] = field(default_factory=list)
    _action_fingerprints: set[str] = field(default_factory=set, repr=False)

    def advance(self, phase: AgentTurnPhase) -> None:
        self.phase = phase
        if not self.phases or self.phases[-1] is not phase:
            self.phases.append(phase)

    def set_candidates(self, names: list[str]) -> None:
        self.candidate_tool_names = list(dict.fromkeys(name for name in names if name))

    def begin_action(
        self,
        *,
        tool_call_id: str,
        skill_name: str,
        params: dict[str, Any],
        side_effect: str,
        deduplicate: bool,
    ) -> bool:
        fingerprint = _action_fingerprint(skill_name, params)
        if deduplicate and fingerprint in self._action_fingerprints:
            self.tool_attempts.append(
                AgentToolAttempt(tool_call_id, skill_name, side_effect, "duplicate")
            )
            return False
        if deduplicate:
            self._action_fingerprints.add(fingerprint)
        self.tool_attempts.append(
            AgentToolAttempt(tool_call_id, skill_name, side_effect, "running")
        )
        return True

    def finish_action(self, tool_call_id: str, *, success: bool, summary: str = "") -> None:
        for attempt in reversed(self.tool_attempts):
            if attempt.tool_call_id == tool_call_id:
                attempt.status = "success" if success else "failed"
                attempt.summary = summary[:500]
                return

    def deny_action(
        self, tool_call_id: str, skill_name: str, side_effect: str, summary: str
    ) -> None:
        self.tool_attempts.append(
            AgentToolAttempt(tool_call_id, skill_name, side_effect, "denied", summary[:500])
        )

    def to_event_data(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "group_id": self.group_id,
            "item_ids": list(self.item_ids),
            "phase": self.phase.value,
            "phases": [phase.value for phase in self.phases],
            "candidate_tool_names": list(self.candidate_tool_names),
            "tool_attempts": [
                {
                    "tool_call_id": attempt.tool_call_id,
                    "skill_name": attempt.skill_name,
                    "side_effect": attempt.side_effect,
                    "status": attempt.status,
                    "summary": attempt.summary,
                }
                for attempt in self.tool_attempts
            ],
        }


def _action_fingerprint(skill_name: str, params: dict[str, Any]) -> str:
    try:
        rendered = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = repr(params)
    return hashlib.sha256(f"{skill_name}\0{rendered}".encode()).hexdigest()
