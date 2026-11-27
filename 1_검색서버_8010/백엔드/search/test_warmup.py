import threading
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from search import app, engine


def test_warmup_endpoint_local_only_and_single_worker(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def warm():
        calls.append(1)
        entered.set()
        assert release.wait(5)

    monkeypatch.setenv("SEARCH_PASSWORD", "test")
    monkeypatch.setattr(app, "warm_duplicate_snapshot", warm)
    try:
        local = TestClient(app.app, client=("127.0.0.1", 1234))
        first = local.post("/api/warmup")
        assert first.status_code == 202 and first.json()["started"]
        assert entered.wait(5)
        second = local.post("/api/warmup")
        assert second.status_code == 202 and not second.json()["started"]
        assert TestClient(app.app, client=("::1", 1234)).post("/api/warmup").status_code == 202
        assert TestClient(app.app, client=("192.0.2.1", 1234)).post("/api/warmup").status_code == 404
        assert local.post("/api/warmup", headers={"X-Forwarded-For": "192.0.2.1"}).status_code == 404
        assert local.post("/api/warmup", headers={"Origin": "https://example.com"}).status_code == 404
        assert calls == [1]
    finally:
        release.set()


def test_warmup_uses_read_only_snapshot_cache(monkeypatch):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.execute.return_value.fetchone.return_value = {"transaction_read_only": "on"}
    connect = MagicMock(return_value=conn)
    snapshot = MagicMock()
    monkeypatch.setattr(engine.psycopg, "connect", connect)
    monkeypatch.setattr(engine, "connection_options", lambda _: {})
    monkeypatch.setattr(engine, "article_data_snapshot", lambda _: (("articles",), None))
    monkeypatch.setattr(engine, "snapshot_duplicate_ids", snapshot)
    engine.warm_duplicate_snapshot()
    assert conn.read_only is True
    assert connect.call_args.kwargs["options"] == "-c default_transaction_read_only=on -c statement_timeout=10000"
    assert "REPEATABLE READ READ ONLY" in conn.execute.call_args_list[0].args[0]
    snapshot.assert_called_once_with(conn, ("articles",))
