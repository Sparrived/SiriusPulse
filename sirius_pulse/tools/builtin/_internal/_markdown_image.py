"""Render a whole structured reply into one shareable image.

当回复里出现行内符号之外的排版结构（代码块、表格、制表符、分隔线、标题、
引用、列表块）时，纯文本已经无法还原排版，因此整段内容都会渲染成一张图片；
同时通过平台适配器补发一条合并转发消息，保留可复制的原文。
"""

from __future__ import annotations

import base64
import html
import logging
import re
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

LOG = logging.getLogger("sirius.tools.markdown_image")

_MAX_CONTENT_CHARS = 12_000
_MAX_TITLE_CHARS = 80
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")
_ORDERED_ITEM_RE = re.compile(r"^\d+[.)]\s+(.+)$")
_BULLET_ITEM_RE = re.compile(r"^[-*+]\s+(.+)$")
_FENCE_LINE_RE = re.compile(r"^\s{0,3}([`~]+)([^\r\n]*)$")
_HEADING_LINE_RE = re.compile(r"^#{1,6}\s+\S")
_BLOCKQUOTE_LINE_RE = re.compile(r"^>\s*\S")
_LIST_ITEM_LINE_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+\S")
_HORIZONTAL_RULE_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,}|—-+|—{2,}|－{3,}|＿{3,})$")
_TABLE_ROW_PIPES = 2
_MIN_TABLE_ROWS = 2
_MIN_LIST_ITEMS = 2
_CUTE_FONT_PATH = Path(__file__).with_name("assets") / "ZCOOLKuaiLe-Regular.ttf"


def has_rich_structure(text: str) -> bool:
    """判断回复是否含有行内符号之外的排版结构。

    代码块、表格、制表符、分隔线、标题、引用、列表块都需要真正的排版才能还原，
    所以只要出现其中任意一种，整段内容都应该转成图片；行内的反引号代码与
    **加粗** 仍算普通文本符号，单独出现时不触发转换。
    """
    source = _normalize_fence_chars(str(text or ""))
    if not source.strip():
        return False
    if "\t" in source:
        return True

    table_rows = 0
    list_items = 0
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            list_items = 0
            continue
        if (
            _is_code_fence_line(line)
            or _is_horizontal_rule_line(line)
            or _HEADING_LINE_RE.match(line)
            or _BLOCKQUOTE_LINE_RE.match(line)
        ):
            return True
        if line.count("|") >= _TABLE_ROW_PIPES:
            table_rows += 1
            if table_rows >= _MIN_TABLE_ROWS:
                return True
            continue
        if _LIST_ITEM_LINE_RE.match(line):
            list_items += 1
            if list_items >= _MIN_LIST_ITEMS:
                return True
            continue
        list_items = 0
    return False


def _normalize_fence_chars(text: str) -> str:
    return str(text or "").replace("｀", "`").replace("～", "~")


def _is_table_row_line(line: str) -> bool:
    return str(line or "").count("|") >= 2


def _is_horizontal_rule_line(line: str) -> bool:
    return bool(_HORIZONTAL_RULE_RE.match(str(line or "").strip()))


def _is_code_fence_line(line: str) -> bool:
    match = _FENCE_LINE_RE.match(str(line or "").rstrip())
    return bool(match and len(match.group(1)) >= 3)


async def render_markdown_image(content: str, title: str, data_store: Any) -> Path:
    """Render a bounded, escaped Markdown-like response using bundled Chromium."""
    text = str(content or "").strip()
    if not text:
        raise ValueError("content 不能为空")
    if len(text) > _MAX_CONTENT_CHARS:
        raise ValueError(f"content 过长，最多 {_MAX_CONTENT_CHARS} 个字符")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("富文本图片需要 Playwright；请重新部署包含 Chromium 的镜像") from exc

    output_dir = _artifact_dir(data_store)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"markdown_{uuid4().hex}.png"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(
                viewport={"width": 960, "height": 800}, device_scale_factor=1
            )
            await page.set_content(build_markdown_card_html(text, title), wait_until="load")
            await page.locator("#markdown-card").screenshot(path=str(output_path))
        finally:
            await browser.close()
    return output_path


async def render_and_send_rich_reply(
    content: str,
    *,
    adapter: Any,
    group_id: str,
    title: str = "",
) -> dict[str, str]:
    """把整段结构化回复渲染成图片发出，再补发同内容的合并转发消息。

    返回 ``{"image_message_id": ..., "forward_message_id": ...}``。合并转发只是
    让用户能复制原文的备份，发送失败只记日志，不影响已经发出的图片。
    """
    target = str(group_id or "").strip()
    client = getattr(adapter, "adapter", None) or adapter
    if not client or not target:
        raise RuntimeError("富文本图片发送缺少平台适配器或聊天目标")

    private = target.startswith("private_")
    target_id = target.removeprefix("private_").removeprefix("qq_")
    image_path = await render_markdown_image(content, title, data_store=None)
    try:
        image = [{"type": "image", "data": {"file": to_image_reference(str(image_path))}}]
        if private:
            response = await client.send_private_msg(target_id, image)
        else:
            response = await client.send_group_msg(target, image)
    finally:
        try:
            image_path.unlink(missing_ok=True)
        except OSError:
            pass

    return {
        "image_message_id": _response_message_id(response),
        "forward_message_id": await _send_merged_forward(
            client, target_id, content, private=private
        ),
    }


async def _send_merged_forward(client: Any, target_id: str, content: str, *, private: bool) -> str:
    """调用平台适配器补发合并转发消息；适配器不支持或发送失败时返回空串。"""
    sender = getattr(
        client, "send_private_forward_msg" if private else "send_group_forward_msg", None
    )
    if not callable(sender):
        return ""
    try:
        response = await sender(target_id, content)
    except Exception as exc:  # noqa: BLE001 - 合并转发是备份，失败不影响图片
        LOG.warning("合并转发消息发送失败: %s", exc)
        return ""
    return _response_message_id(response)


def _response_message_id(response: Any) -> str:
    data = response.get("data", {}) if isinstance(response, dict) else {}
    return str(data.get("message_id") or "") if isinstance(data, dict) else ""


def to_image_reference(image_path: str) -> str:
    """Encode local image data so the platform need not access this process's filesystem."""
    if image_path.startswith(("http://", "https://", "data:", "base64://")):
        return image_path
    path = Path(image_path.removeprefix("file://")).expanduser()
    if not path.is_file():
        return image_path
    return f"base64://{base64.b64encode(path.read_bytes()).decode('ascii')}"


def build_markdown_card_html(content: str, title: str = "") -> str:
    """Build escaped HTML for the small Markdown subset used in chat replies."""
    clean_title = str(title or "").strip()[:_MAX_TITLE_CHARS]
    heading = f"<h1>{_inline_html(clean_title)}</h1>" if clean_title else ""
    font_face = _cute_font_face_css()
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
{font_face}
* {{ box-sizing: border-box; }}
html {{ background: #dfe4df; }}
body {{ margin: 0; padding: 24px; background: #dfe4df; color: #202a2b; font-family: "Sirius Cute", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; }}
#markdown-card {{ width: 900px; background: #f7f7f2; border: 1px solid #1c2b2e; border-radius: 8px; box-shadow: 8px 8px 0 #b8c4bc; overflow: hidden; }}
.masthead {{ display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; min-height: 132px; padding: 25px 36px 24px; background: #18252a; color: #f3f0e7; }}
.identity {{ display: flex; align-items: center; gap: 16px; }}
.signal-mark {{ display: grid; grid-template-columns: repeat(3, 7px); gap: 4px; width: 38px; padding: 7px; border: 1px solid #7bd7c6; background: #26383b; }}
.signal-mark span {{ display: block; height: 7px; background: #7bd7c6; }}
.signal-mark span:nth-child(2), .signal-mark span:nth-child(5), .signal-mark span:nth-child(8) {{ background: #f0b85d; }}
.signal-mark span:nth-child(3), .signal-mark span:nth-child(7) {{ background: #e36b52; }}
.eyebrow, .telemetry-label, .footer {{ font-family: "Cascadia Mono", Consolas, monospace; letter-spacing: 1.4px; text-transform: uppercase; }}
.eyebrow {{ color: #7bd7c6; font-size: 11px; font-weight: 700; }}
.wordmark {{ margin-top: 5px; color: #f3f0e7; font-size: 27px; font-weight: 800; letter-spacing: 1px; }}
.telemetry {{ min-width: 160px; padding-left: 16px; border-left: 1px solid #536467; }}
.telemetry-label {{ color: #a8b8b5; font-size: 10px; }}
.telemetry-value {{ margin-top: 8px; color: #f0b85d; font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; font-weight: 700; }}
.accent {{ height: 8px; background: #e36b52; position: relative; }}
.accent::after {{ content: ""; position: absolute; top: 0; right: 0; width: 34%; height: 100%; background: #f0b85d; }}
.content {{ padding: 34px 42px 37px; }}
h1 {{ color: #18252a; font-size: 30px; line-height: 1.3; margin: 0 0 24px; overflow-wrap: anywhere; }}
h2 {{ color: #18252a; font-size: 24px; line-height: 1.35; margin: 28px 0 12px; padding-bottom: 8px; border-bottom: 2px solid #b8c4bc; }}
h3 {{ color: #b34e3d; font-size: 19px; line-height: 1.4; margin: 24px 0 10px; }}
h4 {{ color: #55716c; font-size: 16px; line-height: 1.4; margin: 20px 0 8px; }}
p, li, blockquote {{ font-size: 17px; line-height: 1.75; overflow-wrap: anywhere; }}
p {{ margin: 0 0 15px; overflow-wrap: anywhere; }}
ul, ol {{ margin: 8px 0 18px; padding-left: 28px; }}
li {{ margin: 4px 0; padding-left: 4px; }}
blockquote {{ border-left: 5px solid #e36b52; background: #e8eeea; color: #4a605e; margin: 18px 0; padding: 7px 15px; }}
hr {{ height: 1px; margin: 29px 0; border: 0; background: #b8c4bc; position: relative; }}
hr::after {{ content: ""; position: absolute; top: -2px; left: 0; width: 36px; height: 5px; background: #7bd7c6; }}
pre {{ background: #202d31; border: 0; border-left: 5px solid #7bd7c6; border-radius: 3px; color: #e8f0e9; font-family: "Cascadia Mono", Consolas, monospace; font-size: 14px; line-height: 1.65; margin: 18px 0; overflow-wrap: anywhere; padding: 16px; white-space: pre-wrap; }}
code {{ background: #e4ebe5; border-radius: 3px; color: #b34e3d; font-family: "Cascadia Mono", Consolas, monospace; font-size: .9em; padding: 2px 4px; }}
pre code {{ background: transparent; color: inherit; padding: 0; }}
strong {{ color: #18252a; }} em {{ color: #55716c; }}
table {{ width: 100%; border-collapse: collapse; margin: 18px 0 22px; table-layout: fixed; }}
th, td {{ border: 1px solid #c9d2cb; padding: 9px 11px; vertical-align: top; text-align: left; overflow-wrap: anywhere; }}
th {{ background: #e8eeea; color: #18252a; font-weight: 800; }}
tbody tr:nth-child(even) td {{ background: #f1f4ef; }}
.table-line {{ background: #edf1eb; border-left: 3px solid #f0b85d; font-family: "Cascadia Mono", Consolas, monospace; font-size: 14px; padding: 7px 10px; white-space: pre-wrap; }}
.footer {{ display: flex; justify-content: space-between; gap: 20px; border-top: 1px solid #c9d2cb; color: #70827d; font-size: 10px; margin-top: 30px; padding-top: 14px; }}
</style></head><body><article id="markdown-card"><header class="masthead"><div class="identity"><div class="signal-mark" aria-hidden="true"><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span></div><div><div class="eyebrow">SIRIUS / RESPONSE ARCHIVE</div><div class="wordmark">FIELD NOTE</div></div></div><div class="telemetry"><div class="telemetry-label">Output mode</div><div class="telemetry-value">MERGED RESPONSE</div></div></header><div class="accent"></div><div class="content">{heading}{_markdown_body_html(content)}<div class="footer"><span>SIRIUS CHAT</span><span>ONE REPLY / MANY SIGNALS</span></div></div></article></body></html>"""


def _cute_font_face_css() -> str:
    try:
        encoded = base64.b64encode(_CUTE_FONT_PATH.read_bytes()).decode("ascii")
    except OSError:
        return ""
    return (
        "@font-face {"
        "font-family: 'Sirius Cute';"
        f"src: url(data:font/ttf;base64,{encoded}) format('truetype');"
        "font-style: normal; font-weight: 400; font-display: block;"
        "}"
    )


def _markdown_body_html(content: str) -> str:
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_tag = ""
    table_lines: list[str] = []
    code_lines: list[str] = []
    in_code = False

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{'<br>'.join(_inline_html(line) for line in paragraph)}</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_items:
            blocks.append(f"<{list_tag}>{''.join(list_items)}</{list_tag}>")
            list_items.clear()
        list_tag = ""

    def flush_code() -> None:
        if code_lines:
            blocks.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
            code_lines.clear()

    def flush_table() -> None:
        if not table_lines:
            return
        if len(table_lines) < 2 or not _is_table_separator(table_lines[1]):
            blocks.extend(f'<p class="table-line">{_inline_html(line)}</p>' for line in table_lines)
            table_lines.clear()
            return

        header = _table_cells(table_lines[0])
        alignments = _table_alignments(table_lines[1])
        rows = [_table_cells(line) for line in table_lines[2:]]
        column_count = max([len(header), *(len(row) for row in rows)], default=0)
        header.extend([""] * (column_count - len(header)))
        alignments.extend([""] * (column_count - len(alignments)))

        def cell_html(tag: str, value: str, alignment: str) -> str:
            align = f' align="{alignment}"' if alignment else ""
            return f"<{tag}{align}>{_inline_html(value)}</{tag}>"

        head_html = "".join(
            cell_html("th", header[index], alignments[index]) for index in range(column_count)
        )
        body_html = "".join(
            "<tr>"
            + "".join(
                cell_html(
                    "td",
                    row[index] if index < len(row) else "",
                    alignments[index],
                )
                for index in range(column_count)
            )
            + "</tr>"
            for row in rows
        )
        blocks.append(
            f"<table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>"
        )
        table_lines.clear()

    for raw_line in str(content or "").strip().splitlines():
        line = raw_line.rstrip()
        if _is_code_fence_line(line):
            flush_table()
            flush_paragraph()
            flush_list()
            if in_code:
                flush_code()
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not line.strip():
            flush_table()
            flush_paragraph()
            flush_list()
            continue

        clean_line = line.strip()
        if _is_horizontal_rule_line(clean_line):
            flush_table()
            flush_paragraph()
            flush_list()
            blocks.append("<hr>")
            continue

        heading_match = _HEADING_RE.match(clean_line)
        if heading_match:
            flush_table()
            flush_paragraph()
            flush_list()
            level = min(4, len(heading_match.group(1)) + 1)
            blocks.append(f"<h{level}>{_inline_html(heading_match.group(2))}</h{level}>")
            continue
        if clean_line.startswith(">"):
            flush_table()
            flush_paragraph()
            flush_list()
            blocks.append(f"<blockquote>{_inline_html(clean_line[1:].lstrip())}</blockquote>")
            continue

        ordered_match = _ORDERED_ITEM_RE.match(clean_line)
        bullet_match = _BULLET_ITEM_RE.match(clean_line)
        if ordered_match or bullet_match:
            flush_table()
            flush_paragraph()
            next_tag = "ol" if ordered_match else "ul"
            if list_tag and list_tag != next_tag:
                flush_list()
            list_tag = next_tag
            item = ordered_match.group(1) if ordered_match else bullet_match.group(1)
            list_items.append(f"<li>{_inline_html(item)}</li>")
            continue

        flush_list()
        if _is_table_row_line(line):
            flush_paragraph()
            table_lines.append(line.strip())
        else:
            flush_table()
            paragraph.append(line)

    if in_code:
        flush_code()
    flush_table()
    flush_paragraph()
    flush_list()
    return "".join(blocks)


def _table_cells(line: str) -> list[str]:
    clean_line = str(line or "").strip()
    if clean_line.startswith("|"):
        clean_line = clean_line[1:]
    if clean_line.endswith("|"):
        clean_line = clean_line[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", clean_line)]


def _is_table_separator(line: str) -> bool:
    cells = _table_cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _table_alignments(line: str) -> list[str]:
    alignments: list[str] = []
    for cell in _table_cells(line):
        if cell.startswith(":") and cell.endswith(":"):
            alignments.append("center")
        elif cell.startswith(":"):
            alignments.append("left")
        elif cell.endswith(":"):
            alignments.append("right")
        else:
            alignments.append("")
    return alignments


def _inline_html(text: str) -> str:
    escaped = html.escape(str(text or ""))
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"(\*\*|__)(.+?)\1", r"<strong>\2</strong>", escaped)
    return re.sub(r"(?<!\*)\*([^*]+)\*", r"<em>\1</em>", escaped)


def _artifact_dir(data_store: Any) -> Path:
    artifact_dir = getattr(data_store, "artifact_dir", None)
    if isinstance(artifact_dir, Path):
        return artifact_dir
    if artifact_dir:
        return Path(str(artifact_dir))
    return Path(tempfile.gettempdir()) / "sirius_pulse" / "markdown_image"
