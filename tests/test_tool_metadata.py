"""Tool risk metadata tests."""

from sirius_pulse.tools.models import ToolDefinition, ToolParameter, ToolSideEffect


def test_risk_metadata_does_not_change_openai_tool_schema():
    tool = ToolDefinition(
        name="lookup",
        description="Read public data",
        parameters=[
            ToolParameter(name="query", type="str", description="Search query", required=True),
        ],
        side_effect=ToolSideEffect.READ_ONLY,
    )

    assert tool.to_tool_schema() == {
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
