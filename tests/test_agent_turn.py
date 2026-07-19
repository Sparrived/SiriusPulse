from sirius_pulse.core.agent_turn import AgentTurn, AgentTurnPhase


def test_agent_turn_tracks_lifecycle_and_deduplicates_side_effects():
    turn = AgentTurn(group_id="g1", item_ids=["item-1"], query="confirm action")

    assert turn.begin_action(
        tool_call_id="call-1",
        skill_name="group_management",
        params={"user_id": "42", "duration": 60},
        side_effect="destructive",
        deduplicate=True,
    )
    turn.finish_action("call-1", success=True, summary="muted")
    assert not turn.begin_action(
        tool_call_id="call-2",
        skill_name="group_management",
        params={"duration": 60, "user_id": "42"},
        side_effect="destructive",
        deduplicate=True,
    )

    turn.advance(AgentTurnPhase.ACT)
    turn.advance(AgentTurnPhase.VERIFY)
    turn.advance(AgentTurnPhase.COMPLETE)
    event = turn.to_event_data()

    assert event["phase"] == "complete"
    assert [attempt["status"] for attempt in event["tool_attempts"]] == ["success", "duplicate"]
