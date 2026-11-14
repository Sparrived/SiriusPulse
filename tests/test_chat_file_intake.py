"""业务视角：有人在群里或私聊里发了文件——她要知道，而且拿得到、读得了。

以前文件消息段在渲染时被直接丢掉：只发了一个文件的消息渲染成空串，整条消息没进
她的上下文；私聊文件也没有任何下载途径。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter
from sirius_pulse.tools.builtin import group_file_exec


class _DownloadResponse:
    def __init__(self, payload: bytes):
        self._chunks = iter([payload])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size: int = -1) -> bytes:
        return next(self._chunks, b"")


def _event(message: list[dict], *, private: bool = False) -> dict:
    event = {
        "time": 1763000000,
        "post_type": "message",
        "message_type": "private" if private else "group",
        "user_id": "300",
        "self_id": "100",
        "message_id": "m-1",
        "message": message,
        "sender": {"nickname": "汝且候", "card": ""},
    }
    if not private:
        event["group_id"] = "9001"
    return event


def _adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api=None) -> NapCatAdapter:
    adapter = NapCatAdapter(
        "ws://example.invalid",
        work_path=tmp_path,
        config={"allowed_group_ids": ["9001"], "persona_name": "月白", "qq_number": "100"},
    )

    async def fake_call_api(action: str, params: dict) -> dict:
        if api is None:
            raise AssertionError(f"unexpected API call {action}")
        return await api(action, params)

    monkeypatch.setattr(adapter, "call_api", fake_call_api)
    return adapter


_PPTX = {
    "type": "file",
    "data": {
        "file": "马苏啦-Masula-技术分享.pptx",
        "file_id": "/a1b2c3",
        "file_size": "2411724",
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True], ids=["group", "private"])
async def test_a_file_on_its_own_reaches_her_with_what_she_needs_to_fetch_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private: bool
) -> None:
    adapter = _adapter(tmp_path, monkeypatch)

    parsed = await adapter.parse_event(_event([_PPTX], private=private))

    assert parsed is not None, "只发了一个文件的消息不能被丢掉"
    assert parsed.prompt == '[文件："马苏啦-Masula-技术分享.pptx" 2.3MB file_id=/a1b2c3]'


@pytest.mark.asyncio
async def test_a_file_sent_with_words_keeps_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path, monkeypatch)

    parsed = await adapter.parse_event(
        _event([{"type": "text", "data": {"text": "帮我看看这份周报 "}}, _PPTX])
    )

    assert parsed is not None
    assert parsed.prompt.startswith("帮我看看这份周报")
    assert "file_id=/a1b2c3" in parsed.prompt


@pytest.mark.asyncio
async def test_an_online_transfer_is_announced_as_something_she_cannot_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path, monkeypatch)

    parsed = await adapter.parse_event(
        _event(
            [{"type": "onlinefile", "data": {"fileName": "录屏.mp4", "fileSize": "52428800"}}],
            private=True,
        )
    )

    assert parsed is not None
    assert "录屏.mp4" in parsed.prompt and "收不下来" in parsed.prompt
    assert "file_id" not in parsed.prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("link_api_works", "segment_url"),
    [(True, ""), (False, "https://example.test/from-message")],
    ids=["fresh-link", "link-from-the-message"],
)
async def test_a_file_sent_in_private_can_be_downloaded_under_its_own_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_api_works: bool,
    segment_url: str,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def api(action: str, params: dict) -> dict:
        calls.append((action, params))
        if not link_api_works:
            raise RuntimeError("real fileUUID not found!")
        return {"data": {"url": "https://example.test/fresh"}}

    adapter = _adapter(tmp_path, monkeypatch, api)
    fetched: list[str] = []

    def fake_urlopen(request, **_kwargs):
        fetched.append(request.full_url)
        return _DownloadResponse(b"pptx-bytes")

    monkeypatch.setattr("sirius_pulse.platforms.onebot_v11.napcat.adapter.urlopen", fake_urlopen)
    segment = {"type": "file", "data": {**_PPTX["data"], "url": segment_url}}
    await adapter.parse_event(_event([segment], private=True))

    # 她只记得 file_id，没传文件名。
    result = await group_file_exec.run(
        "download",
        file_id="/a1b2c3",
        bridge=adapter,
        chat_context={"chat_type": "private", "user_id": "300"},
    )

    assert result["success"] is True, result.get("error")
    saved = Path(result["data"]["path"])
    assert saved.name == "马苏啦-Masula-技术分享.pptx"
    assert saved.read_bytes() == b"pptx-bytes"
    assert "read_document" in result["text_blocks"][0]
    assert calls == [("get_private_file_url", {"file_id": "/a1b2c3"})]
    assert fetched == ["https://example.test/fresh" if link_api_works else segment_url]


@pytest.mark.asyncio
async def test_a_private_file_with_no_link_left_gets_a_reason_she_can_pass_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def api(action: str, params: dict) -> dict:
        return {"data": {}}

    adapter = _adapter(tmp_path, monkeypatch, api)

    result = await group_file_exec.run(
        "download",
        file_id="/gone",
        bridge=adapter,
        chat_context={"chat_type": "private", "user_id": "300"},
    )

    assert result["success"] is False and "重新发一次" in result["error"]


@pytest.mark.asyncio
async def test_a_file_dropped_in_the_group_still_downloads_through_group_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict]] = []

    async def api(action: str, params: dict) -> dict:
        calls.append((action, params))
        return {"data": {"url": "https://example.test/group"}}

    adapter = _adapter(tmp_path, monkeypatch, api)
    monkeypatch.setattr(
        "sirius_pulse.platforms.onebot_v11.napcat.adapter.urlopen",
        lambda *_a, **_k: _DownloadResponse(b"group-bytes"),
    )
    await adapter.parse_event(_event([_PPTX]))

    result = await group_file_exec.run(
        "download",
        file_id="/a1b2c3",
        bridge=adapter,
        chat_context={"chat_type": "group", "group_id": "9001"},
    )

    assert result["success"] is True
    assert Path(result["data"]["path"]).name == "马苏啦-Masula-技术分享.pptx"
    assert calls == [("get_group_file_url", {"group_id": 9001, "file_id": "/a1b2c3"})]
