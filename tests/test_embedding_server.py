from __future__ import annotations


def test_embedding_server_main_when_called_then_configures_logging_with_keyword_args(monkeypatch):
    from aiohttp import web

    from sirius_pulse.embedding import server

    app = object()
    calls: dict[str, object] = {}

    def fake_create_app(**kwargs):
        calls["create_app_kwargs"] = kwargs
        return app

    def fake_run_app(created_app, **kwargs):
        calls["run_app_app"] = created_app
        calls["run_app_kwargs"] = kwargs

    monkeypatch.setattr("sys.argv", ["embedding.server", "--port", "18901"])
    monkeypatch.setattr(server, "create_app", fake_create_app)
    monkeypatch.setattr(web, "run_app", fake_run_app)

    server.main()

    assert calls["run_app_app"] is app
    assert calls["run_app_kwargs"] == {"port": 18901, "print": None}
    assert calls["create_app_kwargs"]["model_name"] == server.DEFAULT_MODEL_NAME
