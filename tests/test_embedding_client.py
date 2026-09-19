"""AMKR ``/v1/embeddings`` 客户端的契约。

业务视角：本框架把文本交给 AMKR 换回向量，模型名由配置决定；响应按 OpenAI 契约的
``index`` 归位，否则会出现「向量与文本错配」这种静默错误。
"""

from __future__ import annotations

import json

import pytest

from sirius_pulse.embedding.client import EmbeddingClient, load_embedding_model


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _patch_transport(monkeypatch, handler):
    """把 ``urllib.request.urlopen`` 换成本地假实现，并记录每次请求。"""
    from sirius_pulse.embedding import client as client_module

    calls: list[dict] = []

    def fake_urlopen(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        body = None
        if getattr(request, "data", None):
            body = json.loads(request.data.decode("utf-8"))
        calls.append({"url": url, "body": body, "headers": dict(request.headers)})
        return handler(url, body)

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_embedding_client_when_encoding_then_posts_openai_payload_with_bearer_token(monkeypatch):
    def handler(url, body):
        return _FakeResponse(
            {
                "data": [
                    {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                    {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                ]
            }
        )

    calls = _patch_transport(monkeypatch, handler)
    client = EmbeddingClient(
        base_url="http://amkr:8000", api_key="amkr_ik_secret", model="BAAI/bge-m3"
    )

    vectors = client.encode(["你好", "世界"])

    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    assert client.dimension == 3
    assert calls[0]["url"] == "http://amkr:8000/v1/embeddings"
    assert calls[0]["body"] == {"model": "BAAI/bge-m3", "input": ["你好", "世界"]}
    assert calls[0]["headers"]["Authorization"] == "Bearer amkr_ik_secret"


def test_embedding_client_when_response_is_out_of_order_then_aligns_to_input(monkeypatch):
    """OpenAI 契约允许乱序返回；归位错了会静默地把向量配错文本。"""

    def handler(url, body):
        return _FakeResponse(
            {
                "data": [
                    {"index": 1, "embedding": [9.0, 9.0]},
                    {"index": 0, "embedding": [1.0, 1.0]},
                ]
            }
        )

    _patch_transport(monkeypatch, handler)
    client = EmbeddingClient(base_url="http://amkr:8000", model="BAAI/bge-m3")

    assert client.encode(["first", "second"]) == [[1.0, 1.0], [9.0, 9.0]]


def test_embedding_client_when_http_error_then_reports_status_and_body(monkeypatch):
    import urllib.error

    def handler(url, body):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    _patch_transport(monkeypatch, handler)
    client = EmbeddingClient(base_url="http://amkr:8000", model="BAAI/bge-m3")

    with pytest.raises(RuntimeError, match="404"):
        client.encode(["x"])


def test_embedding_client_when_too_few_vectors_then_raises(monkeypatch):
    def handler(url, body):
        return _FakeResponse({"data": [{"index": 0, "embedding": [0.1]}]})

    _patch_transport(monkeypatch, handler)
    client = EmbeddingClient(base_url="http://amkr:8000", model="BAAI/bge-m3")

    with pytest.raises(RuntimeError, match="数量不足"):
        client.encode(["a", "b"])


def test_embedding_client_when_no_key_then_omits_authorization_header(monkeypatch):
    """无凭据时不该发一个空的 ``Bearer ``：那会把「没配」伪装成「配错了」。"""

    def handler(url, body):
        return _FakeResponse({"data": [{"index": 0, "embedding": [0.1]}]})

    calls = _patch_transport(monkeypatch, handler)
    client = EmbeddingClient(base_url="http://amkr:8000", model="BAAI/bge-m3")

    client.encode(["x"])

    assert "Authorization" not in calls[0]["headers"]


def test_embedding_client_when_model_missing_from_health_then_not_ready(monkeypatch):
    def handler(url, body):
        return _FakeResponse({"status": "ok", "models": ["some-chat-model"]})

    _patch_transport(monkeypatch, handler)
    client = EmbeddingClient(base_url="http://amkr:8000", model="BAAI/bge-m3")

    assert client.check_health() is False


def test_embedding_client_when_model_present_in_health_then_ready(monkeypatch):
    def handler(url, body):
        return _FakeResponse({"status": "ok", "models": ["BAAI/bge-m3", "chat-model"]})

    calls = _patch_transport(monkeypatch, handler)
    client = EmbeddingClient(base_url="http://amkr:8000", model="BAAI/bge-m3")

    assert client.check_health() is True
    assert calls[0]["url"] == "http://amkr:8000/health"


def test_load_embedding_model_when_unset_then_uses_bge_m3(tmp_path):
    assert load_embedding_model(tmp_path) == "BAAI/bge-m3"


def test_load_embedding_model_when_env_set_then_env_wins(tmp_path, monkeypatch):
    (tmp_path / "global_config.json").write_text(
        json.dumps({"embedding_model": "from-file"}), encoding="utf-8"
    )
    monkeypatch.setenv("SIRIUS_EMBEDDING_MODEL", "from-env")

    assert load_embedding_model(tmp_path) == "from-env"
