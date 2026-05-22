"""
通过必应搜索引擎自动检索网络信息。
"""
from __future__ import annotations
from typing import Any
import requests

SKILL_META = {
    "name": "bing_search",
    "description": "使用必应搜索引擎检索指定关键词的网页摘要，返回前3条结果。",
    "version": "1.0.0",
    "tags": ["web", "search"],
    "dependencies": ["requests", "beautifulsoup4"],
    "parameters": {
        "query": {
            "type": "str",
            "description": "搜索关键词",
            "required": True,
        },
        "count": {
            "type": "int",
            "description": "返回结果条数（1-5）",
            "required": False,
            "default": 3,
        },
    },
}

SEARCH_URL = "https://www.bing.com/search"

from bs4 import BeautifulSoup

def run(query: str, count: int = 3, data_store: Any = None, **kwargs: Any) -> dict:
    """
    通过抓取必应网页搜索结果实现网络检索，无需 API Key。
    """
    # 注意：不要使用 ensearch=1，该参数在 cn.bing.com 上会导致中文搜索结果严重偏差
    params = {"q": query}
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
        for item in soup.select("li.b_algo")[:max(1, min(count, 5))]:
            title_tag = item.select_one("h2 a")
            snippet_tag = item.select_one(".b_caption p") or item.select_one("p")
            url = title_tag["href"] if title_tag and title_tag.has_attr("href") else None
            title = title_tag.get_text(strip=True) if title_tag else None
            snippet = snippet_tag.get_text(strip=True) if snippet_tag else None
            if title and url:
                results.append({"title": title, "url": url, "snippet": snippet})
        if not results:
            return {
                "success": False,
                "error": "未找到相关网页",
                "summary": "必应搜索未返回有效结果",
            }

        # Format results as human-readable text for the model
        lines: list[str] = []
        lines.append(f"必应搜索「{query}」结果：")
        for i, r in enumerate(results, 1):
            lines.append(f"\n[{i}] {r['title']}")
            lines.append(f"链接: {r['url']}")
            if r.get("snippet"):
                lines.append(f"摘要: {r['snippet']}")

        return {
            "success": True,
            "results": results,
            "summary": f"找到 {len(results)} 条搜索结果",
            "text_blocks": ["\n".join(lines)],
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"搜索失败: {e}",
            "summary": "必应搜索执行失败",
        }
