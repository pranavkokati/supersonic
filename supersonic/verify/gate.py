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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from supersonic.providers.base import LLMProvider
from supersonic.verify.critic import CriticVerdict, judge as critic_judge
from supersonic.verify.dependency_trust import DependencyTrustVerdict
from supersonic.verify.qa import CheckResult, run_lint, run_tests
from supersonic.verify.telemetry_gate import TelemetryVerdict
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
) -> GateResult:
    tests = run_tests(workdir)
    lint = run_lint(workdir)
    critic = critic_judge(provider, goal=goal, diff=diff, invariants=invariants)
    thrash = thrash_detect(diff, recent_diffs)
    telemetry = telemetry if telemetry is not None else TelemetryVerdict()
    dependency_trust = dependency_trust if dependency_trust is not None else DependencyTrustVerdict()

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
    )
