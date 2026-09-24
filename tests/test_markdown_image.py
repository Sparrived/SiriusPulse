import base64
from pathlib import Path
from typing import Any

import pytest

from sirius_pulse.tools.builtin._internal import _markdown_image
from sirius_pulse.tools.builtin._internal._markdown_image import (
    build_markdown_card_html,
    has_rich_structure,
)


@pytest.mark.parametrize(
    "text",
    [
        "```\nmodule hello\n\ngo 1.22\n```",
        "配置如下：\n\n| 项目 | 状态 |\n|------|------|\n| WebUI | 正常 |",
        "命令\t说明",
        "先看结论。\n\n---\n\n再看细节。",
        "# 部署结果\n\n- WebUI 正常",
        "> 引用一句话",
        "- WebUI 正常\n- Embedding 正常",
        "1. 先备份\n2. 再升级",
    ],
)
def test_has_rich_structure_detects_layout_beyond_inline_symbols(text):
    assert has_rich_structure(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "执行 `docker ps` 查看状态。",
        "看起来是**只读模式**，可以执行 `/op Sparrived`。",
        "今天状态不错，准备晚点再看看服务器。",
        "只有一条：\n- WebUI 正常",
        "",
    ],
)
def test_has_rich_structure_ignores_plain_text_and_inline_symbols(text):
    assert has_rich_structure(text) is False


@pytest.mark.asyncio
async def test_render_and_send_rich_reply_sends_image_then_merged_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    image_path = tmp_path / "card.png"
    content = "| 项目 | 状态 |\n|------|------|\n| WebUI | 正常 |"
    sent: list[tuple[str, Any]] = []
    forwarded: list[tuple[str, str]] = []

    class Adapter:
        async def send_group_msg(self, group_id: str, message: Any) -> dict[str, Any]:
            sent.append((group_id, message))
            return {"data": {"message_id": 42}}

        async def send_group_forward_msg(self, group_id: str, text: str) -> dict[str, Any]:
            forwarded.append((group_id, text))
            return {"data": {"message_id": 77}}

    async def fake_render(rendered_content: str, title: str, data_store: object) -> Path:
        assert rendered_content == content
        assert title == ""
        assert data_store is None
        image_path.write_bytes(b"card-bytes")
        return image_path

    monkeypatch.setattr(_markdown_image, "render_markdown_image", fake_render)
    delivery = await _markdown_image.render_and_send_rich_reply(
        content, adapter=Adapter(), group_id="9001"
    )

    assert delivery == {"image_message_id": "42", "forward_message_id": "77"}
    assert sent == [
        (
            "9001",
            [
                {
                    "type": "image",
                    "data": {"file": f"base64://{base64.b64encode(b'card-bytes').decode('ascii')}"},
                }
            ],
        )
    ]
    assert forwarded == [("9001", content)]
    assert image_path.exists() is False


@pytest.mark.asyncio
async def test_render_and_send_rich_reply_keeps_image_when_merged_forward_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    image_path = tmp_path / "card.png"

    class Adapter:
        async def send_group_msg(self, group_id: str, message: Any) -> dict[str, Any]:
            return {"data": {"message_id": 42}}

        async def send_group_forward_msg(self, group_id: str, text: str) -> dict[str, Any]:
            raise RuntimeError("forward unavailable")

    async def fake_render(rendered_content: str, title: str, data_store: object) -> Path:
        image_path.write_bytes(b"card-bytes")
        return image_path

    monkeypatch.setattr(_markdown_image, "render_markdown_image", fake_render)
    delivery = await _markdown_image.render_and_send_rich_reply(
        "| 项目 | 状态 |\n|------|------|\n| WebUI | 正常 |",
        adapter=Adapter(),
        group_id="9001",
    )

    assert delivery == {"image_message_id": "42", "forward_message_id": ""}


@pytest.mark.asyncio
async def test_render_and_send_rich_reply_skips_forward_without_adapter_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    image_path = tmp_path / "card.png"
    sent_private: list[tuple[str, Any]] = []

    class Adapter:
        async def send_private_msg(self, user_id: str, message: Any) -> dict[str, Any]:
            sent_private.append((user_id, message))
            return {"data": {"message_id": 5}}

    async def fake_render(rendered_content: str, title: str, data_store: object) -> Path:
        image_path.write_bytes(b"card-bytes")
        return image_path

    monkeypatch.setattr(_markdown_image, "render_markdown_image", fake_render)
    delivery = await _markdown_image.render_and_send_rich_reply(
        "命令\t说明", adapter=Adapter(), group_id="private_qq_10001"
    )

    assert delivery == {"image_message_id": "5", "forward_message_id": ""}
    assert [user_id for user_id, _ in sent_private] == ["10001"]


def test_markdown_card_escapes_content_and_renders_common_structures():
    html = build_markdown_card_html(
        "# 概览\n\n- **服务**已恢复\n- 使用 `docker ps` 验证\n\n```\n<script>alert(1)</script>\n```",
        "处理结果",
    )

    assert "<h1>处理结果</h1>" in html
    assert "@font-face" in html
    assert 'font-family: "Sirius Cute"' in html
    assert "<h2>概览</h2>" in html
    assert "<ul>" in html
    assert "<strong>服务</strong>" in html
    assert "<code>docker ps</code>" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>" not in html


def test_markdown_card_renders_pipe_tables_as_html_tables():
    rendered = build_markdown_card_html("| 项目 | 状态 |\n" "|:-----|:----:|\n" "| WebUI | 正常 |")

    assert "<table>" in rendered
    assert "<thead>" in rendered
    assert '<th align="left">项目</th>' in rendered
    assert '<th align="center">状态</th>' in rendered
    assert '<td align="left">WebUI</td>' in rendered
    assert '<p class="table-line">' not in rendered


def test_markdown_card_renders_supported_horizontal_rule_variants():
    rendered = build_markdown_card_html("前一段\n\n—-\n\n后一段")

    assert "<hr>" in rendered
    assert "—-" not in rendered
