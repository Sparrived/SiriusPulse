from __future__ import annotations

from typing import Any

from sirius_pulse.skills.builtin import web_lookup


class _Store:
    def __init__(self, **values: Any) -> None:
        self.values = values
        self.reload_count = 0

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def reload(self) -> None:
        self.reload_count += 1


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


def test_web_lookup_search_uses_tavily_settings(monkeypatch):
    calls: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _Response:
        calls.update(url=url, kwargs=kwargs)
        return _Response(
            {
                "results": [
                    {
                        "title": "Tavily result",
                        "url": "https://example.com/result",
                        "content": "A result summary.",
                    }
                ],
            }
        )

    monkeypatch.setattr(web_lookup.requests, "post", fake_post)
    store = _Store(tavily_api_key="tvly-test")

    result = web_lookup.run("search", query="Sirius Chat", count=8, data_store=store)

    assert result["success"] is True
    assert result["results"] == [
        {
            "title": "Tavily result",
            "url": "https://example.com/result",
            "snippet": "A result summary.",
        }
    ]
    assert calls["url"] == web_lookup.TAVILY_SEARCH_URL
    assert calls["kwargs"]["json"] == {
        "api_key": "tvly-test",
        "query": "Sirius Chat",
        "topic": "general",
        "search_depth": "basic",
        "max_results": 5,
        "include_answer": False,
    }
    assert store.reload_count == 1


def test_web_lookup_search_requires_tavily_key(monkeypatch):
    monkeypatch.setattr(
        web_lookup.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected request")),
    )

    result = web_lookup.run("search", query="Sirius Chat", data_store=_Store())

    assert result == {
        "success": False,
        "error": "请先在 web_lookup 技能设置中配置 Tavily API Key",
    }
