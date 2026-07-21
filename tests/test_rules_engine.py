"""Self-Evolving Rules Engine (memory/rules_engine.py).

Scope under test: classify_gate_failure()'s category mapping, RulesStore's
persistence + idempotent mirroring into pre-existing convention files only
(never created from scratch), and observe_failure()'s gating logic (repeat
threshold, one-rule-per-category, no-provider no-op, and that a
rule-synthesis exception never propagates).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

from supersonic.memory.ledger import ContinuityLedger
from supersonic.memory.rules_engine import (
    Rule,
    RulesStore,
    _MIRROR_END,
    _MIRROR_START,
    active_rules_block,
    classify_gate_failure,
    observe_failure,
)


@dataclass
class _Verdict:
    ran: bool = False
    ok: bool = True
    passed: bool = True
    satisfied: bool = True
    thrashing: bool = False


@dataclass
class _FakeGate:
    summary: str = ""
    dependency_trust: _Verdict = field(default_factory=_Verdict)
    secret_leak: _Verdict = field(default_factory=_Verdict)
    tests: _Verdict = field(default_factory=_Verdict)
    lint: _Verdict = field(default_factory=_Verdict)
    test_quality: _Verdict = field(default_factory=_Verdict)
    critic: _Verdict = field(default_factory=_Verdict)
    thrash: _Verdict = field(default_factory=_Verdict)


# ---- classify_gate_failure --------------------------------------------------


def test_classify_unknown_when_nothing_failed():
    assert classify_gate_failure(_FakeGate()) == "unknown"


def test_classify_dependency_trust_takes_priority():
    gate = _FakeGate(
        dependency_trust=_Verdict(ran=True, ok=False),
        tests=_Verdict(ran=True, passed=False),  # would also fail, but dep trust wins
    )
    assert classify_gate_failure(gate) == "dependency_trust"


def test_classify_secret_leak():
    gate = _FakeGate(secret_leak=_Verdict(ran=True, ok=False))
    assert classify_gate_failure(gate) == "secret_leak"


def test_classify_syntax_shield_from_summary_text():
    gate = _FakeGate(summary="Syntax Shield failed after one auto-corrective re-prompt: foo.py")
    assert classify_gate_failure(gate) == "syntax_shield"


def test_classify_tests():
    gate = _FakeGate(tests=_Verdict(ran=True, passed=False))
    assert classify_gate_failure(gate) == "tests"


def test_classify_lint():
    gate = _FakeGate(lint=_Verdict(ran=True, passed=False))
    assert classify_gate_failure(gate) == "lint"


def test_classify_test_quality():
    gate = _FakeGate(test_quality=_Verdict(ran=True, passed=False))
    assert classify_gate_failure(gate) == "test_quality"


def test_classify_critic():
    gate = _FakeGate(critic=_Verdict(ran=True, satisfied=False))
    assert classify_gate_failure(gate) == "critic"


def test_classify_thrash():
    gate = _FakeGate(thrash=_Verdict(ran=True, thrashing=True))
    assert classify_gate_failure(gate) == "thrash"


# ---- RulesStore --------------------------------------------------------------


def _rule(category="tests", turn=3, repeats=2) -> Rule:
    return Rule(
        id=f"{category}-{turn}",
        category=category,
        rule_text=f"Always double-check {category}.",
        source_turn=turn,
        source_failure="Turn failed",
        repeats_observed=repeats,
    )


def test_rules_store_add_and_read_back(tmp_path: Path):
    store = RulesStore(tmp_path)
    assert store.all() == []
    assert store.has_rule_for_category("tests") is False

    store.add(_rule("tests"))

    reloaded = RulesStore(tmp_path)
    assert len(reloaded.all()) == 1
    assert reloaded.has_rule_for_category("tests") is True
    assert (tmp_path / ".supersonic" / "rules.json").exists()
    assert (tmp_path / ".supersonic" / "rules.md").exists()


def test_active_rules_block_empty_when_no_rules(tmp_path: Path):
    assert active_rules_block(tmp_path) == ""


def test_active_rules_block_lists_rule_text(tmp_path: Path):
    store = RulesStore(tmp_path)
    store.add(_rule("tests"))
    block = active_rules_block(tmp_path)
    assert "Rules learned from this project" in block
    assert "Always double-check tests." in block


def test_mirror_skips_convention_files_that_dont_exist(tmp_path: Path):
    store = RulesStore(tmp_path)
    store.add(_rule("tests"))
    assert not (tmp_path / ".cursorrules").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_mirror_updates_existing_cursorrules_preserving_user_content(tmp_path: Path):
    cursorrules = tmp_path / ".cursorrules"
    cursorrules.write_text("# My own hand-written rules\nAlways use tabs.\n", encoding="utf-8")

    store = RulesStore(tmp_path)
    store.add(_rule("tests"))

    text = cursorrules.read_text(encoding="utf-8")
    assert "Always use tabs." in text  # user content preserved
    assert _MIRROR_START in text and _MIRROR_END in text
    assert "Always double-check tests." in text


def test_mirror_is_idempotent_on_repeated_updates(tmp_path: Path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Project rules\nRun pytest before committing.\n", encoding="utf-8")

    store = RulesStore(tmp_path)
    store.add(_rule("tests"))
    store.add(_rule("lint"))

    text = claude_md.read_text(encoding="utf-8")
    assert text.count(_MIRROR_START) == 1
    assert text.count(_MIRROR_END) == 1
    assert "Run pytest before committing." in text  # still preserved
    assert "Always double-check tests." in text
    assert "Always double-check lint." in text


# ---- observe_failure ----------------------------------------------------------


def _ledger_with_repeated_failures(tmp_path: Path, category: str, count: int) -> ContinuityLedger:
    ledger = ContinuityLedger(tmp_path)
    for i in range(count):
        ledger.record_failure(i + 1, f"Turn {i + 1} failed", "details", tags=["claude", category])
    return ledger


def test_observe_failure_returns_none_for_unknown_category(tmp_path: Path):
    ledger = ContinuityLedger(tmp_path)
    gate = _FakeGate()
    provider = MagicMock()
    result = observe_failure(
        tmp_path, ledger, gate=gate, turn=1, failure_title="t", failure_body="b", provider=provider,
    )
    assert result is None


def test_observe_failure_returns_none_below_repeat_threshold(tmp_path: Path):
    gate = _FakeGate(tests=_Verdict(ran=True, passed=False))
    ledger = _ledger_with_repeated_failures(tmp_path, "tests", count=1)
    provider = MagicMock()
    result = observe_failure(
        tmp_path, ledger, gate=gate, turn=1, failure_title="t", failure_body="b",
        provider=provider, min_repeats=2,
    )
    assert result is None
    provider.complete_json.assert_not_called()


def test_observe_failure_returns_none_without_provider(tmp_path: Path):
    gate = _FakeGate(tests=_Verdict(ran=True, passed=False))
    ledger = _ledger_with_repeated_failures(tmp_path, "tests", count=2)
    result = observe_failure(
        tmp_path, ledger, gate=gate, turn=2, failure_title="t", failure_body="b",
        provider=None, min_repeats=2,
    )
    assert result is None


def test_observe_failure_synthesizes_rule_once_threshold_met(tmp_path: Path):
    gate = _FakeGate(tests=_Verdict(ran=True, passed=False))
    ledger = _ledger_with_repeated_failures(tmp_path, "tests", count=2)
    provider = MagicMock()
    provider.complete_json.return_value = {"rule": "Run the full test suite locally before shipping."}

    rule = observe_failure(
        tmp_path, ledger, gate=gate, turn=2, failure_title="Turn 2 failed: x", failure_body="tests broke",
        provider=provider, min_repeats=2,
    )

    assert rule is not None
    assert rule.category == "tests"
    assert rule.rule_text == "Run the full test suite locally before shipping."
    assert RulesStore(tmp_path).has_rule_for_category("tests") is True


def test_observe_failure_is_noop_once_a_rule_exists_for_category(tmp_path: Path):
    gate = _FakeGate(tests=_Verdict(ran=True, passed=False))
    ledger = _ledger_with_repeated_failures(tmp_path, "tests", count=5)
    RulesStore(tmp_path).add(_rule("tests"))  # already has one
    provider = MagicMock()

    result = observe_failure(
        tmp_path, ledger, gate=gate, turn=5, failure_title="t", failure_body="b",
        provider=provider, min_repeats=2,
    )
    assert result is None
    provider.complete_json.assert_not_called()


def test_observe_failure_swallows_provider_exceptions(tmp_path: Path):
    gate = _FakeGate(tests=_Verdict(ran=True, passed=False))
    ledger = _ledger_with_repeated_failures(tmp_path, "tests", count=2)
    provider = MagicMock()
    provider.complete_json.side_effect = RuntimeError("provider exploded")

    result = observe_failure(
        tmp_path, ledger, gate=gate, turn=2, failure_title="t", failure_body="b",
        provider=provider, min_repeats=2,
    )
    assert result is None  # must never raise, loop must keep going


def test_observe_failure_returns_none_when_provider_gives_empty_rule(tmp_path: Path):
    gate = _FakeGate(tests=_Verdict(ran=True, passed=False))
    ledger = _ledger_with_repeated_failures(tmp_path, "tests", count=2)
    provider = MagicMock()
    provider.complete_json.return_value = {"rule": "   "}

    result = observe_failure(
        tmp_path, ledger, gate=gate, turn=2, failure_title="t", failure_body="b",
        provider=provider, min_repeats=2,
    )
    assert result is None
