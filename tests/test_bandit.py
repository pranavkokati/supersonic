"""Bandit-Gated Agent Racing — task classification and Thompson-sampling convergence."""

from __future__ import annotations

from supersonic.loop.bandit import AgentBandit, classify_task


def test_classify_task_frontend():
    assert classify_task("Add a new button component to the login page") == "frontend"


def test_classify_task_backend():
    assert classify_task("Add a new API endpoint with a database migration") == "backend"


def test_classify_task_bugfix():
    assert classify_task("Fix the crash on startup, regression from last turn") == "bugfix"


def test_classify_task_falls_back_to_general():
    assert classify_task("do the thing") == "general"


def test_should_race_when_no_trials_yet(tmp_path):
    bandit = AgentBandit(tmp_path, ["claude", "codex"], seed=0)
    # With zero observations for either arm, the bandit should always explore.
    assert bandit.should_race("backend") is True


def test_record_result_updates_posterior_and_persists(tmp_path):
    bandit = AgentBandit(tmp_path, ["claude", "codex"], seed=0)
    for _ in range(20):
        bandit.record_result("backend", winner="claude", participants=["claude", "codex"])

    rates = bandit.win_rates()
    assert rates["backend"]["claude"] > rates["backend"]["codex"]
    assert rates["backend"]["claude"] > 0.8

    # Persisted state survives a fresh instance reading the same workdir.
    reloaded = AgentBandit(tmp_path, ["claude", "codex"], seed=1)
    reloaded_rates = reloaded.win_rates()
    assert reloaded_rates["backend"]["claude"] == rates["backend"]["claude"]


def test_should_race_stops_once_one_agent_dominates(tmp_path):
    bandit = AgentBandit(tmp_path, ["claude", "codex"], seed=0)
    for _ in range(40):
        bandit.record_result("backend", winner="claude", participants=["claude", "codex"])

    # After 40 lopsided observations, should_race should resolve to False far more
    # often than True across repeated samples (Thompson sampling is stochastic, so
    # we check the aggregate tendency rather than asserting a single sample).
    races = sum(1 for _ in range(30) if bandit.should_race("backend"))
    assert races < 10


def test_best_agent_picks_the_winner(tmp_path):
    bandit = AgentBandit(tmp_path, ["claude", "codex"], seed=0)
    for _ in range(10):
        bandit.record_result("frontend", winner="codex", participants=["claude", "codex"])
    assert bandit.best_agent("frontend") == "codex"
