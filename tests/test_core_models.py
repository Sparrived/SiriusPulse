from __future__ import annotations

from sirius_pulse.config import TokenUsageRecord
from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.models.intent_v3 import IntentAnalysisV3, SocialIntent
from sirius_pulse.models.models import Message, ReplyRuntimeState, Transcript
from sirius_pulse.models.persona import PersonaProfile


def _token_record() -> TokenUsageRecord:
    return TokenUsageRecord(
        actor_id="u1",
        task_name="response_generate",
        model="model-a",
        prompt_tokens=3,
        completion_tokens=2,
        total_tokens=5,
    )


def test_message_when_created_or_added_then_trims_only_trailing_spaces_and_newlines():
    assert Message(role="human", content=" hello \n").content == " hello"

    transcript = Transcript()
    message = Message(role="human", content="value\t \n")
    transcript.add(message)

    assert transcript.messages[0].content == "value\t"


def test_transcript_when_serialized_then_restores_messages_memory_runtime_and_token_usage():
    transcript = Transcript(session_summary="summary", orchestration_stats={"task": {"calls": 1}})
    transcript.add(
        Message(
            role="human",
            content="hello",
            speaker="Alice",
            channel="qq",
            channel_user_id="1001",
            group_id="g1",
            multimodal_inputs=[{"type": "image", "value": "a.png"}],
        )
    )
    transcript.reply_runtime = ReplyRuntimeState(
        user_last_turn_at={"u1": "t1"},
        group_recent_turn_timestamps=["t1"],
        last_assistant_reply_at="t2",
        assistant_reply_timestamps=["t2"],
    )
    transcript.remember_participant(
        participant=UnifiedUser(user_id="u1", name="Alice", identities={"qq": "1001"}),
        group_id="g1",
    )
    transcript.add_token_usage_record(_token_record())

    restored = Transcript.from_dict(transcript.to_dict())

    assert restored.messages == transcript.messages
    assert restored.reply_runtime == transcript.reply_runtime
    assert restored.session_summary == "summary"
    assert restored.orchestration_stats == {"task": {"calls": 1}}
    assert restored.token_usage_records == [_token_record()]
    assert restored.find_user_by_channel_uid(channel="qq", uid="1001", group_id="g1").name == "Alice"


def test_transcript_when_old_participant_memory_payload_is_loaded_then_builds_user_memory():
    restored = Transcript.from_dict(
        {
            "messages": [],
            "participant_memories": {
                "u1": {
                    "name": "Alice",
                    "persona": "tester",
                    "recent_messages": ["hello"],
                }
            },
        }
    )

    user = restored.user_memory.get_user("u1")

    assert user is not None
    assert user.name == "Alice"
    assert user.persona == "tester"


def test_transcript_when_compressed_then_archives_old_messages_into_summary():
    transcript = Transcript()
    transcript.add(Message(role="human", content="first message", speaker="Alice"))
    transcript.add(Message(role="assistant", content="second message", speaker="Bot"))
    transcript.add(Message(role="human", content="third message", speaker="Bob"))

    transcript.compress_for_budget(max_messages=2, max_chars=1000)

    assert len(transcript.messages) == 2
    assert "Alice" in transcript.session_summary
    assert "first message" in transcript.session_summary


def test_transcript_when_history_is_rendered_then_speaker_and_multimodal_text_are_included():
    transcript = Transcript()
    transcript.add(
        Message(
            role="human",
            content="hello",
            speaker="Alice",
            multimodal_inputs=[{"type": "image", "value": "a.png"}],
        )
    )
    transcript.add(Message(role="assistant", content="plain"))

    history = transcript.as_chat_history()

    assert history[0]["role"] == "human"
    assert "Alice" in history[0]["content"]
    assert "hello" in history[0]["content"]
    assert "a.png" in history[0]["content"]
    assert history[1] == {"role": "assistant", "content": "plain"}


def test_intent_analysis_when_serialized_then_preserves_plugin_fields_and_unknown_intent_falls_back():
    intent = IntentAnalysisV3(
        social_intent=SocialIntent.PLUGIN_COMMAND,
        intent_subtype="weather",
        urgency_score=80,
        plugin_intent="weather",
        plugin_confidence=0.9,
        plugin_slots={"city": "Shanghai"},
        plugin_render_mode="llm",
    )

    restored = IntentAnalysisV3.from_dict(intent.to_dict())
    fallback = IntentAnalysisV3.from_dict({"social_intent": "not-real"})

    assert restored.social_intent == SocialIntent.PLUGIN_COMMAND
    assert restored.plugin_slots == {"city": "Shanghai"}
    assert restored.plugin_render_mode == "llm"
    assert fallback.social_intent == SocialIntent.SOCIAL


def test_persona_profile_when_serialized_then_nested_preferences_round_trip():
    profile = PersonaProfile(
        name="Sirius",
        aliases=["Star"],
        persona_summary="summary",
        personality_traits=["curious"],
        emotional_baseline={"valence": 0.4, "arousal": 0.5},
        emotional_range={"min_valence": -0.2, "max_valence": 0.9},
        max_tokens_preference=64,
        temperature_preference=0.3,
        source="manual",
    )

    restored = PersonaProfile.from_dict(profile.to_dict())

    assert restored == profile


def test_persona_profile_when_full_system_prompt_is_set_then_builder_returns_it_directly():
    profile = PersonaProfile(name="Sirius", full_system_prompt="use this exact prompt")

    assert profile.build_system_prompt() == "use this exact prompt"
