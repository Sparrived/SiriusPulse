"""业务视角：她能读群里发来的文档；长文分段读完；不能被人借去读配置和密钥。"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.tools.builtin import read_document
from sirius_pulse.tools.data_store import ToolDataStore
from sirius_pulse.tools.models import ToolInvocationContext


def _persona(tmp_path: Path) -> tuple[ToolDataStore, Path, Path]:
    """人格目录：家目录与下载下来的群文件目录。"""
    store = ToolDataStore(tmp_path / "tool_data" / "read_document.json")
    home, downloads = tmp_path / "home", tmp_path / "group_files"
    home.mkdir()
    downloads.mkdir()
    return store, home, downloads


def _who(*, developer: bool = False) -> ToolInvocationContext:
    return ToolInvocationContext(
        caller=UnifiedUser(user_id="u1", name="小明", metadata={"is_developer": developer}),
        group_id="g1",
    )


def _read(store: ToolDataStore, path: str, *, developer: bool = False, **kwargs) -> dict:
    return read_document.run(
        path, data_store=store, invocation_context=_who(developer=developer), **kwargs
    )


def _pdf(lines: list[str]) -> bytes:
    """一份最小但合法的单页 PDF；lines 为空时页面没有文字层（像扫描件）。"""
    text_ops = "".join(
        f"BT /F1 12 Tf 72 {720 - 20 * i} Td ({line}) Tj ET\n" for i, line in enumerate(lines)
    )
    stream = text_ops.encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def _docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def _pptx(slides: list[list[str]]) -> bytes:
    """每页若干段文字，按页码的字符串顺序写进压缩包（slide10 会排在 slide2 前面）。"""
    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ppt/presentation.xml", "<p:presentation/>")
        numbered = list(enumerate(slides, start=1))
        for number, paragraphs in sorted(numbered, key=lambda item: str(item[0])):
            body = "".join(f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" for text in paragraphs)
            archive.writestr(
                f"ppt/slides/slide{number}.xml",
                f'<p:sld xmlns:a="{ns}" xmlns:p="urn:p"><p:txBody>{body}</p:txBody></p:sld>',
            )
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("notes.txt", "周末聚餐改到周日晚上七点".encode("gb18030"), "周日晚上七点"),
        ("readme.md", "# 安装\n先运行 uv sync".encode("utf-8"), "先运行 uv sync"),
        (
            "page.html",
            "<html><body><h1>公告</h1><p>本周停更</p><script>x()</script></body></html>".encode(),
            "本周停更",
        ),
        ("minutes.docx", _docx(["会议纪要", "下周三交初稿"]), "下周三交初稿"),
        ("report.pdf", _pdf(["Quarterly report", "Revenue grew 12 percent"]), "Revenue grew 12"),
        ("talk.pptx", _pptx([["技术分享"], ["适配器分层"]]), "--- 第 2 页 ---\n适配器分层"),
    ],
)
def test_documents_sent_in_the_group_can_be_read(
    tmp_path: Path, name: str, content: bytes, expected: str
) -> None:
    store, _, downloads = _persona(tmp_path)
    (downloads / name).write_bytes(content)

    result = _read(store, str(downloads / name))

    assert result["success"] is True, result.get("error")
    assert expected in result["text_blocks"][0]
    assert "已读完" in result["text_blocks"][0]


def test_a_long_document_is_read_in_pieces_until_the_end(tmp_path: Path) -> None:
    store, home, _ = _persona(tmp_path)
    body = "".join(f"第{i:04d}行内容。\n" for i in range(800))
    (home / "long.txt").write_text(body, encoding="utf-8")

    pieces, offset = [], 0
    while offset is not None:
        result = _read(store, "~/long.txt", offset=offset, max_chars=2000)
        assert result["success"] is True
        pieces.append(result["text_blocks"][0])
        offset = result["data"]["next_offset"]

    assert len(pieces) > 2
    assert "offset=2000" in pieces[0]
    assert "第0799行" in pieces[-1] and "已读完" in pieces[-1]


@pytest.mark.parametrize(
    ("name", "content", "error"),
    [
        ("scan.pdf", _pdf([]), "扫描件"),
        ("photo.png", b"\x89PNG\r\n\x1a\n", "read_image"),
        ("archive.bin", b"\x00\x01\x02binary", "不是文本文件"),
        ("old.doc", b"legacy", "旧版 Office"),
        ("broken.docx", b"not a zip", "已损坏"),
        ("broken.pptx", b"not a zip", "已损坏"),
    ],
)
def test_unreadable_files_get_a_reason_she_can_pass_on(
    tmp_path: Path, name: str, content: bytes, error: str
) -> None:
    store, _, downloads = _persona(tmp_path)
    (downloads / name).write_bytes(content)

    result = _read(store, str(downloads / name))

    assert result["success"] is False and error in result["error"]


@pytest.mark.parametrize(
    ("developer", "readable"),
    [(False, False), (True, True)],
    ids=["group-member", "developer"],
)
def test_config_and_secrets_outside_her_own_places_stay_closed(
    tmp_path: Path, developer: bool, readable: bool
) -> None:
    store, home, _ = _persona(tmp_path)
    secret = tmp_path / "accounts.json"
    secret.write_text('{"token": "ghp_secret"}', encoding="utf-8")
    sneaky = home / "link.json"
    try:
        sneaky.symlink_to(secret)
    except OSError:
        sneaky = None

    direct = _read(store, str(secret), developer=developer)

    assert direct["success"] is readable
    if not readable:
        assert "ghp_secret" not in str(direct)
        if sneaky is not None:
            # 家目录里的符号链接指向外面，也不能借道读出来。
            via_link = _read(store, str(sneaky))
            assert via_link["success"] is False and "ghp_secret" not in str(via_link)
