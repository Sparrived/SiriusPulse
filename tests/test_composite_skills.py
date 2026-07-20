from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from sirius_pulse.skills.builtin import file_upload, interaction


class _Adapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def send_poke(self, user_id: str, group_id: str) -> dict[str, Any]:
        self.calls.append(("send_poke", (user_id, group_id)))
        return {"ok": True}

    async def delete_message(self, message_id: str) -> dict[str, Any]:
        self.calls.append(("delete_message", (message_id,)))
        return {"ok": True}

    async def send_group_msg(self, group_id: str, message: Any) -> dict[str, Any]:
        self.calls.append(("send_group_msg", (group_id, message)))
        return {"data": {"message_id": 7}}

    async def send_private_msg(self, user_id: str, message: Any) -> dict[str, Any]:
        self.calls.append(("send_private_msg", (user_id, message)))
        return {"data": {"message_id": 9}}

    async def upload_group_file(
        self, group_id: str, file_path: str, file_name: str
    ) -> dict[str, Any]:
        self.calls.append(("upload_group_file", (group_id, file_path, file_name)))
        return {"data": {"message_id": 8}}

    async def upload_private_file(
        self, user_id: str, file_path: str, file_name: str
    ) -> dict[str, Any]:
        self.calls.append(("upload_private_file", (user_id, file_path, file_name)))
        return {"data": {"message_id": 10}}


class _StickerContext:
    def list_sticker_names(self) -> list[str]:
        return ["开心"]

    async def send_sticker_by_names(self, group_id: str, names: list[str]) -> dict[str, Any]:
        return {"success": True, "sticker_name": names[0], "group_id": group_id}


@pytest.mark.asyncio
async def test_interaction_runs_poke_and_records_action():
    adapter = _Adapter()

    result = await interaction.run(
        action="poke",
        user_id=1001,
        bridge=adapter,
        chat_context={"chat_type": "group", "chat_id": "9001"},
    )

    assert result["success"] is True
    assert result["internal_metadata"]["interaction_action"] == "poke"
    assert adapter.calls == [("send_poke", ("1001", "9001"))]


@pytest.mark.asyncio
async def test_interaction_runs_sticker_with_engine_context():
    result = await interaction.run(
        action="sticker",
        names=["开心"],
        chat_context={"group_id": "9001"},
        engine_context=_StickerContext(),
    )

    assert result["success"] is True
    assert result["internal_metadata"]["interaction_action"] == "sticker"
    assert result["summary"] == "已发送表情包：开心"


@pytest.mark.asyncio
async def test_interaction_runs_recall_and_records_action():
    adapter = _Adapter()

    result = await interaction.run(action="recall", message_id=42, bridge=adapter)

    assert result["success"] is True
    assert result["internal_metadata"]["interaction_action"] == "recall"
    assert adapter.calls == [("delete_message", ("42",))]


@pytest.mark.asyncio
async def test_file_upload_sends_image_and_uploads_file(tmp_path: Path):
    adapter = _Adapter()
    file_path = tmp_path / "report.pdf"
    file_path.write_text("report", encoding="utf-8")
    context = {"chat_type": "group", "chat_id": "9001"}

    image_result = await file_upload.run(
        action="image",
        image_path="https://example.test/image.png",
        bridge=adapter,
        chat_context=context,
    )
    file_result = await file_upload.run(
        action="file",
        file_path=str(file_path),
        file_name="report.pdf",
        bridge=adapter,
        chat_context=context,
    )

    assert image_result["success"] is True
    assert image_result["internal_metadata"]["file_upload_action"] == "image"
    assert file_result["success"] is True
    assert file_result["internal_metadata"]["file_upload_action"] == "file"
    assert [call[0] for call in adapter.calls] == ["send_group_msg", "upload_group_file"]


@pytest.mark.asyncio
async def test_file_upload_encodes_local_images_for_napcat(tmp_path: Path):
    adapter = _Adapter()
    image_path = tmp_path / "container_status.png"
    image_path.write_bytes(b"image-bytes")

    result = await file_upload.run(
        action="image",
        image_path=str(image_path),
        bridge=adapter,
        chat_context={"chat_type": "group", "chat_id": "9001"},
    )

    assert result["success"] is True
    assert adapter.calls == [
        (
            "send_group_msg",
            (
                "9001",
                [
                    {
                        "type": "image",
                        "data": {
                            "file": f"base64://{base64.b64encode(b'image-bytes').decode('ascii')}"
                        },
                    }
                ],
            ),
        )
    ]


@pytest.mark.asyncio
async def test_file_upload_sends_image_and_file_to_private_chat(tmp_path: Path):
    adapter = _Adapter()
    file_path = tmp_path / "report.pdf"
    file_path.write_text("report", encoding="utf-8")

    image_result = await file_upload.run(
        action="image",
        image_path="https://example.test/image.png",
        bridge=adapter,
        chat_context={"group_id": "private_qq_10001"},
    )
    file_result = await file_upload.run(
        action="file",
        file_path=str(file_path),
        file_name="report.pdf",
        bridge=adapter,
        chat_context={"chat_type": "private", "user_id": "qq_10001"},
    )

    assert image_result["success"] is True
    assert image_result["internal_metadata"]["target_type"] == "private"
    assert file_result["success"] is True
    assert file_result["internal_metadata"]["target_id"] == "10001"
    assert [call[0] for call in adapter.calls] == ["send_private_msg", "upload_private_file"]
    assert adapter.calls[0][1][0] == "10001"
    assert adapter.calls[1][1] == ("10001", str(file_path.resolve()), "report.pdf")


@pytest.mark.asyncio
async def test_composite_skill_when_action_is_unknown_then_does_not_execute():
    result = await interaction.run(action="unknown")

    assert result["success"] is False
