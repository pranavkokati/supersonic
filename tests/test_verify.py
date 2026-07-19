"""Verify gate signals, templates, webhooks, queue/portfolio, and API endpoints."""

from __future__ import annotations

from supersonic.store import create_project, enqueue_project, init_db, list_queue, portfolio_summary
from supersonic.templates import apply_template, list_templates
from supersonic.verify.gate import run_gate
from supersonic.verify.qa import run_tests
from supersonic.verify.thrash import detect
from supersonic.webhooks import sign_payload


def test_list_templates():
    t = list_templates()
    assert any(x["id"] == "cli" for x in t)


def test_apply_cli_template(tmp_path):
    wd = tmp_path / "p"
    hint = apply_template(wd, "cli", "sync tool")
    assert "CLI" in hint
    assert (wd / "app" / "main.py").exists()


def test_run_tests_not_ran_when_no_suite_detected(tmp_path):
    result = run_tests(tmp_path)
    assert result.ran is False


def test_thrash_detects_high_similarity():
    diff_a = "diff --git a/x.py b/x.py\n+print('hello')\n"
    verdict = detect(diff_a, [diff_a, diff_a], threshold=0.85)
    assert verdict.ran is True
    assert verdict.thrashing is True


def test_thrash_not_flagged_for_distinct_diffs():
    diff_a = "diff --git a/x.py b/x.py\n+print('hello')\n"
    diff_b = "diff --git a/y.py b/y.py\n+def totally_different(): return 42\n"
    verdict = detect(diff_a, [diff_b], threshold=0.85)
    assert verdict.thrashing is False


def test_run_gate_passes_by_default_with_no_signals(tmp_path):
    gate = run_gate(
        tmp_path, provider=None, goal="do something", diff="", invariants=[], recent_diffs=[], min_signals_pass=3
    )
    assert gate.signals_ran == 0
    assert gate.passed is True


def test_run_gate_fails_on_a_real_failing_test_suite(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_broken.py").write_text("def test_x():\n    assert 1 == 2\n")

    gate = run_gate(
        tmp_path, provider=None, goal="do something", diff="", invariants=[], recent_diffs=[], min_signals_pass=3
    )
    assert gate.tests.ran is True
    assert gate.tests.passed is False
    assert gate.passed is False


def test_webhook_sign():
    sig = sign_payload("secret", b'{"a":1}')
    assert len(sig) == 64


def test_queue_and_portfolio(tmp_path, monkeypatch):
    monkeypatch.setattr("supersonic.store.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("supersonic.store.CONFIG_DIR", tmp_path)
    init_db()
    p = create_project("Q", template_id="cli")
    qid = enqueue_project(p.id, "seed idea")
    assert qid
    assert len(list_queue()) == 1
    pf = portfolio_summary()
    assert pf and pf[0]["name"] == "Q"


def test_templates_api():
    from fastapi.testclient import TestClient

    from supersonic.server import app

    client = TestClient(app)
    r = client.get("/api/templates")
    assert r.status_code == 200
    assert len(r.json()) >= 3


def test_portfolio_api():
    from fastapi.testclient import TestClient

    from supersonic.server import app

    client = TestClient(app)
    assert client.get("/api/portfolio").status_code == 200


def test_health_api_reports_new_architecture_features():
    from fastapi.testclient import TestClient

    from supersonic.server import app

    client = TestClient(app)
    body = client.get("/api/health").json()
    assert "checkpoint_verify_rollback" in body["features"]
    assert "continuity_graph" in body["features"]
    assert "bandit_agent_racing" in body["features"]
