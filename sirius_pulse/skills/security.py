"""Skill security helpers for developer-gated tool execution."""

from __future__ import annotations

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.models import Transcript
from sirius_pulse.skills.models import SkillDefinition, SkillInvocationContext


def build_skill_invocation_context(
    *,
    transcript: Transcript,
    caller: UnifiedUser | None,
) -> SkillInvocationContext:
    """Build per-turn invocation context used for tool visibility and auth."""
    caller_profile = _to_user_profile(caller)
    developer_profiles = collect_declared_developer_profiles(
        transcript=transcript,
        caller=caller_profile,
    )
    return SkillInvocationContext(
        caller=caller_profile,
        developer_profiles=developer_profiles,
    )


def collect_declared_developer_profiles(
    *,
    transcript: Transcript,
    caller: UnifiedUser | None = None,
) -> list[UnifiedUser]:
    """Collect explicitly declared developer profiles from transcript state."""
    developers: list[UnifiedUser] = []
    seen: set[str] = set()

    for group_entries in transcript.user_memory.entries.values():
        for entry in group_entries.values():
            profile = entry.profile
            if not profile.is_developer:
                continue
            if profile.user_id in seen:
                continue
            developers.append(profile)
            seen.add(profile.user_id)

    if caller is not None and caller.is_developer and caller.user_id not in seen:
        developers.append(caller)

    return developers


def validate_skill_access(
    *,
    skill: SkillDefinition,
    invocation_context: SkillInvocationContext | None,
) -> str:
    """Return an error message when the caller is not allowed to run the skill."""
    if not skill.developer_only:
        return ""

    if invocation_context is None:
        return (
            f"SKILL '{skill.name}' 仅允许 developer 调用，但当前调用未提供开发者上下文。"
        )

    if not invocation_context.has_declared_developer:
        return (
            f"SKILL '{skill.name}' 仅允许 developer 调用。"
            "当前会话尚未显式声明 developer 用户，请在 UnifiedUser.metadata 中设置 is_developer=true。"
        )

    if invocation_context.caller_is_developer:
        return ""

    caller_name = invocation_context.caller_name or "当前用户"
    return f"SKILL '{skill.name}' 仅允许 developer 调用，{caller_name} 未被标记为 developer。"


def ensure_developer_access(
    *,
    skill_name: str,
    invocation_context: SkillInvocationContext | None,
) -> None:
    """Raise PermissionError when the current caller is not a developer."""
    error = validate_skill_access(
        skill=SkillDefinition(name=skill_name, description="", developer_only=True),
        invocation_context=invocation_context,
    )
    if error:
        raise PermissionError(error)


def _to_user_profile(caller: UnifiedUser | None) -> UnifiedUser | None:
    return caller