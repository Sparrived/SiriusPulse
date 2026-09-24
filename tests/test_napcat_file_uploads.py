from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import quote

import pytest

from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter


class _DownloadResponse:
    def __init__(self, chunks: list[bytes]):
        self._chunks = iter(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size: int = -1) -> bytes:
        return next(self._chunks, b"")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "target_id", "action_name", "target_key"),
    [
        ("upload_group_file", 9001, "upload_group_file", "group_id"),
        ("upload_private_file", 10001, "upload_private_file", "user_id"),
    ],
)
async def test_napcat_upload_stages_local_file_for_the_other_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    target_id: int,
    action_name: str,
    target_key: str,
):
    source = tmp_path / "source" / "container report.txt"
    source.parent.mkdir()
    source.write_bytes(b"container report")
    shared_root = tmp_path / "shared-upload"
    monkeypatch.setenv("SIRIUS_NAPCAT_UPLOAD_ROOT", str(shared_root))
    monkeypatch.setenv("SIRIUS_NAPCAT_UPLOAD_TARGET_ROOT", "/sirius-upload")

    adapter = NapCatAdapter("ws://example.invalid")
    observed: dict[str, object] = {}

    async def fake_call_api(action: str, params: dict[str, object]) -> dict[str, object]:
        staged_files = list(shared_root.iterdir())
        assert len(staged_files) == 1
        assert staged_files[0].read_bytes() == b"container report"
        assert params == {
            target_key: target_id,
            "file": f"file:///sirius-upload/{quote(staged_files[0].name)}",
            "name": "report.txt",
        }
        observed["action"] = action
        return {"data": {"file_id": "file-1"}}

    monkeypatch.setattr(adapter, "call_api", fake_call_api)
    result = await getattr(adapter, method_name)(target_id, str(source), "report.txt")

    assert result == {"data": {"file_id": "file-1"}}
    assert observed["action"] == action_name
    assert list(shared_root.iterdir()) == []


@pytest.mark.asyncio
async def test_napcat_upload_preserves_remote_file_reference(monkeypatch: pytest.MonkeyPatch):
    adapter = NapCatAdapter("ws://example.invalid")
    captured: dict[str, object] = {}

    async def fake_call_api(action: str, params: dict[str, object]) -> dict[str, object]:
        captured["action"] = action
        captured["params"] = params
        return {"data": {"file_id": "file-1"}}

    monkeypatch.setattr(adapter, "call_api", fake_call_api)
    await adapter.upload_group_file(9001, "https://example.test/report.txt", "report.txt")

    assert captured == {
        "action": "upload_group_file",
        "params": {
            "group_id": 9001,
            "file": "https://example.test/report.txt",
            "name": "report.txt",
        },
    }


@pytest.mark.asyncio
async def test_napcat_group_image_encodes_local_file_for_api(tmp_path: Path):
    source = tmp_path / "github_update.png"
    source.write_bytes(b"png-bytes")
    adapter = NapCatAdapter("ws://example.invalid")
    captured: dict[str, object] = {}

    async def fake_send_group_msg(group_id: str, message: list[dict[str, object]]):
        captured["group_id"] = group_id
        captured["message"] = message
        return {"data": {"message_id": 1}}

    adapter.send_group_msg = fake_send_group_msg  # type: ignore[method-assign]
    await adapter._send_group_image("9001", str(source))

    message = captured["message"]
    assert isinstance(message, list)
    image_reference = message[0]["data"]["file"]
    assert isinstance(image_reference, str)
    assert image_reference.startswith("base64://")
    assert base64.b64decode(image_reference.removeprefix("base64://")) == b"png-bytes"


@pytest.mark.asyncio
async def test_napcat_upload_removes_partial_copy_when_staging_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source.txt"
    source.write_text("report", encoding="utf-8")
    shared_root = tmp_path / "shared-upload"
    monkeypatch.setenv("SIRIUS_NAPCAT_UPLOAD_ROOT", str(shared_root))

    def partial_copy(_: Path, destination: Path) -> None:
        destination.write_text("partial", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(
        "sirius_pulse.platforms.onebot_v11.napcat.adapter.shutil.copyfile", partial_copy
    )
    adapter = NapCatAdapter("ws://example.invalid")

    with pytest.raises(OSError, match="disk full"):
        await adapter.upload_group_file(9001, str(source), "report.txt")

    assert list(shared_root.iterdir()) == []


@pytest.mark.asyncio
async def test_napcat_group_file_list_uses_root_and_folder_actions(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = NapCatAdapter("ws://example.invalid")
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call_api(action: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((action, params))
        return {"data": {"files": [], "folders": []}}

    monkeypatch.setattr(adapter, "call_api", fake_call_api)

    await adapter.get_group_file_list(9001)
    await adapter.get_group_file_list(9001, "folder-1", 20)

    assert calls == [
        ("get_group_root_files", {"group_id": 9001, "file_count": 50}),
        (
            "get_group_files_by_folder",
            {"group_id": 9001, "file_count": 20, "folder_id": "folder-1"},
        ),
    ]


@pytest.mark.asyncio
async def test_napcat_downloads_group_file_url_to_persona_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    adapter = NapCatAdapter("ws://example.invalid", work_path=tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call_api(action: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((action, params))
        return {"data": {"url": "https://example.test/file.bin"}}

    monkeypatch.setattr(adapter, "call_api", fake_call_api)
    monkeypatch.setattr(
        "sirius_pulse.platforms.onebot_v11.napcat.adapter.urlopen",
        lambda *_args, **_kwargs: _DownloadResponse([b"file", b"-bytes"]),
    )

    result = await adapter.download_group_file(9001, "file-1", "../report.bin")

    output = Path(result["path"])
    assert output == (tmp_path / "group_files" / "report.bin").resolve()
    assert output.read_bytes() == b"file-bytes"
    assert result["size"] == 10
    assert calls == [
        ("get_group_file_url", {"group_id": 9001, "file_id": "file-1"}),
    ]


@pytest.mark.asyncio
async def test_napcat_group_forward_wraps_text_into_signed_nodes(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = NapCatAdapter(
        "ws://example.invalid",
        config={"allowed_group_ids": [9001], "qq_number": "3385516316"},
    )
    adapter.set_persona_name("月白")
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call_api(action: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((action, params))
        return {"data": {"message_id": 88}}

    monkeypatch.setattr(adapter, "call_api", fake_call_api)

    result = await adapter.send_group_forward_msg(9001, "第一行\n第二行")

    assert result == {"data": {"message_id": 88}}
    assert calls == [
        (
            "send_group_forward_msg",
            {
                "group_id": 9001,
                "messages": [
                    {
                        "type": "node",
                        "data": {
                            "uin": "3385516316",
                            "nickname": "月白",
                            "content": [{"type": "text", "data": {"text": "第一行\n第二行"}}],
                        },
                    }
                ],
            },
        )
    ]


@pytest.mark.asyncio
async def test_napcat_private_forward_splits_long_text_across_nodes(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = NapCatAdapter("ws://example.invalid", config={"qq_number": "3385516316"})
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_call_api(action: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((action, params))
        return {"data": {"message_id": 99}}

    monkeypatch.setattr(adapter, "call_api", fake_call_api)

    long_text = "\n".join(f"第{index}行" for index in range(400))
    await adapter.send_private_forward_msg(10001, long_text)

    action, params = calls[0]
    assert action == "send_private_forward_msg"
    assert params["user_id"] == 10001
    nodes = params["messages"]
    assert isinstance(nodes, list) and len(nodes) > 1
    assert all(len(node["data"]["content"][0]["data"]["text"]) <= 800 for node in nodes)
    assert "\n".join(node["data"]["content"][0]["data"]["text"] for node in nodes) == long_text


@pytest.mark.asyncio
async def test_napcat_forward_actions_share_the_reply_throttle_channel():
    adapter = NapCatAdapter("ws://example.invalid")

    assert adapter._is_send_action("send_group_forward_msg") is True
    assert adapter._is_send_action("send_private_forward_msg") is True
    assert adapter._send_channel_key("send_group_forward_msg", {"group_id": 9001}) == "group_9001"
    assert (
        adapter._send_channel_key("send_private_forward_msg", {"user_id": 10001}) == "private_10001"
    )
