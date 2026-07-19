"""Verify gate — combine independent signals into one pass/fail decision.

The core is four signals (tests, lint, critic, thrash) that are always
attempted. A turn is accepted only if enough of the signals that actually
*ran* came back positive. Signals that didn't run (no test suite yet, no
provider configured) never count against a turn — the gate only judges on
evidence it actually has, and adapts its required margin down when fewer
signals are available.

An OPTIONAL fifth signal — the DLE Telemetry Gate (`verify/telemetry_gate.py`)
— can be passed in via `telemetry=`. It follows the exact same "only counts
if it ran" rule as the original four, so callers that never pass it (every
existing caller, and every existing test) see byte-identical behavior to
before this signal existed.

An OPTIONAL sixth signal — the Dependency Trust Gate
(`verify/dependency_trust.py`) — can be passed in via `dependency_trust=`,
same non-breaking rule. Unlike the others it doesn't get a fair vote: a
`nonexistent` finding (a hallucinated package name that fails registry
lookup) fails the gate outright regardless of what the other signals say,
because a turn that ships a slopsquatting-vulnerable dependency is not a
"3 of 5 signals passed, good enough" situation.

An OPTIONAL seventh signal — the Secret Leak Gate (`verify/secret_leak.py`)
— can be passed in via `secret_leak=`, same non-breaking rule and the same
no-fair-vote treatment as Dependency Trust: a `critical` finding (a real
credential shape, or a new .env file) fails the turn outright.

An OPTIONAL eighth signal — the Test Quality Gate (`verify/test_quality.py`)
— can be passed in via `test_quality=`, same non-breaking rule, but back to
the *fair-vote* treatment (like telemetry): a surviving mutant is evidence a
touched function's tests are weaker than they look, not proof of a bug, so
it participates in the normal N-of-M count instead of failing the turn
outright.

`critic_model=` is a separate, orthogonal knob — not a new signal, just an
override for which model the existing critic signal above uses this call.
Risk-Aware Model Escalation (`loop/orchestrator.py`) sets it to the
provider's stronger `default_model` for the turn right after one shipped
with a HIGH-risk Review Risk finding; every other caller leaves it unset and
gets the original fast_model/default_model behavior unchanged.

`build_qa_reprompt()` generalizes the "one corrective re-prompt, then accept
the verdict" pattern established by Syntax Shield / Dependency Trust / Secret
Leak to the two original Verify signals mechanical enough for it to actually
help: a failing test or a lint/typecheck error. It deliberately does NOT
cover the critic or thrash signals — those are judgment calls ("does this
satisfy the goal", "is the agent going in circles"), not a specific bug a
re-prompt can point at and expect fixed. Callers decide when to use it and
how many times (this module has no opinion on retry count); the intended
caller pattern (see `loop/orchestrator.py`) is exactly one retry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from supersonic.providers.base import LLMProvider
from supersonic.verify.critic import CriticVerdict, judge as critic_judge
from supersonic.verify.dependency_trust import DependencyTrustVerdict
from supersonic.verify.qa import CheckResult, run_lint, run_tests
from supersonic.verify.secret_leak import SecretLeakVerdict
from supersonic.verify.telemetry_gate import TelemetryVerdict
from supersonic.verify.test_quality import TestQualityVerdict
from supersonic.verify.thrash import ThrashVerdict, detect as thrash_detect


@dataclass
class GateResult:
    passed: bool
    signals_ran: int
    signals_passed: int
    tests: CheckResult
    lint: CheckResult
    critic: CriticVerdict
    thrash: ThrashVerdict
    summary: str
    telemetry: TelemetryVerdict = field(default_factory=TelemetryVerdict)
    dependency_trust: DependencyTrustVerdict = field(default_factory=DependencyTrustVerdict)
    secret_leak: SecretLeakVerdict = field(default_factory=SecretLeakVerdict)
    test_quality: TestQualityVerdict = field(default_factory=TestQualityVerdict)

    def to_context_block(self) -> str:
        blocks = [
            f"## Verify gate — {'PASS' if self.passed else 'FAIL'} ({self.signals_passed}/{self.signals_ran} signals)",
            self.tests.to_context_block(),
            self.lint.to_context_block(),
            self.critic.to_context_block(),
            self.thrash.to_context_block(),
        ]
        if self.telemetry.ran:
            blocks.append(self.telemetry.to_context_block())
        if self.dependency_trust.ran:
            blocks.append(self.dependency_trust.to_context_block())
        if self.secret_leak.ran:
            blocks.append(self.secret_leak.to_context_block())
        if self.test_quality.ran:
            blocks.append(self.test_quality.to_context_block())
        return "\n\n".join(blocks)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "signals_ran": self.signals_ran,
            "signals_passed": self.signals_passed,
            "tests_passed": self.tests.passed if self.tests.ran else None,
            "lint_passed": self.lint.passed if self.lint.ran else None,
            "critic_satisfied": self.critic.satisfied if self.critic.ran else None,
            "thrashing": self.thrash.thrashing if self.thrash.ran else None,
            "telemetry_passed": self.telemetry.passed if self.telemetry.ran else None,
            "dependency_trust_passed": self.dependency_trust.ok if self.dependency_trust.ran else None,
            "secret_leak_passed": self.secret_leak.ok if self.secret_leak.ran else None,
            "test_quality_passed": self.test_quality.passed if self.test_quality.ran else None,
            "summary": self.summary,
        }


def run_gate(
    workdir: Path,
    *,
    provider: Optional[LLMProvider],
    goal: str,
    diff: str,
    invariants: List[str],
    recent_diffs: List[str],
    min_signals_pass: int = 3,
    telemetry: Optional[TelemetryVerdict] = None,
    dependency_trust: Optional[DependencyTrustVerdict] = None,
    secret_leak: Optional[SecretLeakVerdict] = None,
    test_quality: Optional[TestQualityVerdict] = None,
    critic_model: Optional[str] = None,
) -> GateResult:
    tests = run_tests(workdir)
    lint = run_lint(workdir)
    critic = critic_judge(provider, goal=goal, diff=diff, invariants=invariants, model=critic_model)
    thrash = thrash_detect(diff, recent_diffs)
    telemetry = telemetry if telemetry is not None else TelemetryVerdict()
    dependency_trust = dependency_trust if dependency_trust is not None else DependencyTrustVerdict()
    secret_leak = secret_leak if secret_leak is not None else SecretLeakVerdict()
    test_quality = test_quality if test_quality is not None else TestQualityVerdict()

    signal_status = []
    if tests.ran:
        signal_status.append(tests.passed)
    if lint.ran:
        signal_status.append(lint.passed)
    if critic.ran:
        signal_status.append(critic.satisfied)
    if thrash.ran:
        signal_status.append(not thrash.thrashing)
    if telemetry.ran:
        signal_status.append(telemetry.passed)
    if dependency_trust.ran:
        signal_status.append(dependency_trust.ok)
    if secret_leak.ran:
        signal_status.append(secret_leak.ok)
    if test_quality.ran:
        signal_status.append(test_quality.passed)

    signals_ran = len(signal_status)
    signals_passed = sum(1 for s in signal_status if s)

    if signals_ran == 0:
        passed = True
        summary = "No verification signals available yet — accepted by default."
    else:
        required = min(min_signals_pass, signals_ran)
        passed = signals_passed >= required
        summary = f"{signals_passed}/{signals_ran} verification signals passed (needed {required})."

    # A hallucinated/nonexistent dependency is not a "3 of 5 is good enough"
    # situation — it fails the turn outright regardless of how the other
    # signals landed, same as an unresolved syntax error would.
    if dependency_trust.ran and not dependency_trust.ok:
        passed = False
        summary = f"Dependency Trust Gate failed: {len(dependency_trust.critical)} nonexistent package(s). " + summary

    # Same treatment for a likely hardcoded credential: not a vote, a veto.
    if secret_leak.ran and not secret_leak.ok:
        passed = False
        summary = f"Secret Leak Gate failed: {len(secret_leak.critical)} likely credential(s) found. " + summary

    return GateResult(
        passed=passed,
        signals_ran=signals_ran,
        signals_passed=signals_passed,
        tests=tests,
        lint=lint,
        critic=critic,
        thrash=thrash,
        summary=summary,
        telemetry=telemetry,
        dependency_trust=dependency_trust,
        secret_leak=secret_leak,
        test_quality=test_quality,
    )


def build_qa_reprompt(result: GateResult) -> str:
    """If `result` failed because of a fixable tests/lint signal, build a
    corrective re-prompt string naming the exact failure output — the exact
    same shape of fix Syntax Shield/Dependency Trust/Secret Leak already get.
    Returns "" if the gate passed, or if it failed for reasons this function
    can't point at anything mechanical for (critic/thrash only) — the caller
    should just accept the verdict in that case rather than re-prompting
    blindly."""
    if result.passed:
        return ""
    parts: List[str] = []
    if result.tests.ran and not result.tests.passed:
        parts.append(
            "## Verify gate caught a failing test suite — fix ONLY this, before anything else.\n"
            f"`{result.tests.command}`\n```\n{result.tests.output[-2000:]}\n```"
        )
    if result.lint.ran and not result.lint.passed:
        parts.append(
            "## Verify gate caught a lint/typecheck failure — fix ONLY this, before anything else.\n"
            f"`{result.lint.command}`\n```\n{result.lint.output[-2000:]}\n```"
        )
    return "\n\n".join(parts)
