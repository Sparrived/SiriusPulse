#!/usr/bin/env python
"""
Example: Event Memory V2 — Buffer-based Batch Observation Extraction

Demonstrates the V2 event memory strategy:
1. Messages are buffered per user (no LLM calls on each message)
2. When buffer reaches batch_size, LLM batch-extracts user observations
3. Observations are categorized and directly written to user memory

This avoids the V1 problem of generating an event for nearly every message.
"""

import asyncio

from sirius_pulse.memory import EventMemoryManager
from sirius_pulse.memory.event import OBSERVATION_CATEGORIES
from sirius_pulse.providers.base import GenerationRequest


async def demo_buffer_and_extract():
    """
    Demo: How V2 buffer-based extraction works.

    Scenario:
    - User sends several messages
    - Messages are buffered until batch_size is reached
    - LLM extracts structured observations in one call
    """

    event_manager = EventMemoryManager()
    user_id = "user_alice"
    batch_size = 3

    messages = [
        "I really love Italian coffee, especially espresso",
        "My sister just moved to Tokyo last week",
        "I've been trying to learn piano for two months",
        "Espresso is my daily morning ritual",
    ]

    print("1. Buffering messages (no LLM calls)...")
    for msg in messages:
        event_manager.buffer_message(user_id=user_id, content=msg)
        ready = event_manager.should_extract(user_id=user_id, batch_size=batch_size)
        print(f"  buffered: '{msg[:50]}' | ready={ready}")

    # Mock provider that returns structured observations
    class DemoAsyncProvider:
        async def generate_async(self, request: GenerationRequest) -> str:
            return """[
                {"category": "preference", "content": "loves Italian coffee, especially espresso", "confidence": 0.9},
                {"category": "relationship", "content": "has a sister who recently moved to Tokyo", "confidence": 0.8},
                {"category": "goal", "content": "learning piano, two months in", "confidence": 0.7}
            ]"""

    print("\n2. Extracting observations (single LLM call)...")
    new_entries = await event_manager.extract_observations(
        user_id=user_id,
        user_name="Alice",
        provider_async=DemoAsyncProvider(),
        model_name="gpt-4o-mini",
    )

    print(f"   Extracted {len(new_entries)} observations:\n")
    for entry in new_entries:
        print(f"   [{entry.category}] {entry.summary} (confidence={entry.confidence})")
        print(f"     evidence: {entry.evidence}")

    # Query observations for a user
    print("\n3. Querying observations by user...")
    user_obs = event_manager.get_user_observations(user_id)
    print(f"   {user_id} has {len(user_obs)} observations")

    # Check relevance of new message against existing observations
    print("\n4. Checking relevance of new content...")
    hit = event_manager.check_relevance(user_id=user_id, content="I had a great espresso today")
    if hit:
        print(f"   Relevant! level={hit['level']}, score={hit['score']:.2f}")
    else:
        print("   No relevant observations found")


def demo_serialization():
    """
    Demo: V2 serialization with automatic V1 migration.
    """
    event_manager = EventMemoryManager()
    event_manager.buffer_message(user_id="u1", content="test message")

    # Serialize
    data = event_manager.to_dict()
    print("\n5. Serialized format:")
    print(f"   version: {data['version']}")
    print(f"   entries: {len(data['entries'])}")

    # Deserialize (also handles V1 auto-migration)
    restored = EventMemoryManager.from_dict(data)
    print(f"   Restored {len(restored.entries)} entries")

    # V1 data is auto-migrated
    v1_data = {
        "entries": [{
            "event_id": "legacy_001",
            "summary": "old event",
            "keywords": ["test"],
            "role_slots": [],
            "entities": ["Alice"],
            "time_hints": ["yesterday"],
            "emotion_tags": ["happy"],
            "hit_count": 5,
            "verified": True,
            "first_seen": "2025-01-01T00:00:00",
            "last_seen": "2025-01-01T00:00:00",
            "mention_count": 5,
        }]
    }
    migrated = EventMemoryManager.from_dict(v1_data)
    entry = migrated.entries[0]
    print(f"\n6. V1 auto-migration:")
    print(f"   event_id={entry.event_id}, category={entry.category}")
    print(f"   confidence={entry.confidence}, evidence={entry.evidence}")


def demo_categories():
    """
    Demo: Available observation categories.
    """
    print("\n7. Observation categories:")
    for cat in sorted(OBSERVATION_CATEGORIES):
        print(f"   - {cat}")


if __name__ == "__main__":
    print("=" * 60)
    print("Event Memory V2 — Batch Observation Extraction Demo")
    print("=" * 60 + "\n")

    asyncio.run(demo_buffer_and_extract())
    demo_serialization()
    demo_categories()
    
    print("\n" + "=" * 60)
    print("Integration points:")
    print("- Call event_manager.finalize_pending_events() after:")
    print("  - Session ends (batch verification)")
    print("  - Every N messages (incremental verification)")
    print("  - Before exporting top_events for context")
    print("=" * 60)
