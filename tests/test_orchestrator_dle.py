"""DLE wiring — orchestrator.run_factory still completes a full demo run with
the five new stages wired in, and non-demo turns route through the new DLE
stages (dependency mapper, patch-diff mode, syntax shield, telemetry gate)
without breaking the Checkpoint/Verify/Rollback contract. No network calls:
the coding-agent CLI and LLM provider are mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from supersonic.agents.runner import AgentResult
from supersonic.config import UserSecrets, get_settings
from supersonic.loop.orchestrator import run_factory
from supersonic.store import create_project, create_run, init_db


@pytest.fixture
def demo_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("supersonic.store.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("supersonic.store.CONFIG_DIR", tmp_path)
    monkeypatch.setenv("SONIC_DEMO", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_demo_run_completes_with_dle_stages_wired_in(demo_settings, tmp_path):
    init_db()
    project = create_project("Demo", idea="a tiny CLI tool", agent="claude", workdir=str(tmp_path / "proj"))
    run = create_run(project.id)
    secrets = UserSecrets(max_turn_budget=5)

    result = run_factory(run, secrets, seed="a tiny CLI tool")

    assert result["build_complete"] is True
    assert result["turns"] >= 1
    # Demo mode never engages the DLE stages that need real file edits — the
    # cache file for the dependency mapper should not have been created.
    assert not (Path(project.workdir) / ".dle" / "target_graph.json").exists()


def test_non_demo_turn_uses_dependency_mapper_and_syntax_shield(tmp_path, monkeypatch):
    """Exercise a single non-demo turn through run_factory with the coding
    agent, provider, git shipping, and the four-signal gate all mocked out
    (gate outcome is covered exhaustively by tests/test_verify.py already —
    here we only need it deterministic), to prove the DLE stage-1/3 wiring
    actually executes as part of a real turn, not just that the modules
    import cleanly in isolation."""
    monkeypatch.setattr("supersonic.store.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("supersonic.store.CONFIG_DIR", tmp_path)
    # Signed Turn Receipts (on by default) would otherwise generate/reuse a
    # real signing key under the actual ~/.supersonic/keys/ on whatever
    # machine runs this suite — keep it fully sandboxed in tmp_path instead.
    monkeypatch.setattr("supersonic.verify.receipts.CONFIG_DIR", tmp_path / "supersonic-home")
    monkeypatch.delenv("SONIC_DEMO", raising=False)
    get_settings.cache_clear()

    init_db()
    workdir = tmp_path / "proj"
    project = create_project("Real", idea="billing invoice tool", agent="claude", workdir=str(workdir))
    run = create_run(project.id)
    secrets = UserSecrets(
        anthropic_api_key="sk-test-fake",
        max_turn_budget=1,
        dle_dependency_mapper=True,
        dle_syntax_shield=True,
        dle_patch_diff_mode=False,
        dle_telemetry_gate=False,  # no package.json in this project — would auto-skip anyway
    )

    dependency_mapper_calls = []

    def fake_agent_run(self, prompt, wd, on_line=None, model=None):
        (Path(wd) / "invoice.py").write_text("def charge():\n    return 1\n")
        if on_line:
            on_line("wrote invoice.py")
        return AgentResult(agent="claude", success=True, output="done", command="claude -p ...")

    real_build_target_graph = _import_build_target_graph()

    def spying_build_target_graph(wd, goal, **kw):
        graph = real_build_target_graph(wd, goal, **kw)
        dependency_mapper_calls.append(goal)
        return graph

    from supersonic.loop.planner import ProductBrand, TurnPlan
    from supersonic.verify.critic import CriticVerdict
    from supersonic.verify.gate import GateResult
    from supersonic.verify.qa import CheckResult
    from supersonic.verify.thrash import ThrashVerdict

    fixed_passing_gate = GateResult(
        passed=True, signals_ran=0, signals_passed=0,
        tests=CheckResult(name="Tests"), lint=CheckResult(name="Lint/typecheck"),
        critic=CriticVerdict(), thrash=ThrashVerdict(), summary="forced pass for wiring test",
    )

    with patch("supersonic.loop.orchestrator.validate_live_run"), \
         patch("supersonic.loop.orchestrator.get_provider", return_value=None), \
         patch("supersonic.agents.runner.CodingAgentRunner.run", new=fake_agent_run), \
         patch("supersonic.loop.orchestrator.generate_plan", return_value="1. Build it"), \
         patch("supersonic.loop.orchestrator.generate_brand") as gen_brand, \
         patch("supersonic.loop.orchestrator.generate_turn_plan") as gen_turn_plan, \
         patch("supersonic.loop.orchestrator.build_target_graph", side_effect=spying_build_target_graph), \
         patch("supersonic.loop.orchestrator.run_gate", return_value=fixed_passing_gate), \
         patch("supersonic.integrations.git_ops.has_remote", return_value=False):
        gen_brand.return_value = ProductBrand(product_name="Billing", tagline="tag", repo_slug="billing")
        gen_turn_plan.return_value = TurnPlan(done=True, follow_up="", reason="one turn is enough")

        result = run_factory(run, secrets, seed="billing invoice tool")

    assert result["turns"] == 1
    assert result["build_complete"] is True
    # Dependency mapper (DLE stage 1) actually ran for this real turn, scoped to the turn's goal.
    assert dependency_mapper_calls == ["billing invoice tool"]
    # The gate passed (forced), so the checkpoint commit kept the DLE cache on disk.
    assert (workdir / ".dle" / "target_graph.json").exists()
    # Signed Turn Receipts: a real receipt was written for the shipped turn,
    # landed in the same working tree as the checkpoint, and verifies clean.
    from supersonic.verify.receipts import verify_all_receipts

    receipt_results = verify_all_receipts(workdir)
    assert len(receipt_results) == 1
    assert receipt_results[0].turn == 1
    assert receipt_results[0].ok is True


def _import_build_target_graph():
    from supersonic.loop.dependency_mapper import build_target_graph
    return build_target_graph


def test_rules_engine_rule_survives_the_rollback_that_learned_it(tmp_path, monkeypatch):
    """Regression test for a real bug this session found and fixed in
    rollback.py: observe_failure() (memory/rules_engine.py) writes a
    brand-new .supersonic/rules.json on the SAME failed turn that
    immediately rolls back a few lines later in orchestrator.py's failure
    branch. Before the fix, `git clean -fd -e .continuity` didn't exclude
    `.supersonic`, so the just-learned rule was silently deleted by its own
    turn's rollback — the feature could never actually persist a rule in
    real operation. This drives two real failed-and-rolled-back turns
    (same failure category, same as the ledger would tag them) through the
    actual orchestrator loop, not just rollback_to() in isolation (see
    test_checkpoint_rollback.py for that unit-level check), and asserts the
    rule synthesized on turn 2 is still on disk after that turn's rollback."""
    monkeypatch.setattr("supersonic.store.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("supersonic.store.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("supersonic.verify.receipts.CONFIG_DIR", tmp_path / "supersonic-home")
    monkeypatch.delenv("SONIC_DEMO", raising=False)
    get_settings.cache_clear()

    init_db()
    workdir = tmp_path / "proj"
    project = create_project("Real", idea="a tool", agent="claude", workdir=str(workdir))
    run = create_run(project.id)
    secrets = UserSecrets(
        anthropic_api_key="sk-test-fake", max_turn_budget=2,
        dle_dependency_mapper=False, dle_syntax_shield=False, dle_patch_diff_mode=False,
        dle_telemetry_gate=False, dle_dependency_trust=False, dle_secret_leak=False,
        dle_test_quality=False, dle_review_risk=False, dle_signed_receipts=False,
        dle_live_syntax_watch=False, dle_pty_supervision=False,
        dle_rules_evolution=True, rules_evolution_min_repeats=2,
    )

    def fake_agent_run(self, prompt, wd, on_line=None, model=None):
        (Path(wd) / "x.py").write_text("x = 1\n")
        return AgentResult(agent="claude", success=True, output="wrote x.py", command="claude -p ...")

    from supersonic.loop.planner import ProductBrand, TurnPlan
    from supersonic.verify.critic import CriticVerdict
    from supersonic.verify.gate import GateResult
    from supersonic.verify.qa import CheckResult
    from supersonic.verify.thrash import ThrashVerdict

    failing_gate = GateResult(
        passed=False, signals_ran=1, signals_passed=0,
        tests=CheckResult(name="Tests", ran=True, passed=False, command="pytest -q", output="AssertionError"),
        lint=CheckResult(name="Lint/typecheck"), critic=CriticVerdict(), thrash=ThrashVerdict(),
        summary="0/1 verification signals passed (needed 1).",
    )

    fake_provider = MagicMock()
    fake_provider.name = "anthropic"
    fake_provider.default_model = "claude-opus-4-8"
    fake_provider.fast_model = "claude-haiku-4-5"
    fake_provider.complete_json.return_value = {"rule": "Run the full test suite locally before shipping."}

    with patch("supersonic.loop.orchestrator.validate_live_run"), \
         patch("supersonic.loop.orchestrator.get_provider", return_value=fake_provider), \
         patch("supersonic.agents.runner.CodingAgentRunner.run", new=fake_agent_run), \
         patch("supersonic.loop.orchestrator.generate_plan", return_value="1. Build it"), \
         patch("supersonic.loop.orchestrator.generate_brand") as gen_brand, \
         patch("supersonic.loop.orchestrator.generate_turn_plan") as gen_turn_plan, \
         patch("supersonic.loop.orchestrator.run_gate", return_value=failing_gate), \
         patch("supersonic.integrations.git_ops.has_remote", return_value=False):
        gen_brand.return_value = ProductBrand(product_name="X", tagline="t", repo_slug="x")
        gen_turn_plan.return_value = TurnPlan(done=False, follow_up="keep going", reason="not done")

        result = run_factory(run, secrets, seed="a tool")

    assert result["turns"] == 2
    assert result["build_complete"] is False  # every turn failed Verify; capped by max_turn_budget

    rules_path = workdir / ".supersonic" / "rules.json"
    assert rules_path.exists(), "the rule learned on turn 2 must survive that turn's own rollback"
    assert "Run the full test suite locally before shipping." in rules_path.read_text()
