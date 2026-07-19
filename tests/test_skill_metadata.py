"""Skill risk metadata tests."""

from sirius_pulse.skills.models import SkillDefinition, SkillParameter, SkillSideEffect


def test_skill_definition_defaults_to_conservative_risk_metadata():
    skill = SkillDefinition(name="status", description="Read current status")

    assert skill.side_effect is SkillSideEffect.UNKNOWN


def test_skill_definition_allows_risk_metadata_overrides():
    skill = SkillDefinition(
        name="lookup",
        description="Read public data",
        side_effect=SkillSideEffect.READ_ONLY,
    )

    assert skill.side_effect is SkillSideEffect.READ_ONLY


def test_risk_metadata_does_not_change_openai_tool_schema():
    skill = SkillDefinition(
        name="lookup",
        description="Read public data",
        parameters=[
            SkillParameter(name="query", type="str", description="Search query", required=True),
        ],
        side_effect=SkillSideEffect.READ_ONLY,
    )

    assert skill.to_tool_schema() == {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Read public data",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    }
                },
                "required": ["query"],
            },
        },
    }
