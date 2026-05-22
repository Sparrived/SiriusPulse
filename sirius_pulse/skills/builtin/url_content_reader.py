"""读取并提取网页链接内容，帮助 AI 理解页面主题与关键信息。"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import requests
from bs4 import BeautifulSoup

SKILL_META = {
    "name": "url_content_reader",
    "description": "读取网页链接并提取标题、描述、正文摘要，帮助AI理解链接内容。",
    "version": "1.0.0",
    "tags": ["web", "content"],
    "dependencies": ["requests", "beautifulsoup4"],
    "parameters": {
        "url": {
            "type": "str",
            "description": "要读取的网页链接，例如 https://example.com/article",
            "required": True,
        },
        "max_chars": {
            "type": "int",
            "description": "正文最多返回字符数，默认3000，范围300-12000",
            "required": False,
            "default": 3000,
        },
        "timeout": {
            "type": "int",
            "description": "HTTP请求超时秒数，默认12，范围3-60",
            "required": False,
            "default": 12,
        },
    },
}


def run(
    url: str,
    max_chars: int = 3000,
    timeout: int = 12,
    data_store: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if not url or not isinstance(url, str):
        return {"error": "url 不能为空"}

    max_chars = _clamp_int(max_chars, default=3000, low=300, high=12000)
    timeout = _clamp_int(timeout, default=12, low=3, high=60)

    normalized_url = url.strip()
    if not re.match(r"^https?://", normalized_url, flags=re.IGNORECASE):
        return {"error": "仅支持 http/https 链接"}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    try:
        response = requests.get(
            normalized_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        )
        response.raise_for_status()
    except Exception as exc:
        return {"error": f"请求失败: {exc}"}

    content_type = (response.headers.get("Content-Type") or "").lower()
    final_url = response.url

    if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
        text = response.text.strip()
        excerpt = _normalize_text(text)[:max_chars]
        result = {
            "url": normalized_url,
            "final_url": final_url,
            "content_type": content_type or "unknown",
            "title": "",
            "description": "",
            "content_excerpt": excerpt,
            "content_length": len(excerpt),
            "note": "目标不是标准 HTML 页面，返回原始文本摘要",
        }
        _save_history(data_store, normalized_url, final_url, result.get("title", ""))
        return result

    html = response.text
    soup = BeautifulSoup(html, "html.parser")

    # 移除噪音节点，避免导航和脚本内容干扰摘要
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    meta_desc = ""
    meta_desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if meta_desc_tag:
        meta_desc = str(meta_desc_tag.get("content", "")).strip()

    og_desc_tag = soup.find("meta", attrs={"property": re.compile(r"^og:description$", re.I)})
    if not meta_desc and og_desc_tag:
        meta_desc = str(og_desc_tag.get("content", "")).strip()

    headings: list[str] = []
    for h in soup.find_all(["h1", "h2", "h3"]):
        t = _normalize_text(h.get_text(" ", strip=True))
        if t:
            headings.append(t)
        if len(headings) >= 8:
            break

    text = _normalize_text(soup.get_text("\n", strip=True))
    excerpt = text[:max_chars]

    result = {
        "url": normalized_url,
        "final_url": final_url,
        "content_type": content_type or "text/html",
        "title": title,
        "description": meta_desc,
        "headings": headings,
        "content_excerpt": excerpt,
        "content_length": len(excerpt),
        "truncated": len(text) > len(excerpt),
    }

    _save_history(data_store, normalized_url, final_url, title)
    return result


def _normalize_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        n = int(value)
    except Exception:
        n = default
    if n < low:
        return low
    if n > high:
        return high
    return n


def _save_history(data_store: Any, url: str, final_url: str, title: str) -> None:
    if data_store is None:
        return
    history = data_store.get("history", [])
    history.append(
        {
            "time": datetime.now().isoformat(),
            "url": url,
            "final_url": final_url,
            "title": title,
        }
    )
    data_store.set("history", history[-30:])
