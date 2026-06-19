from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from sirius_pulse.adapters.base import BaseAdapter
from sirius_pulse.adapters.models import MessageGroup, ParsedEvent
from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.models import Transcript
from sirius_pulse.platforms.onebot_v11.protocol import (
    build_image_label,
    dedupe_image_name,
    extract_image_name,
    extract_image_urls,
    extract_sender_names,
    extract_text_from_segments,
    sanitize_image_name,
)
from sirius_pulse.providers.base import GenerationRequest, GenerationResult
from sirius_pulse.providers.mock import MockProvider
from sirius_pulse.skills.models import SkillDefinition, SkillInvocationContext
from sirius_pulse.skills.security import (
    build_skill_invocation_context,
    collect_declared_developer_profiles,
    ensure_developer_access,
    validate_skill_access,
)
from sirius_pulse.skills.telemetry import SkillExecutionRecord, SkillTelemetry


class RecordingAdapter(BaseAdapter):
    adapter_type = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def parse_event(self, raw_event: dict[str, Any]) -> ParsedEvent | None:
        return ParsedEvent(prompt=str(raw_event.get("prompt", "")))

    async def send_group_message(
        self, group_id: str, message: MessageGroup | str
    ) -> dict[str, Any]:
        return {"group_id": group_id, "message": message}

    async def send_private_message(
        self, user_id: str, message: MessageGroup | str
    ) -> dict[str, Any]:
        return {"user_id": user_id, "message": message}

    async def call_api(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, params))
        return {"action": action, "params": params}


@pytest.mark.asyncio
async def test_base_adapter_when_default_api_methods_are_called_then_actions_and_params_are_forwarded():
    adapter = RecordingAdapter()

    assert await adapter.delete_message("42") == {
        "action": "delete_msg",
        "params": {"message_id": 42},
    }
    await adapter.set_group_kick("100", "200", reject_add_request=True)
    await adapter.set_group_ban("100", "200", duration=60)
    await adapter.set_group_whole_ban("100", enable=False)
    await adapter.set_group_admin("100", "200", enable=False)
    await adapter.set_group_card("100", "200", card="Card")
    await adapter.set_group_name("100", "Group")
    await adapter.send_poke("200", "100")

    assert adapter.calls == [
        ("delete_msg", {"message_id": 42}),
        ("set_group_kick", {"group_id": 100, "user_id": 200, "reject_add_request": True}),
        ("set_group_ban", {"group_id": 100, "user_id": 200, "duration": 60}),
        ("set_group_whole_ban", {"group_id": 100, "enable": False}),
        ("set_group_admin", {"group_id": 100, "user_id": 200, "enable": False}),
        ("set_group_card", {"group_id": 100, "user_id": 200, "card": "Card"}),
        ("set_group_name", {"group_id": 100, "group_name": "Group"}),
        ("group_poke", {"group_id": 100, "user_id": 200}),
    ]


@pytest.mark.asyncio
async def test_base_adapter_when_optional_defaults_are_used_then_safe_empty_values_are_returned(
    tmp_path,
):
    adapter = RecordingAdapter()
    old_file = tmp_path / "old.bin"
    new_file = tmp_path / "new.bin"
    url_file = tmp_path / "old.bin.url"
    old_file.write_text("old", encoding="utf-8")
    new_file.write_text("new", encoding="utf-8")
    url_file.write_text("source", encoding="utf-8")
    os.utime(old_file, (1, 1))
    os.utime(new_file, (2, 2))

    await adapter._cleanup_cache(tmp_path, max_files=1)

    assert await adapter.get_group_member_list("100") == []
    assert await adapter.get_group_member_info("100", "200") == {}
    assert await adapter.get_group_info("100") == {}
    assert await adapter.get_group_msg_history("100") == []
    assert await adapter.get_login_info() == {}
    assert await adapter.upload_group_file("100", "file.txt") == {}
    assert await adapter.upload_private_file("200", "file.txt") == {}
    assert await adapter.cache_image("file:///local/image.png") == "file:///local/image.png"
    assert "User-Agent" in adapter._cache_image_headers()
    assert old_file.exists() is False
    assert new_file.exists() is True
    assert url_file.exists() is True


def test_onebot_protocol_when_segments_are_parsed_then_text_images_and_sender_fields_are_extracted():
    message = [
        {"type": "text", "data": {"text": "hello "}},
        {"type": "face", "data": {"id": "95"}},
        {"type": "text", "data": {"text": " world"}},
        {"type": "image", "data": {"url": "https://example.test/a.png"}},
        {"type": "image", "data": {"file": "local.png"}},
    ]
    event = {"sender": {"nickname": " Ada ", "card": " Card "}}

    text = extract_text_from_segments(message)

    assert text.startswith("hello")
    assert "OK" in text
    assert text.endswith("world")
    assert extract_image_urls(message) == ["https://example.test/a.png", "local.png"]
    assert extract_sender_names(event) == ("Ada", "Card")


def test_onebot_protocol_when_image_names_are_normalized_then_duplicates_and_fallbacks_are_stable():
    counter: dict[str, int] = {}

    assert sanitize_image_name("'folder%20name/[photo].png\n'") == "folder name/(photo).png"
    assert (
        extract_image_name({"data": {"url": "https://example.test/images/pic.png?x=1"}}, 1)
        == "pic.png"
    )
    assert (
        extract_image_name({"data": {"file": "data:image/png;base64,abcd"}}, 2, "fallback")
        == "fallback_2"
    )
    assert dedupe_image_name("pic.png", counter) == "pic.png"
    assert dedupe_image_name("pic.png", counter) == "pic#2.png"
    label = build_image_label({"data": {"filename": "pic.png"}}, 3, "image", counter)
    assert "image" in label
    assert "pic#3.png" in label


@pytest.mark.asyncio
async def test_mock_provider_when_responses_are_configured_then_requests_are_recorded_in_order():
    provider = MockProvider(["first", "second"])
    request = GenerationRequest(
        model="mock-model",
        system_prompt="system",
        messages=[{"role": "user", "content": "hello"}],
        temperature=0.2,
        max_tokens=12,
    )

    first = await provider.generate_async(request)
    second = await provider.generate_async(request, return_reasoning=True)
    fallback = await provider.generate_async(request)

    assert isinstance(first, GenerationResult)
    assert first.content == "first"
    assert second.content == "second"
    assert fallback.content == "[mock] no configured response"
    assert provider.requests == [request, request, request]


def test_skill_security_when_context_is_built_then_declared_developers_are_collected(tmp_path):
    transcript = Transcript()
    dev = UnifiedUser(user_id="dev-1", name="Dev", metadata={"is_developer": True})
    caller = UnifiedUser(user_id="caller-1", name="Caller", metadata={"is_developer": True})
    transcript.user_memory.register_user(dev, group_id="group-1")

    developers = collect_declared_developer_profiles(transcript=transcript)
    context = build_skill_invocation_context(transcript=transcript, caller=caller)

    assert [item.user_id for item in developers] == ["dev-1"]
    assert [item.user_id for item in context.developer_profiles] == ["dev-1", "caller-1"]
    assert context.caller_is_developer is True
    assert context.caller_name == "Caller"
    assert context.caller_user_id == "caller-1"


def test_skill_security_when_developer_only_skill_is_validated_then_access_depends_on_context():
    public_skill = SkillDefinition(name="public", description="")
    developer_skill = SkillDefinition(name="server_shell", description="", developer_only=True)
    developer = UnifiedUser(user_id="dev", name="Dev", metadata={"is_developer": True})
    user = UnifiedUser(user_id="user", name="User", metadata={})

    assert validate_skill_access(skill=public_skill, invocation_context=None) == ""
    assert "developer" in validate_skill_access(skill=developer_skill, invocation_context=None)
    assert "developer" in validate_skill_access(
        skill=developer_skill,
        invocation_context=SkillInvocationContext(caller=user),
    )
    assert "User" in validate_skill_access(
        skill=developer_skill,
        invocation_context=SkillInvocationContext(caller=user, developer_profiles=[developer]),
    )
    assert (
        validate_skill_access(
            skill=developer_skill,
            invocation_context=SkillInvocationContext(
                caller=developer, developer_profiles=[developer]
            ),
        )
        == ""
    )
    with pytest.raises(PermissionError):
        ensure_developer_access(
            skill_name="server_shell",
            invocation_context=SkillInvocationContext(caller=user, developer_profiles=[developer]),
        )


def test_skill_telemetry_when_records_are_written_then_query_filters_and_summary_are_stable(
    tmp_path: Path,
):
    path = tmp_path / "skill_data" / ".telemetry.jsonl"
    telemetry = SkillTelemetry(path)
    telemetry.record(SkillExecutionRecord("alpha", 1.0, True, 10.0, params={"q": "a"}))
    telemetry.record(SkillExecutionRecord("beta", 2.0, False, 20.0, error="failed"))
    telemetry.record(SkillExecutionRecord("alpha", 3.0, False, 30.0, error="later"))
    path.write_text(path.read_text(encoding="utf-8") + "{bad-json\n", encoding="utf-8")

    latest, total = telemetry.query(limit=2)
    alpha_only, alpha_total = telemetry.query(skill_name="alpha")
    failures, failure_total = telemetry.query(success=False, since=2.0)
    empty_page, empty_total = telemetry.query(offset=99)
    summary = telemetry.summary()

    assert [item.skill_name for item in latest] == ["beta", "alpha"]
    assert total == 3
    assert [item.timestamp for item in alpha_only] == [1.0, 3.0]
    assert alpha_total == 2
    assert [item.error for item in failures] == ["failed", "later"]
    assert failure_total == 2
    assert empty_page == []
    assert empty_total == 3
    assert summary["alpha"]["calls"] == 2
    assert summary["alpha"]["successes"] == 1
    assert summary["alpha"]["failures"] == 1
    assert summary["alpha"]["avg_ms"] == 20.0
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["params"] == {"q": "a"}
