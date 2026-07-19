"""Risk-Aware Model Escalation, end to end through run_factory: a turn whose
Review Risk brief flags a HIGH-risk file causes the NEXT turn (not itself)
to run the coding agent CLI and Supersonic's own critic call at a stronger
configured model. No network calls — the coding-agent CLI, LLM provider,
and Verify gate are all mocked; only the escalation wiring itself is real.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from supersonic.agents.runner import AgentResult
from supersonic.config import UserSecrets, get_settings
from supersonic.loop.orchestrator import run_factory
from supersonic.loop.planner import ProductBrand, TurnPlan
from supersonic.store import create_project, create_run, init_db
from supersonic.verify.critic import CriticVerdict
from supersonic.verify.gate import GateResult
from supersonic.verify.qa import CheckResult
from supersonic.verify.review_risk import FileRisk, ReviewBrief
from supersonic.verify.thrash import ThrashVerdict


class _FakeProvider:
    name = "anthropic"
    default_model = "claude-opus-4-8"
    fast_model = "claude-haiku-4-5-20251001"


def _fixed_gate() -> GateResult:
    return GateResult(
        passed=True, signals_ran=0, signals_passed=0,
        tests=CheckResult(name="Tests"), lint=CheckResult(name="Lint/typecheck"),
        critic=CriticVerdict(), thrash=ThrashVerdict(), summary="forced pass for escalation wiring test",
    )


def test_high_risk_turn_escalates_the_following_turn_only(tmp_path, monkeypatch):
    monkeypatch.setattr("supersonic.store.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("supersonic.store.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("supersonic.verify.receipts.CONFIG_DIR", tmp_path / "supersonic-home")
    monkeypatch.delenv("SONIC_DEMO", raising=False)
    get_settings.cache_clear()

    init_db()
    workdir = tmp_path / "proj"
    project = create_project("Real", idea="billing invoice tool", agent="claude", workdir=str(workdir))
    run = create_run(project.id)
    secrets = UserSecrets(
        anthropic_api_key="sk-test-fake",
        max_turn_budget=2,
        dle_dependency_mapper=False, dle_patch_diff_mode=False, dle_syntax_shield=False,
        dle_telemetry_gate=False, dle_dependency_trust=False, dle_secret_leak=False,
        dle_test_quality=False, dle_signed_receipts=True, dle_risk_escalation=True,
        escalation_model_claude="opus",
    )

    agent_run_calls = []

    def fake_agent_run(self, prompt, wd, on_line=None, model=None):
        agent_run_calls.append(model)
        turn_n = len(agent_run_calls)
        (Path(wd) / f"file_turn_{turn_n}.py").write_text(f"def f_{turn_n}():\n    return {turn_n}\n")
        if on_line:
            on_line(f"wrote file_turn_{turn_n}.py")
        return AgentResult(agent="claude", success=True, output="done", command="claude -p ...")

    gate_calls = []

    def fake_run_gate(workdir_arg, **kwargs):
        gate_calls.append(kwargs.get("critic_model"))
        return _fixed_gate()

    review_briefs = [
        ReviewBrief(turn=1, items=[FileRisk(path="app/auth/session.py", score=6, level="high", reasons=["no test delta"])]),
        ReviewBrief(turn=2, items=[]),
    ]

    def fake_build_review_brief(workdir_arg, turn, diff, **kwargs):
        return review_briefs[turn - 1]

    turn_plans = [
        TurnPlan(done=False, follow_up="turn 2 goal", reason="continue"),
        TurnPlan(done=True, follow_up="", reason="done after turn 2"),
    ]

    with patch("supersonic.loop.orchestrator.validate_live_run"), \
         patch("supersonic.loop.orchestrator.get_provider", return_value=_FakeProvider()), \
         patch("supersonic.agents.runner.CodingAgentRunner.run", new=fake_agent_run), \
         patch("supersonic.loop.orchestrator.generate_plan", return_value="1. Build it"), \
         patch("supersonic.loop.orchestrator.generate_brand") as gen_brand, \
         patch("supersonic.loop.orchestrator.generate_turn_plan", side_effect=turn_plans), \
         patch("supersonic.loop.orchestrator.run_gate", side_effect=fake_run_gate), \
         patch("supersonic.loop.orchestrator.build_review_brief", side_effect=fake_build_review_brief), \
         patch("supersonic.integrations.git_ops.has_remote", return_value=False):
        gen_brand.return_value = ProductBrand(product_name="Billing", tagline="tag", repo_slug="billing")

        result = run_factory(run, secrets, seed="billing invoice tool")

    assert result["turns"] == 2
    assert result["build_complete"] is True

    # Turn 1 runs at the normal (unescalated) model — nothing has shipped yet
    # to escalate from.
    assert agent_run_calls[0] is None
    assert gate_calls[0] is None

    # Turn 1 shipped with a HIGH-risk finding, so turn 2 — and only turn 2 —
    # escalates: the coding agent CLI gets --model opus, and Supersonic's own
    # critic call gets the provider's stronger default_model instead of its
    # normal fast_model.
    assert agent_run_calls[1] == "opus"
    assert gate_calls[1] == "claude-opus-4-8"


def test_escalation_disabled_never_overrides_the_model(tmp_path, monkeypatch):
    monkeypatch.setattr("supersonic.store.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("supersonic.store.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("supersonic.verify.receipts.CONFIG_DIR", tmp_path / "supersonic-home")
    monkeypatch.delenv("SONIC_DEMO", raising=False)
    get_settings.cache_clear()

    init_db()
    workdir = tmp_path / "proj"
    project = create_project("Real", idea="billing invoice tool", agent="claude", workdir=str(workdir))
    run = create_run(project.id)
    secrets = UserSecrets(
        anthropic_api_key="sk-test-fake",
        max_turn_budget=2,
        dle_dependency_mapper=False, dle_patch_diff_mode=False, dle_syntax_shield=False,
        dle_telemetry_gate=False, dle_dependency_trust=False, dle_secret_leak=False,
        dle_test_quality=False, dle_signed_receipts=True,
        dle_risk_escalation=False,  # <-- the setting under test
        escalation_model_claude="opus",
    )

    agent_run_calls = []

    def fake_agent_run(self, prompt, wd, on_line=None, model=None):
        agent_run_calls.append(model)
        turn_n = len(agent_run_calls)
        (Path(wd) / f"file_turn_{turn_n}.py").write_text(f"def f_{turn_n}():\n    return {turn_n}\n")
        return AgentResult(agent="claude", success=True, output="done", command="claude -p ...")

    def fake_run_gate(workdir_arg, **kwargs):
        return _fixed_gate()

    def fake_build_review_brief(workdir_arg, turn, diff, **kwargs):
        return ReviewBrief(turn=turn, items=[FileRisk(path="app/auth/session.py", score=6, level="high")])

    turn_plans = [
        TurnPlan(done=False, follow_up="turn 2 goal", reason="continue"),
        TurnPlan(done=True, follow_up="", reason="done after turn 2"),
    ]

    with patch("supersonic.loop.orchestrator.validate_live_run"), \
         patch("supersonic.loop.orchestrator.get_provider", return_value=_FakeProvider()), \
         patch("supersonic.agents.runner.CodingAgentRunner.run", new=fake_agent_run), \
         patch("supersonic.loop.orchestrator.generate_plan", return_value="1. Build it"), \
         patch("supersonic.loop.orchestrator.generate_brand") as gen_brand, \
         patch("supersonic.loop.orchestrator.generate_turn_plan", side_effect=turn_plans), \
         patch("supersonic.loop.orchestrator.run_gate", side_effect=fake_run_gate), \
         patch("supersonic.loop.orchestrator.build_review_brief", side_effect=fake_build_review_brief), \
         patch("supersonic.integrations.git_ops.has_remote", return_value=False):
        gen_brand.return_value = ProductBrand(product_name="Billing", tagline="tag", repo_slug="billing")

        run_factory(run, secrets, seed="billing invoice tool")

    # Even though every turn shipped a HIGH-risk finding, escalation is
    # disabled, so both turns must run at the normal, unescalated model.
    assert agent_run_calls == [None, None]
