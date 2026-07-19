"""Built-in skill for searching the web or reading a URL."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import requests
from bs4 import BeautifulSoup

from sirius_pulse.config.config_builder import ConfigBuilder

SEARCH_URL = "https://www.bing.com/search"

_config = ConfigBuilder()
_config.group("网页查询").add(
    "action",
    type="str",
    description="操作类型：search 搜索关键词；read_url 读取网页链接。",
    required=True,
    choices=["search", "read_url"],
)
_config.group("网页查询").add(
    "query",
    type="str",
    description="搜索关键词；action=search 时必填。",
)
_config.group("网页查询").add(
    "url",
    type="str",
    description="要读取的网页链接；action=read_url 时必填。",
)
_config.group("网页查询").add(
    "count",
    type="int",
    description="搜索返回结果条数（1-5），默认3。",
    default=3,
)
_config.group("网页查询").add(
    "max_chars",
    type="int",
    description="读取网页正文最多返回字符数，默认3000，范围300-12000。",
    default=3000,
)
_config.group("网页查询").add(
    "timeout",
    type="int",
    description="读取网页 HTTP 请求超时秒数，默认12，范围3-60。",
    default=12,
)

SKILL_META = {
    "name": "web_lookup",
    "description": (
        "群聊里需要查外部资料、最新信息、陌生名词，或有人发网页链接/文章/公告让你看看时使用；"
        "可搜索关键词或读取 URL，拿到结果后再用自然口吻概括回应。"
    ),
    "version": "1.0.0",
    "retry_safe": True,
    "side_effect": "read_only",
    "tags": ["web", "search", "content"],
    "dependencies": ["requests", "beautifulsoup4"],
    "parameters": _config.build(),
}


def run(
    action: str,
    query: str = "",
    url: str = "",
    count: int = 3,
    max_chars: int = 3000,
    timeout: int = 12,
    data_store: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    action_key = str(action or "").strip().lower()
    if action_key == "search":
        return _search_web(query, count)
    if action_key == "read_url":
        return _read_url(url, max_chars, timeout, data_store)
    return {"success": False, "error": "action 必须是 search 或 read_url"}


def _search_web(query: str, count: int = 3) -> dict[str, Any]:
    text = str(query or "").strip()
    if not text:
        return {"success": False, "error": "query 不能为空"}
    params = {"q": text}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        resp = requests.get(SEARCH_URL, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[dict[str, Any]] = []
        for item in soup.select("li.b_algo")[: max(1, min(int(count), 5))]:
            title_tag = item.select_one("h2 a")
            snippet_tag = item.select_one(".b_caption p") or item.select_one("p")
            result_url = title_tag["href"] if title_tag and title_tag.has_attr("href") else None
            title = title_tag.get_text(strip=True) if title_tag else None
            snippet = snippet_tag.get_text(strip=True) if snippet_tag else None
            if title and result_url:
                results.append({"title": title, "url": result_url, "snippet": snippet})
        if not results:
            return {
                "success": False,
                "error": "未找到相关网页",
                "summary": "必应搜索未返回有效结果",
            }

        lines = [f"必应搜索「{text}」结果："]
        for i, result in enumerate(results, 1):
            lines.append(f"\n[{i}] {result['title']}")
            lines.append(f"链接: {result['url']}")
            if result.get("snippet"):
                lines.append(f"摘要: {result['snippet']}")
        return {
            "success": True,
            "results": results,
            "summary": f"找到 {len(results)} 条搜索结果",
            "text_blocks": ["\n".join(lines)],
        }
    except Exception as exc:
        return {"success": False, "error": f"搜索失败: {exc}", "summary": "必应搜索执行失败"}


def _read_url(
    url: str,
    max_chars: int = 3000,
    timeout: int = 12,
    data_store: Any = None,
) -> dict[str, Any]:
    if not url or not isinstance(url, str):
        return {"success": False, "error": "url 不能为空"}

    max_chars = _clamp_int(max_chars, default=3000, low=300, high=12000)
    timeout = _clamp_int(timeout, default=12, low=3, high=60)
    normalized_url = url.strip()
    if not re.match(r"^https?://", normalized_url, flags=re.IGNORECASE):
        return {"success": False, "error": "仅支持 http/https 链接"}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        response = requests.get(
            normalized_url, headers=headers, timeout=timeout, allow_redirects=True
        )
        response.raise_for_status()
    except Exception as exc:
        return {"success": False, "error": f"请求失败: {exc}"}

    content_type = (response.headers.get("Content-Type") or "").lower()
    final_url = response.url
    if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
        excerpt = _normalize_text(response.text.strip())[:max_chars]
        result = {
            "success": True,
            "url": normalized_url,
            "final_url": final_url,
            "content_type": content_type or "unknown",
            "title": "",
            "description": "",
            "content_excerpt": excerpt,
            "content_length": len(excerpt),
            "note": "目标不是标准 HTML 页面，返回原始文本摘要",
        }
        _save_history(data_store, normalized_url, final_url, "")
        return result

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    meta_desc = ""
    meta_desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if meta_desc_tag:
        meta_desc = str(meta_desc_tag.get("content", "")).strip()
    og_desc_tag = soup.find("meta", attrs={"property": re.compile(r"^og:description$", re.I)})
    if not meta_desc and og_desc_tag:
        meta_desc = str(og_desc_tag.get("content", "")).strip()

    headings: list[str] = []
    for heading in soup.find_all(["h1", "h2", "h3"]):
        heading_text = _normalize_text(heading.get_text(" ", strip=True))
        if heading_text:
            headings.append(heading_text)
        if len(headings) >= 8:
            break

    text = _normalize_text(soup.get_text("\n", strip=True))
    excerpt = text[:max_chars]
    result = {
        "success": True,
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
    return max(low, min(n, high))


def _save_history(data_store: Any, url: str, final_url: str, title: str) -> None:
    if data_store is None:
        return
    history = data_store.get("history", [])
    history.append(
        {"time": datetime.now().isoformat(), "url": url, "final_url": final_url, "title": title}
    )
    data_store.set("history", history[-30:])
