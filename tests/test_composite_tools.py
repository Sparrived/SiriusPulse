from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from sirius_pulse.tools.builtin import group_file_exec


class _Adapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

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

    async def get_group_file_list(
        self, group_id: str, folder_id: str, file_count: int
    ) -> dict[str, Any]:
        self.calls.append(("get_group_file_list", (group_id, folder_id, file_count)))
        return {
            "files": [{"file_id": "file-1", "file_name": "report.pdf", "file_size": 7}],
            "folders": [{"folder_id": "folder-1", "folder_name": "资料"}],
        }

    async def download_group_file(
        self, group_id: str, file_id: str, file_name: str, download_dir: str
    ) -> dict[str, Any]:
        self.calls.append(("download_group_file", (group_id, file_id, file_name, download_dir)))
        return {
            "path": f"{download_dir or 'group_files'}/{file_name}",
            "file_name": file_name,
            "size": 7,
        }


def test_group_file_exec_schema_exposes_group_file_actions():
    parameters = {item["name"]: item for item in group_file_exec.TOOL_META["parameters"]}

    assert parameters["action"]["choices"] == ["image", "file", "list", "download"]
    assert {"folder_id", "file_count", "file_id", "download_dir"} <= set(parameters)


@pytest.mark.asyncio
async def test_group_file_exec_sends_image_and_uploads_file(tmp_path: Path):
    adapter = _Adapter()
    file_path = tmp_path / "report.pdf"
    file_path.write_text("report", encoding="utf-8")
    context = {"chat_type": "group", "chat_id": "9001"}

    image_result = await group_file_exec.run(
        action="image",
        image_path="https://example.test/image.png",
        bridge=adapter,
        chat_context=context,
    )
    file_result = await group_file_exec.run(
        action="file",
        file_path=str(file_path),
        file_name="report.pdf",
        bridge=adapter,
        chat_context=context,
    )

    assert image_result["success"] is True
    assert image_result["internal_metadata"]["group_file_exec_action"] == "image"
    assert file_result["success"] is True
    assert file_result["internal_metadata"]["group_file_exec_action"] == "file"
    assert [call[0] for call in adapter.calls] == ["send_group_msg", "upload_group_file"]


@pytest.mark.asyncio
async def test_group_file_exec_encodes_local_images_for_napcat(tmp_path: Path):
    adapter = _Adapter()
    image_path = tmp_path / "container_status.png"
    image_path.write_bytes(b"image-bytes")

    result = await group_file_exec.run(
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
async def test_group_file_exec_sends_image_and_file_to_private_chat(tmp_path: Path):
    adapter = _Adapter()
    file_path = tmp_path / "report.pdf"
    file_path.write_text("report", encoding="utf-8")

    image_result = await group_file_exec.run(
        action="image",
        image_path="https://example.test/image.png",
        bridge=adapter,
        chat_context={"group_id": "private_qq_10001"},
    )
    file_result = await group_file_exec.run(
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
async def test_group_file_exec_lists_and_downloads_group_files(tmp_path: Path):
    adapter = _Adapter()
    context = {"chat_type": "group", "chat_id": "9001"}

    list_result = await group_file_exec.run(
        action="list",
        folder_id="",
        file_count=20,
        bridge=adapter,
        chat_context=context,
    )
    download_result = await group_file_exec.run(
        action="download",
        file_id="file-1",
        file_name="report.pdf",
        download_dir=str(tmp_path),
        bridge=adapter,
        chat_context=context,
    )

    assert list_result["success"] is True
    assert list_result["internal_metadata"]["group_file_exec_action"] == "list"
    assert "file_id=file-1" in "\n".join(list_result["text_blocks"])
    assert download_result["success"] is True
    assert download_result["internal_metadata"]["group_file_exec_action"] == "download"
    assert download_result["internal_metadata"]["file_id"] == "file-1"
    assert adapter.calls[-2:] == [
        ("get_group_file_list", ("9001", "", 20)),
        ("download_group_file", ("9001", "file-1", "report.pdf", str(tmp_path))),
    ]


@pytest.mark.asyncio
async def test_group_file_exec_group_file_actions_reject_private_chat():
    result = await group_file_exec.run(
        action="list",
        bridge=_Adapter(),
        chat_context={"chat_type": "private", "user_id": "10001"},
    )

    assert result["success"] is False
    assert "群聊" in result["error"]


@pytest.mark.asyncio
async def test_group_file_exec_expands_tilde_against_process_home(tmp_path: Path, monkeypatch):
    """`~` must resolve to this process's home, matching what bash sees."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "moonlit").mkdir()
    (tmp_path / "moonlit" / "report.md").write_text("report", encoding="utf-8")

    adapter = _Adapter()
    result = await group_file_exec.run(
        action="file",
        file_path="~/moonlit/report.md",
        bridge=adapter,
        chat_context={"chat_type": "group", "chat_id": "9001"},
    )

    assert result["success"] is True
    assert adapter.calls[-1] == (
        "upload_group_file",
        ("9001", str(tmp_path / "moonlit" / "report.md"), "report.md"),
    )


@pytest.mark.asyncio
async def test_group_file_exec_reports_home_when_path_check_raises(tmp_path: Path, monkeypatch):
    """An unreadable parent (other user's home) must not surface a bare errno."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    adapter = _Adapter()

    def _raise(self: Path) -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "exists", _raise)

    result = await group_file_exec.run(
        action="file",
        file_path="/root/moonlit/report.md",
        bridge=adapter,
        chat_context={"chat_type": "group", "chat_id": "9001"},
    )

    assert result["success"] is False
    assert adapter.calls == []
    assert "没有权限访问" in result["error"]
    assert str(tmp_path) in result["error"]
