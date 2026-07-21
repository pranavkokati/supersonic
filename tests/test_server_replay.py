"""GET /api/projects/{pid}/replay — Black Box Replay served straight from
the local dashboard, exercised through FastAPI's TestClient (per this
project's own rule: never start a real `uvicorn` server in a test, use
TestClient instead)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from supersonic.loop.checkpoint import CheckpointManager
from supersonic.server import app
from supersonic.store import create_project, init_db


def test_replay_endpoint_404s_for_unknown_project(tmp_path, monkeypatch):
    monkeypatch.setattr("supersonic.store.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("supersonic.store.CONFIG_DIR", tmp_path)
    init_db()
    client = TestClient(app)
    r = client.get("/api/projects/does-not-exist/replay")
    assert r.status_code == 404


def test_replay_endpoint_serves_generated_html(tmp_path, monkeypatch):
    monkeypatch.setattr("supersonic.store.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("supersonic.store.CONFIG_DIR", tmp_path)
    init_db()
    workdir = tmp_path / "proj"
    project = create_project("Replay Test", idea="a tool", agent="claude", workdir=str(workdir))
    CheckpointManager(workdir).create(0, "setup complete")

    client = TestClient(app)
    r = client.get(f"/api/projects/{project.id}/replay")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Black Box Replay" in r.text
    assert '"turn": 0' in r.text
