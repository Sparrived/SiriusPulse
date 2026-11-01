"""Render a whole structured reply into one shareable image.

当回复里出现行内符号之外的排版结构（代码块、表格、制表符、分隔线、标题、
引用、列表块）时，纯文本已经无法还原排版，因此整段内容都会渲染成一张图片；
同时通过平台适配器补发一条合并转发消息，保留可复制的原文。

此外，回复按换行符会被拆成多条消息；一旦超过 ``_MAX_TEXT_MESSAGES`` 条，
刷屏的代价比一张图片更大，同样改成「图片 + 合并转发」投递。
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

try:  # Pygments 只服务代码块高亮；缺失时整块退化为纯转义文本。
    from pygments import highlight as _pygments_highlight
    from pygments.formatters import HtmlFormatter as _HtmlFormatter
    from pygments.lexers import TextLexer as _TextLexer
    from pygments.lexers import get_lexer_by_name as _get_lexer_by_name
    from pygments.style import Style as _PygmentsStyle
    from pygments.token import (
        Comment,
        Error,
        Generic,
        Keyword,
        Name,
        Number,
        Operator,
        Punctuation,
        String,
    )
    from pygments.util import ClassNotFound as _PygmentsClassNotFound

    class _CardCodeStyle(_PygmentsStyle):
        """与卡片配色同源的浅色代码高亮方案（月白底、墨蓝字、樱粉关键字）。"""

        background_color = "#f7f6ff"
        default_style = "#2d3154"
        styles = {
            Comment: "italic #8b8fae",
            Error: "#d1477a",
            Generic.Deleted: "#d1477a",
            Generic.Emph: "italic #2d3154",
            Generic.Heading: "bold #232849",
            Generic.Inserted: "#23866a",
            Generic.Prompt: "#8b8fae",
            Generic.Strong: "bold #232849",
            Generic.Subheading: "bold #3a5bc7",
            Keyword: "bold #d1477a",
            Name.Attribute: "#b86e12",
            Name.Builtin: "#7a52c7",
            Name.Class: "bold #3a5bc7",
            Name.Decorator: "#7a52c7",
            Name.Function: "bold #3a5bc7",
            Name.Tag: "bold #d1477a",
            Number: "#b86e12",
            Operator: "#6a6f96",
            Punctuation: "#7c80a3",
            String: "#23866a",
        }

    _HIGHLIGHT_READY = True
except ImportError:  # pragma: no cover - 只有没装 Pygments 的部署会走到
    _HIGHLIGHT_READY = False

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
# 平台按换行符把一段回复拆成多条消息；超过这个条数就该整段转图片，避免刷屏。
_MAX_TEXT_MESSAGES = 3
# 三行以内的普通文本不足这个字数时，删掉换行合并成一条消息，避免碎片化。
_SHORT_TEXT_MERGE_WEIGHT = 50.0
# 常见但 Pygments 认不出的围栏标签，映射到它支持的别名。
_CODE_LEXER_ALIASES = {
    "yml": "yaml",
    "txt": "text",
    "plaintext": "text",
    "jsonc": "json",
    "patch": "diff",
    "kt": "kotlin",
    "cmd": "batch",
    "csv": "text",
    "log": "text",
    "textile": "text",
    "mermaid": "text",
    "svelte": "html",
    "astro": "html",
}
_CUTE_FONT_PATH = Path(__file__).with_name("assets") / "ZCOOLKuaiLe-Regular.ttf"
# 卡片按手机阅读设计：版心窄、字号大，再以 2 倍像素截图保证放大后依旧清晰。
_CARD_VIEWPORT_WIDTH = 800
_CARD_SCALE = 2
_MAX_IMAGE_PIXEL_HEIGHT = 14_000


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


def should_send_as_image(text: str) -> bool:
    """判断一段回复是否该整段转成图片投递。

    两种情况：含有换行符之外的排版结构（表格、代码块、标题……），
    或者按换行符会拆成超过 ``_MAX_TEXT_MESSAGES`` 条消息——后者刷屏的
    阅读代价比一张图片更大。这里只决定"怎么投递"，不改动回复内容本身。
    """
    if has_rich_structure(text):
        return True
    return count_text_messages(text) > _MAX_TEXT_MESSAGES


def merge_short_plain_text(text: str) -> str:
    """把三行以内的普通短文本合并成一条消息。

    平台会按换行把一段回复拆成多条消息。回复本身没有 Markdown 排版需求、
    总字数又不足 50 时（ASCII 字母、数字、符号按半个字计），逐行发送只会
    制造碎片；这里删除换行符后再交给发送层，其余正文保持不变。
    """
    source = str(text or "")
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    if len(lines) < 2:
        return text
    if should_send_as_image(source):
        return text
    if _weighted_text_length(source) >= _SHORT_TEXT_MERGE_WEIGHT:
        return text

    merged = lines[0]
    for line in lines[1:]:
        if _needs_join_separator(merged, line):
            merged += " "
        merged += line
    return merged


def _weighted_text_length(text: str) -> float:
    """按中文一字、ASCII 半字估算回复字数，空白不计。"""
    total = 0.0
    for char in str(text or ""):
        if not char.isspace():
            total += 0.5 if ord(char) < 128 else 1.0
    return total


def _needs_join_separator(left: str, right: str) -> bool:
    """删除换行时是否要补空格，避免英文单词、URL 或 @昵称粘连。"""
    if not left or not right:
        return False
    if left[-1].isascii():
        return True
    # @昵称 后面直接接正文会让昵称匹配吞掉下一行，补一个空格保住边界。
    return bool(re.search(r"@[^\s@]+$", left))


def _normalize_fence_chars(text: str) -> str:
    return str(text or "").replace("｀", "`").replace("～", "~")


def _is_table_row_line(line: str) -> bool:
    return str(line or "").count("|") >= 2


def _is_horizontal_rule_line(line: str) -> bool:
    return bool(_HORIZONTAL_RULE_RE.match(str(line or "").strip()))


def _is_code_fence_line(line: str) -> bool:
    match = _FENCE_LINE_RE.match(str(line or "").rstrip())
    return bool(match and len(match.group(1)) >= 3)


def _code_fence_language(line: str) -> str:
    """取围栏行上的语言标签，例如 ```python 里的 python。"""
    match = _FENCE_LINE_RE.match(str(line or "").rstrip())
    if not match or len(match.group(1)) < 3:
        return ""
    return match.group(2).strip().split()[0].lower() if match.group(2).strip() else ""


def count_text_messages(text: str) -> int:
    """按平台拆分规则估算这段文本会占几条消息。

    适配器最终兜底是按换行符拆分并丢弃空行，这里保持一致，
    让「会拆成几条」在投递前就能判定。
    """
    return len([line for line in str(text or "").splitlines() if line.strip()])


def _highlight_code_block(code: str, language: str) -> str:
    """把代码块渲染成带语法高亮的 HTML，无法高亮时退回纯转义文本。

    输出沿用 Pygments 的行内样式（``noclasses=True``），样式直接挂在 span 上，
    既不用往卡片里注入额外 CSS，也不会污染卡片自己的 ``pre``/``code`` 规则。
    """
    if not _HIGHLIGHT_READY:
        return html.escape(code)

    name = _CODE_LEXER_ALIASES.get(language, language)
    try:
        lexer = _get_lexer_by_name(name, stripnl=False, ensurenl=False) if name else _TextLexer()
    except _PygmentsClassNotFound:
        lexer = _TextLexer()
    except Exception:  # pragma: no cover - 防御异常 lexer
        return html.escape(code)

    try:
        rendered = _pygments_highlight(
            code,
            lexer,
            _HtmlFormatter(nowrap=True, noclasses=True, style=_CardCodeStyle),
        )
    except Exception:  # pragma: no cover - 高亮失败不应该拖垮整张卡片
        return html.escape(code)
    # Pygments 恒定在结尾补一个换行，卡片里由 CSS 控制间距，这里去掉。
    return rendered.rstrip("\n") if rendered.endswith("\n") else rendered


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

    card_html = build_markdown_card_html(text, title)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(
                viewport={"width": _CARD_VIEWPORT_WIDTH, "height": 800},
                device_scale_factor=_CARD_SCALE,
            )
            await page.set_content(card_html, wait_until="load")
            card = page.locator("#markdown-card")
            box = await card.bounding_box()
            if box and box["height"] * _CARD_SCALE > _MAX_IMAGE_PIXEL_HEIGHT:
                # 超长卡片按 2 倍截图会超出 Chromium 与聊天平台的尺寸上限，退回 1 倍。
                await page.close()
                page = await browser.new_page(
                    viewport={"width": _CARD_VIEWPORT_WIDTH, "height": 800},
                    device_scale_factor=1,
                )
                await page.set_content(card_html, wait_until="load")
                card = page.locator("#markdown-card")
            await card.screenshot(path=str(output_path))
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
    heading = (
        f"<h1>{_inline_html(clean_title)}</h1>"
        if clean_title
        else '<span class="note-label">小纸条</span>'
    )
    font_face = _cute_font_face_css()
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
{font_face}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: #f1efff; }}
body {{ color: #2d3154; font-family: "Noto Sans CJK SC", "Source Han Sans SC", "PingFang SC", "Microsoft YaHei", "WenQuanYi Zen Hei", sans-serif; -webkit-font-smoothing: antialiased; }}
#markdown-card {{ position: relative; width: 760px; padding: 38px 26px 26px; background-color: #f1efff; background-image: radial-gradient(circle at 12% 8%, rgba(255, 210, 225, .75), transparent 42%), radial-gradient(circle at 92% 96%, rgba(214, 226, 255, .9), transparent 46%), radial-gradient(rgba(150, 140, 220, .16) 1.4px, transparent 1.6px); background-size: 100% 100%, 100% 100%, 22px 22px; }}
.paper {{ position: relative; background: #fffdfe; border: 2px solid #ebe7fb; border-radius: 28px; box-shadow: 0 10px 0 -4px #e3defa, 0 18px 36px -18px rgba(79, 70, 160, .28); padding: 30px 38px 22px; }}
.tape {{ position: absolute; top: -17px; left: 50%; width: 132px; height: 32px; margin-left: -66px; transform: rotate(-3deg); border-radius: 4px; background: repeating-linear-gradient(135deg, rgba(255, 170, 196, .72) 0 9px, rgba(255, 196, 214, .72) 9px 18px); box-shadow: 0 2px 4px rgba(200, 120, 150, .18); }}
.sparkle {{ position: absolute; clip-path: polygon(50% 0, 61% 39%, 100% 50%, 61% 61%, 50% 100%, 39% 61%, 0 50%, 39% 39%); }}
.sparkle.s1 {{ top: 14px; left: 30px; width: 22px; height: 22px; background: #ffc85c; }}
.sparkle.s2 {{ bottom: 9px; left: 46px; width: 12px; height: 12px; background: #ff9fb8; }}
.sparkle.s3 {{ top: 12px; right: 44px; width: 15px; height: 15px; background: #9fb4ff; }}
.note-head {{ display: flex; align-items: center; gap: 12px; margin: 0 0 20px; padding-bottom: 16px; border-bottom: 2px dashed #ece8fb; }}
.moon {{ flex: none; display: grid; place-items: center; width: 42px; height: 42px; border-radius: 50%; background: #eef0ff; box-shadow: inset 0 0 0 2px #dfe4ff; }}
.moon svg {{ width: 24px; height: 24px; }}
h1, h2, h3, .note-label, .note-foot {{ font-family: "Sirius Cute", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; font-weight: 400; }}
h1 {{ margin: 0; color: #232849; font-size: 28px; line-height: 1.3; overflow-wrap: anywhere; }}
.note-label {{ color: #6a6f9c; font-size: 20px; letter-spacing: 2px; }}
.content {{ font-size: 18px; line-height: 1.8; }}
.content > :first-child {{ margin-top: 0; }}
h2 {{ display: table; margin: 26px 0 12px; padding: 0 6px; color: #232849; font-size: 25px; line-height: 1.4; overflow-wrap: anywhere; background: linear-gradient(transparent 58%, #ffe1ea 58%, #ffe1ea 90%, transparent 90%); }}
h3 {{ position: relative; margin: 22px 0 10px; padding-left: 24px; color: #3a52b0; font-size: 21px; line-height: 1.45; overflow-wrap: anywhere; }}
h3::before {{ content: ""; position: absolute; left: 2px; top: .42em; width: 13px; height: 13px; background: #9fb4ff; clip-path: polygon(50% 0, 61% 39%, 100% 50%, 61% 61%, 50% 100%, 39% 61%, 0 50%, 39% 39%); }}
h4 {{ margin: 18px 0 8px; color: #5b6188; font-size: 18px; font-weight: 700; line-height: 1.5; }}
p, li, blockquote {{ overflow-wrap: anywhere; }}
p {{ margin: 0 0 14px; }}
ul, ol {{ list-style: none; margin: 6px 0 16px; padding: 0; }}
li {{ position: relative; margin: 6px 0; padding-left: 30px; }}
ul > li::before {{ content: ""; position: absolute; left: 8px; top: .62em; width: 10px; height: 10px; border-radius: 50%; background: #ff9fb8; }}
ul > li:nth-child(3n+2)::before {{ background: #9fb4ff; }}
ul > li:nth-child(3n)::before {{ background: #ffc85c; }}
ol {{ counter-reset: item; }}
ol > li {{ counter-increment: item; padding-left: 36px; }}
ol > li::before {{ content: counter(item); position: absolute; left: 0; top: .28em; width: 25px; height: 25px; border-radius: 50%; background: #e7ecff; color: #3a52b0; font: 700 14px/25px "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; text-align: center; }}
blockquote {{ margin: 16px 0; padding: 12px 18px 12px 20px; border-radius: 16px; background: #f5f3ff; box-shadow: inset 4px 0 0 #c9c1f5; color: #4f5580; }}
hr {{ position: relative; height: 0; margin: 26px 0; border: 0; border-top: 2px dashed #e4e0f8; overflow: visible; }}
hr::after {{ content: ""; position: absolute; top: -8px; left: 50%; width: 14px; height: 14px; margin-left: -7px; background: #ffb5c8; clip-path: polygon(50% 0, 61% 39%, 100% 50%, 61% 61%, 50% 100%, 39% 61%, 0 50%, 39% 39%); box-shadow: 0 0 0 6px #fffdfe; }}
code {{ border-radius: 7px; background: #fff0f5; color: #c2416f; font-family: "Cascadia Mono", Consolas, "DejaVu Sans Mono", "Noto Sans Mono CJK SC", "Microsoft YaHei", monospace; font-size: .86em; padding: 2px 6px; }}
.code {{ margin: 16px 0 18px; border: 2px solid #e8e5fb; border-radius: 18px; background: #f7f6ff; overflow: hidden; }}
.code-head {{ display: flex; align-items: center; justify-content: space-between; padding: 9px 14px; background: #efedff; }}
.code-dots {{ display: flex; gap: 7px; }}
.code-dots i {{ display: block; width: 11px; height: 11px; border-radius: 50%; background: #ff9fb8; }}
.code-dots i:nth-child(2) {{ background: #ffc85c; }}
.code-dots i:nth-child(3) {{ background: #8fd9b8; }}
.code-lang {{ color: #7c80a3; font-family: "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace; font-size: 13px; letter-spacing: .5px; }}
pre {{ margin: 0; padding: 14px 18px 16px; color: #2d3154; font-family: "Cascadia Mono", Consolas, "DejaVu Sans Mono", "Noto Sans Mono CJK SC", "Microsoft YaHei", monospace; font-size: 15px; line-height: 1.65; overflow-wrap: anywhere; white-space: pre-wrap; }}
pre code {{ background: transparent; color: inherit; font-size: inherit; padding: 0; border-radius: 0; }}
strong {{ color: #232849; font-weight: 700; background: linear-gradient(transparent 62%, #ffe1ea 62%); padding: 0 1px; }}
em {{ color: #6a4fc0; font-style: normal; }}
table {{ width: 100%; margin: 16px 0 20px; border: 2px solid #e8e5fb; border-radius: 16px; border-collapse: separate; border-spacing: 0; overflow: hidden; table-layout: fixed; font-size: 16px; line-height: 1.6; }}
th, td {{ padding: 10px 13px; text-align: left; vertical-align: top; overflow-wrap: anywhere; border-bottom: 1px solid #efedfb; }}
th + th, td + td {{ border-left: 1px solid #efedfb; }}
tbody tr:last-child td {{ border-bottom: 0; }}
th {{ background: #eef0ff; color: #232849; font-weight: 700; }}
tbody tr:nth-child(even) td {{ background: #fbfaff; }}
th[align="center"], td[align="center"] {{ text-align: center; }}
th[align="right"], td[align="right"] {{ text-align: right; }}
.table-line {{ margin: 0 0 6px; padding: 8px 12px; border-radius: 10px; background: #f7f6ff; font-family: "Cascadia Mono", Consolas, "Microsoft YaHei", monospace; font-size: 15px; white-space: pre-wrap; }}
.note-foot {{ display: flex; align-items: center; justify-content: flex-end; gap: 10px; margin-top: 22px; padding-top: 14px; border-top: 2px dashed #ece8fb; color: #a3a6c6; font-size: 14px; letter-spacing: 1px; }}
.note-foot .code-dots i {{ width: 8px; height: 8px; }}
</style></head><body><article id="markdown-card"><span class="sparkle s1"></span><span class="sparkle s2"></span><span class="sparkle s3"></span><div class="paper"><span class="tape" aria-hidden="true"></span><header class="note-head"><span class="moon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M15.2 2.6a9.6 9.6 0 1 0 6.3 14.6A8 8 0 0 1 15.2 2.6z" fill="#ffc85c"/><circle cx="19" cy="6" r="1.3" fill="#ff9fb8"/></svg></span>{heading}</header><div class="content">{_markdown_body_html(content)}</div><footer class="note-foot"><span class="code-dots" aria-hidden="true"><i></i><i></i><i></i></span><span>sirius pulse</span></footer></div></article></body></html>"""


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
    code_language = ""
    in_code = False
    quote_open = False

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
        nonlocal code_language
        if code_lines:
            body = _highlight_code_block(chr(10).join(code_lines), code_language)
            label = f'<span class="code-lang">{html.escape(code_language)}</span>'
            blocks.append(
                '<figure class="code"><div class="code-head">'
                '<span class="code-dots" aria-hidden="true"><i></i><i></i><i></i></span>'
                f"{label if code_language else ''}</div>"
                f"<pre><code>{body}</code></pre></figure>"
            )
            code_lines.clear()
        code_language = ""

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
        # 只有紧挨着的引用行才合并；其余任何一行都会结束当前引用。
        quote_open, was_quote = False, quote_open
        if _is_code_fence_line(line):
            flush_table()
            flush_paragraph()
            flush_list()
            if in_code:
                flush_code()
            else:
                code_language = _code_fence_language(line)
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
            quote = _inline_html(clean_line[1:].lstrip())
            if was_quote:
                # 连续的引用行属于同一段引用，合进上一个气泡里。
                blocks[-1] = blocks[-1].removesuffix("</blockquote>") + f"<br>{quote}</blockquote>"
            else:
                blocks.append(f"<blockquote>{quote}</blockquote>")
            quote_open = True
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
