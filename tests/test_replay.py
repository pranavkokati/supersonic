"""Black Box Replay (loop/replay.py).

Built against real temp git repos (CheckpointManager), a real Continuity
Ledger, and real Ed25519-signed receipts — same pattern as
test_checkpoint_rollback.py and test_receipts.py — because the entire point
of this feature is faithfully reconstructing what those real subsystems
already recorded, not mocking around them.

Scope under test: the diff-between-checkpoints reconstruction excludes
exactly the right bookkeeping paths and matches the receipt's diff_sha256
for the common case; the `diff_hash_reconstructable` self-check correctly
disables the client-side recompute claim when it can't be trusted (an
intervening rolled-back turn, or a diff too large to embed); rolled-back
turns show no diff and no receipt; and the rendered HTML is well-formed and
embeds the data faithfully.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from supersonic.loop import replay as rp
from supersonic.loop.checkpoint import CheckpointManager
from supersonic.memory.ledger import ContinuityLedger
from supersonic.verify import receipts as rc
from supersonic.verify.critic import CriticVerdict
from supersonic.verify.dependency_trust import DependencyTrustVerdict
from supersonic.verify.gate import GateResult
from supersonic.verify.qa import CheckResult
from supersonic.verify.secret_leak import SecretLeakVerdict
from supersonic.verify.telemetry_gate import TelemetryVerdict
from supersonic.verify.test_quality import TestQualityVerdict
from supersonic.verify.thrash import ThrashVerdict


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """The project's own git repo lives at tmp_path/proj/. The signing key
    directory is a SIBLING (tmp_path/supersonic-home/), never nested inside
    it -- exactly like the real ~/.supersonic is never inside a project's
    own repo. Nesting them (a mistake made and caught while first building
    this feature) makes the signing key file itself show up in every
    reconstructed diff, which is a test-harness artifact, not something
    that can happen in real operation."""
    monkeypatch.setattr(rc, "CONFIG_DIR", tmp_path / "supersonic-home")
    wd = tmp_path / "proj"
    wd.mkdir()
    return wd


def _fake_gate(passed: bool = True) -> GateResult:
    return GateResult(
        passed=passed, signals_ran=2, signals_passed=2 if passed else 0,
        tests=CheckResult(name="Tests", ran=True, passed=passed, command="pytest -q"),
        lint=CheckResult(name="Lint/typecheck", ran=True, passed=passed, command="ruff check ."),
        critic=CriticVerdict(ran=True, satisfied=passed, reasoning="ok"), thrash=ThrashVerdict(),
        summary="2/2 verification signals passed (needed 2).", telemetry=TelemetryVerdict(),
        dependency_trust=DependencyTrustVerdict(), secret_leak=SecretLeakVerdict(), test_quality=TestQualityVerdict(),
    )


def _ship_turn(mgr, ledger, wd, turn, note, filetext, prev_checkpoint):
    """Mirrors orchestrator.py's REAL ordering exactly: record the ledger
    decision and write the signed receipt BEFORE creating the checkpoint,
    so both land in that turn's own commit -- see run_factory()'s
    `if gate.passed:` branch. Getting this order backwards (checkpoint
    first) was a real mistake made while building this test, and it
    silently shifts a turn's own bookkeeping into the NEXT turn's diff."""
    (wd / "app.py").write_text(filetext)
    diff = mgr.diff_since(prev_checkpoint)
    ledger.record_decision(turn, f"Turn {turn}", f"body {turn}", tags=["claude"])
    receipt = rc.build_receipt(
        turn=turn, goal=f"goal {turn}", prompt=f"prompt {turn}", diff=diff,
        coding_agent="claude", provider_name="anthropic", model="m", temperature=0.0, gate=_fake_gate(True),
    )
    rc.write_receipt(wd, receipt)
    checkpoint = mgr.create(turn, note)
    return checkpoint, diff


def test_empty_project_has_no_turns(workdir):
    data = rp.build_replay_data(workdir)
    assert data["turns"] == []
    assert data["linked_repos"] == []


def test_turn_zero_is_the_setup_checkpoint(workdir):
    mgr = CheckpointManager(workdir)
    mgr.create(0, "setup complete")
    data = rp.build_replay_data(workdir)
    assert len(data["turns"]) == 1
    assert data["turns"][0]["turn"] == 0
    assert data["turns"][0]["shipped"] is True


def test_shipped_turn_diff_matches_the_original_diff_since_output(workdir):
    mgr = CheckpointManager(workdir)
    ledger = ContinuityLedger(workdir)
    cp0 = mgr.create(0, "setup complete")
    _cp1, diff1 = _ship_turn(mgr, ledger, workdir, 1, "turn 1", "def f():\n    return 1\n", cp0)

    data = rp.build_replay_data(workdir)
    turn1 = next(t for t in data["turns"] if t["turn"] == 1)
    assert turn1["diff"] == diff1


def test_diff_hash_reconstructable_and_matches_receipt_for_clean_turn(workdir):
    """The core claim: for a turn with no intervening failure, the
    reconstructed diff's SHA-256 equals the receipt's own diff_sha256 --
    i.e. exactly what the client-side 'recompute in your browser' button
    would independently confirm."""
    mgr = CheckpointManager(workdir)
    ledger = ContinuityLedger(workdir)
    cp0 = mgr.create(0, "setup complete")
    cp1, _ = _ship_turn(mgr, ledger, workdir, 1, "turn 1", "def f():\n    return 1\n", cp0)
    _cp2, _ = _ship_turn(mgr, ledger, workdir, 2, "turn 2", "def f():\n    return 2\n", cp1)

    data = rp.build_replay_data(workdir)
    turn2 = next(t for t in data["turns"] if t["turn"] == 2)
    assert turn2["diff_hash_reconstructable"] is True
    assert hashlib.sha256(turn2["diff"].encode("utf-8")).hexdigest() == turn2["receipt"]["diff_sha256"]
    assert turn2["receipt"]["verified"] is True


def test_diff_hash_not_reconstructable_when_a_turn_failed_in_between(workdir):
    """Honest self-check in action: a rolled-back turn's preserved ledger
    entry rides along uncommitted into the NEXT shipped turn's commit, so
    that next turn's raw commit-to-commit diff (even after excluding
    .continuity/.supersonic) is no longer byte-identical to what was
    signed. build_replay_data must detect this itself and disable the
    recompute claim, not report a false match."""
    mgr = CheckpointManager(workdir)
    ledger = ContinuityLedger(workdir)
    cp0 = mgr.create(0, "setup complete")
    cp1, _ = _ship_turn(mgr, ledger, workdir, 1, "turn 1", "def f():\n    return 1\n", cp0)

    ledger.record_failure(2, "Turn 2 failed", "Dependency Trust Gate failed.", tags=["claude", "dependency_trust"])
    # no checkpoint for turn 2 -- it rolled back

    _cp3, _ = _ship_turn(mgr, ledger, workdir, 3, "turn 3", "def f():\n    return 3\n", cp1)

    data = rp.build_replay_data(workdir)
    turn3 = next(t for t in data["turns"] if t["turn"] == 3)
    assert turn3["diff_hash_reconstructable"] is False
    # The receipt itself is still perfectly valid -- only independent
    # client-side re-derivation of the diff hash is what's disabled.
    assert turn3["receipt"]["verified"] is True


def test_rolled_back_turn_has_no_diff_and_no_receipt(workdir):
    mgr = CheckpointManager(workdir)
    ledger = ContinuityLedger(workdir)
    mgr.create(0, "setup complete")
    ledger.record_failure(1, "Turn 1 failed: bad dep", "Dependency Trust Gate failed.", tags=["claude", "dependency_trust"])

    data = rp.build_replay_data(workdir)
    turn1 = next(t for t in data["turns"] if t["turn"] == 1)
    assert turn1["shipped"] is False
    assert turn1["diff"] == ""
    assert turn1["receipt"] is None
    assert turn1["diff_hash_reconstructable"] is False
    assert len(turn1["failures"]) == 1
    assert turn1["failures"][0]["title"] == "Turn 1 failed: bad dep"


def test_tampered_receipt_is_reported_as_unverified(workdir):
    mgr = CheckpointManager(workdir)
    ledger = ContinuityLedger(workdir)
    cp0 = mgr.create(0, "setup complete")
    _ship_turn(mgr, ledger, workdir, 1, "turn 1", "def f():\n    return 1\n", cp0)

    receipt_path = workdir / ".supersonic" / "receipts" / "turn-1.json"
    raw = json.loads(receipt_path.read_text())
    raw["goal"] = "a completely different goal, edited after the fact"
    receipt_path.write_text(json.dumps(raw))

    data = rp.build_replay_data(workdir)
    turn1 = next(t for t in data["turns"] if t["turn"] == 1)
    assert turn1["receipt"]["verified"] is False
    assert "signature invalid" in turn1["receipt"]["verify_reason"]


def test_rules_learned_are_attributed_to_the_turn_that_learned_them(workdir):
    from supersonic.memory.rules_engine import Rule, RulesStore

    mgr = CheckpointManager(workdir)
    ledger = ContinuityLedger(workdir)
    mgr.create(0, "setup complete")
    ledger.record_failure(1, "Turn 1 failed", "tests failed", tags=["claude", "tests"])
    ledger.record_failure(2, "Turn 2 failed", "tests failed again", tags=["claude", "tests"])
    RulesStore(workdir).add(Rule(
        id="tests-2", category="tests", rule_text="Always run pytest before shipping.",
        source_turn=2, source_failure="Turn 2 failed", repeats_observed=2,
    ))

    data = rp.build_replay_data(workdir)
    turn2 = next(t for t in data["turns"] if t["turn"] == 2)
    assert len(turn2["rules_learned"]) == 1
    assert turn2["rules_learned"][0]["rule_text"] == "Always run pytest before shipping."
    assert data["all_rules"][0]["category"] == "tests"


def test_diff_truncation_disables_reconstructable_flag(workdir, monkeypatch):
    monkeypatch.setattr(rp, "MAX_EMBEDDED_DIFF_CHARS", 10)
    mgr = CheckpointManager(workdir)
    ledger = ContinuityLedger(workdir)
    cp0 = mgr.create(0, "setup complete")
    _ship_turn(mgr, ledger, workdir, 1, "turn 1", "def f():\n    return 1\nx = 'padding to exceed the tiny cap'\n", cp0)

    data = rp.build_replay_data(workdir)
    turn1 = next(t for t in data["turns"] if t["turn"] == 1)
    assert turn1["diff_truncated"] is True
    assert turn1["diff_hash_reconstructable"] is False
    assert len(turn1["diff"]) == 10


def test_linked_repos_are_surfaced(workdir):
    from supersonic.loop.multi_repo import LinkedRepo, save_linked_repos

    mgr = CheckpointManager(workdir)
    mgr.create(0, "setup complete")
    save_linked_repos(workdir, [LinkedRepo(path="/tmp/frontend", label="React frontend")])

    data = rp.build_replay_data(workdir)
    assert data["linked_repos"] == [{"path": "/tmp/frontend", "label": "React frontend"}]


def test_render_replay_html_is_well_formed_and_embeds_data(workdir):
    mgr = CheckpointManager(workdir)
    ledger = ContinuityLedger(workdir)
    cp0 = mgr.create(0, "setup complete")
    _ship_turn(mgr, ledger, workdir, 1, "turn 1", "def f():\n    return 1\n", cp0)

    html = rp.build_replay_html(workdir)
    assert html.startswith("<!DOCTYPE html>")
    assert html.count("<script") == html.count("</script>") + html.count("<\\/script>")
    assert '"turn": 1' in html
    assert "Black Box Replay" in html


def test_render_replay_html_escapes_literal_closing_script_tag(workdir):
    """A diff or ledger entry containing a literal `</script>` substring
    must not be able to break out of the embedded JSON <script> block."""
    mgr = CheckpointManager(workdir)
    ledger = ContinuityLedger(workdir)
    mgr.create(0, "setup complete")
    ledger.record_decision(1, "</script><script>alert(1)</script>", "malicious-looking title", tags=["claude"])
    mgr.create(1, "turn 1")

    html = rp.build_replay_html(workdir)
    # The dangerous literal sequence must never appear unescaped.
    assert "</script><script>alert(1)</script>" not in html
    assert "<\\/script>" in html


def test_build_replay_html_one_shot_matches_two_step(workdir):
    """build_replay_html() is exactly render_replay_html(build_replay_data())
    -- same turn count and structure, modulo the generated_at timestamp,
    which legitimately differs because each call re-stamps "now"."""
    mgr = CheckpointManager(workdir)
    mgr.create(0, "setup complete")

    data = rp.build_replay_data(workdir)
    two_step_html = rp.render_replay_html(data)
    one_shot_html = rp.build_replay_html(workdir)

    def strip_ts(html: str) -> str:
        return re.sub(r'"generated_at":\s*"[^"]*"', '"generated_at":"TIMESTAMP"', html)

    assert strip_ts(two_step_html) == strip_ts(one_shot_html)
