"""Scaffold smoke tests for GridSafe."""

from pathlib import Path

from fastapi.testclient import TestClient

from server.app import app, load_models_config

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_models_config_has_distinct_gemini4_models() -> None:
    cfg = load_models_config()
    assert cfg["reader"]["model"] == "gemini-4-neon"
    assert cfg["verifier"]["model"] == "gemini-decision-1"
    assert cfg["designer"]["model"] == "gemini-4-hydrogen"
    assert cfg["hardener"]["model"] == "gemini-4-carbon"
    gemini4_models = {
        cfg["reader"]["model"],
        cfg["designer"]["model"],
        cfg["hardener"]["model"],
    }
    assert len(gemini4_models) >= 2


def test_health_endpoint() -> None:
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "models" in body
    assert "git" in body


def test_sse_ping_streams_events() -> None:
    client = TestClient(app)
    with client.stream("GET", "/api/stream/ping") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        payload = "".join(resp.iter_text())
    assert "event: step" in payload
    assert "event: done" in payload


def test_gitignore_ignores_env() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore.splitlines()


def test_serves_built_frontend() -> None:
    dist_index = REPO_ROOT / "frontend" / "dist" / "index.html"
    if not dist_index.exists():
        return
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "GridSafe" in resp.text

