"""图片进入模型视觉通道前必须真正落地为本地副本。

回归背景：平台图片地址（QQ 多媒体 download?rkey=…）带短时效签名并校验
Referer。一旦下载失败就把原始 URL 塞进多模态输入，上游模型自行下载必然
403，最后表现为模型说「我只能看到一张禁止访问的图片」。这些用例钉住：
失败的图片不进视觉通道，XML 历史不回显路径/链接，大图降采样而不丢图。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from sirius_pulse.adapters.base import BaseAdapter
from sirius_pulse.adapters.models import MessageGroup, ParsedEvent
from sirius_pulse.core.helpers import Helpers
from sirius_pulse.memory.basic.manager import BasicMemoryManager
from sirius_pulse.memory.context_assembler import ContextAssembler
from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter
from sirius_pulse.providers.base import prepare_openai_compatible_messages
from sirius_pulse.utils.image_bytes import downscale_image_bytes

_QQ_SIGNED_URL = (
    "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=EhQabc&rkey=CAISMFXvNf9M3KGuiB5A"
)


class _StubAdapter(BaseAdapter):
    """只实现抽象方法，用于验证 BaseAdapter.cache_image 的通用语义。"""

    adapter_type = "stub"

    def __init__(self, work_path: Path) -> None:
        self._image_cache_dir = work_path / "image_cache"
        self._sticker_cache_dir = work_path / "sticker_cache"

    async def parse_event(self, raw_event: dict) -> ParsedEvent | None:  # pragma: no cover
        return None

    async def send_group_message(self, group_id: str, message: MessageGroup | str) -> dict:
        return {}

    async def send_private_message(self, user_id: str, message: MessageGroup | str) -> dict:
        return {}

    async def call_api(self, action: str, params: dict) -> dict:
        return {}


def _png_bytes(
    size: tuple[int, int] = (8, 8), color: tuple[int, int, int] = (200, 30, 30)
) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, *, status: int = 200, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def get(self, url: str, headers: dict | None = None) -> _FakeResponse:
        return self._response

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def _patch_download(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> None:
    monkeypatch.setattr(
        "aiohttp.ClientSession",
        lambda *args, **kwargs: _FakeSession(response),
    )


# ── cache_image：失败不再退回平台地址 ──


@pytest.mark.asyncio
async def test_cache_image_when_download_returns_403_then_yields_no_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """平台拒绝下载时必须明确「没有图」，而不是把签名链接递给模型。"""
    adapter = _StubAdapter(tmp_path)
    _patch_download(monkeypatch, _FakeResponse(status=403))

    result = await adapter.cache_image(_QQ_SIGNED_URL)

    assert result == ""


@pytest.mark.asyncio
async def test_cache_image_when_download_raises_then_yields_no_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """网络异常时同样不得回退成原始 URL。"""
    adapter = _StubAdapter(tmp_path)

    class _BoomSession:
        def get(self, url: str, headers: dict | None = None):
            raise OSError("getaddrinfo failed")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **k: _BoomSession())

    result = await adapter.cache_image(_QQ_SIGNED_URL)

    assert result == ""
    assert _QQ_SIGNED_URL not in result


@pytest.mark.asyncio
async def test_cache_image_when_download_succeeds_then_returns_local_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """正常下载应落盘成可内联的本地文件。"""
    adapter = _StubAdapter(tmp_path)
    _patch_download(monkeypatch, _FakeResponse(body=_png_bytes()))

    result = await adapter.cache_image(_QQ_SIGNED_URL)

    assert result and not result.startswith("http")
    assert Path(result).is_file()
    assert Path(result).read_bytes() == _png_bytes()


@pytest.mark.asyncio
async def test_cache_image_when_image_exceeds_limit_then_downscales_instead_of_dropping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """超大图应被压缩后保留，而不是丢弃（丢弃等于模型看不到图）。"""
    adapter = _StubAdapter(tmp_path)
    adapter.MAX_CACHED_IMAGE_BYTES = 2048
    _patch_download(monkeypatch, _FakeResponse(body=_png_bytes((600, 600))))

    result = await adapter.cache_image(_QQ_SIGNED_URL)

    assert result
    path = Path(result)
    assert path.is_file()
    assert path.stat().st_size <= 2048
    # 压缩输出恒为 JPEG，扩展名必须与之相符，否则传输层 MIME 会推断错。
    assert path.suffix == ".jpg"


# ── NapCat：失败图片不进多模态通道，可用 get_image 兜底 ──


def _napcat(tmp_path: Path) -> NapCatAdapter:
    return NapCatAdapter(
        "ws://example.invalid",
        work_path=tmp_path,
        config={"allowed_group_ids": ["100"], "qq_number": "100"},
    )


@pytest.mark.asyncio
async def test_collect_image_inputs_when_download_fails_then_image_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """缓存失败时该图必须被丢弃，不能把 QQ 签名链接带进视觉输入。"""
    adapter = _napcat(tmp_path)
    _patch_download(monkeypatch, _FakeResponse(status=403))

    async def no_recovery(file_ref: str, *, is_sticker: bool) -> str:
        return ""

    monkeypatch.setattr(adapter, "_recover_image_via_api", no_recovery)

    inputs = await adapter._collect_image_inputs(
        [{"type": "image", "data": {"url": _QQ_SIGNED_URL}}]
    )

    assert inputs == []


@pytest.mark.asyncio
async def test_collect_image_inputs_when_direct_download_fails_then_recovers_via_get_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """直连失败时应尝试 NapCat 的 get_image，把真正的图取回来。"""
    adapter = _napcat(tmp_path)
    _patch_download(monkeypatch, _FakeResponse(status=403))

    napcat_file = tmp_path / "from_napcat.png"
    napcat_file.write_bytes(_png_bytes())
    calls: list[tuple[str, dict]] = []

    async def fake_call_api(action: str, params: dict) -> dict:
        calls.append((action, params))
        return {"data": {"file": str(napcat_file)}}

    monkeypatch.setattr(adapter, "call_api", fake_call_api)

    inputs = await adapter._collect_image_inputs(
        [{"type": "image", "data": {"url": _QQ_SIGNED_URL, "file": "abc.image"}}]
    )

    assert [c[0] for c in calls] == ["get_image"]
    assert len(inputs) == 1
    value = inputs[0]["value"]
    assert not value.startswith("http")
    assert Path(value).is_file()
    # 取回的图应被收进人格图片缓存，而不是直接引用 NapCat 的临时文件。
    assert Path(value).parent == adapter._image_cache_dir


@pytest.mark.asyncio
async def test_collect_image_inputs_when_get_image_returns_remote_url_then_caches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """get_image 返回新链接时，应重新下载成本地副本。"""
    adapter = _napcat(tmp_path)
    fresh_url = "https://multimedia.nt.qq.com.cn/download?rkey=fresh"
    _patch_download(monkeypatch, _FakeResponse(status=403))

    async def fake_call_api(action: str, params: dict) -> dict:
        return {"data": {"url": fresh_url}}

    monkeypatch.setattr(adapter, "call_api", fake_call_api)
    _patch_download(monkeypatch, _FakeResponse(body=_png_bytes()))

    inputs = await adapter._collect_image_inputs(
        [{"type": "image", "data": {"url": _QQ_SIGNED_URL, "file": "abc.image"}}]
    )

    assert len(inputs) == 1
    assert Path(inputs[0]["value"]).is_file()


@pytest.mark.asyncio
async def test_collect_image_inputs_keeps_sticker_subtype(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """动画表情仍应带上 sub_type，表情包链路不能因此失效。"""
    adapter = _napcat(tmp_path)
    _patch_download(monkeypatch, _FakeResponse(body=_png_bytes()))

    inputs = await adapter._collect_image_inputs(
        [{"type": "image", "data": {"url": _QQ_SIGNED_URL, "sub_type": "1"}}]
    )

    assert len(inputs) == 1
    assert inputs[0]["sub_type"] == "1"
    assert Path(inputs[0]["value"]).parent == adapter._sticker_cache_dir


# ── 视觉通道：旧数据里的平台链接也要被剔除 ──


def _messages(content: str = "看这个") -> list[dict]:
    return [{"role": "user", "content": content}]


def test_inject_multimodal_when_value_is_platform_url_then_it_is_not_sent_to_model():
    """历史里已持久化的签名链接不应再进入请求体。"""
    messages = Helpers.inject_multimodal_into_user_message(
        _messages(),
        [{"type": "image", "value": _QQ_SIGNED_URL}],
    )

    assert messages[0]["content"] == "看这个"


def test_inject_multimodal_when_value_is_local_path_then_it_is_sent_to_model():
    """本地副本仍应正常进入视觉通道。"""
    messages = Helpers.inject_multimodal_into_user_message(
        _messages(),
        [{"type": "image", "value": "cat.png"}],
    )

    assert messages[0]["content"][1] == {"type": "image_url", "image_url": {"url": "cat.png"}}


def test_inject_multimodal_when_mixing_valid_and_invalid_then_keeps_only_valid():
    messages = Helpers.inject_multimodal_into_user_message(
        _messages(),
        [
            {"type": "image", "value": _QQ_SIGNED_URL},
            {"type": "image", "value": "cat.png"},
        ],
    )

    urls = [part["image_url"]["url"] for part in messages[0]["content"][1:]]
    assert urls == ["cat.png"]


# ── XML 历史：只给描述，不回显路径/链接 ──


def test_context_history_when_image_present_then_xml_omits_src():
    """聊天历史 XML 不应把本地路径或签名链接暴露给模型。"""
    memory = BasicMemoryManager()
    memory.add_entry(
        "group-1",
        "u1",
        "human",
        "[图片]",
        speaker_name="Alice",
        multimodal_inputs=[
            {
                "type": "image",
                "value": _QQ_SIGNED_URL,
                "caption": "一张橘猫照片",
            }
        ],
    )
    assembler = ContextAssembler(memory)

    messages = assembler.build_messages(
        group_id="group-1",
        current_query="这是什么",
        system_prompt="system",
    )
    rendered = "\n".join(str(m["content"]) for m in messages)

    assert "一张橘猫照片" in rendered
    assert "src=" not in rendered
    assert "rkey" not in rendered


# ── 传输层：超大本地图降采样后再内联 ──


def test_prepare_messages_when_local_image_exceeds_limit_then_data_url_is_downscaled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """base64 会放大 4/3，超大原图应在内联前降采样。"""
    import sirius_pulse.providers.base as provider_base

    monkeypatch.setattr(provider_base, "MAX_INLINE_IMAGE_BYTES", 2048)
    image_path = tmp_path / "huge.png"
    image_path.write_bytes(_png_bytes((600, 600)))

    prepared, stats = prepare_openai_compatible_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {"type": "image_url", "image_url": {"url": str(image_path)}},
                ],
            }
        ]
    )

    assert stats["local_image_path_conversions"] == 1
    data_url = prepared[0]["content"][1]["image_url"]["url"]
    assert data_url.startswith("data:image/jpeg;base64,")
    import base64

    encoded = data_url.split(",", 1)[1]
    assert len(base64.b64decode(encoded)) <= 2048


def test_downscale_image_bytes_when_undecodable_then_returns_none():
    """无法解码时返回 None，让调用方明确放弃内联而不是猜测。"""
    assert downscale_image_bytes(b"not-an-image") is None
