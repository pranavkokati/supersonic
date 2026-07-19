"""Verify gate — combine four independent signals into one pass/fail decision.

A turn is accepted only if enough of the signals that actually *ran* came
back positive. Signals that didn't run (no test suite yet, no provider
configured) never count against a turn — the gate only judges on evidence it
actually has, and adapts its required margin down when fewer signals are
available.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from supersonic.providers.base import LLMProvider
from supersonic.verify.critic import CriticVerdict, judge as critic_judge
from supersonic.verify.qa import CheckResult, run_lint, run_tests
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

    def to_context_block(self) -> str:
        return "\n\n".join(
            [
                f"## Verify gate — {'PASS' if self.passed else 'FAIL'} ({self.signals_passed}/{self.signals_ran} signals)",
                self.tests.to_context_block(),
                self.lint.to_context_block(),
                self.critic.to_context_block(),
                self.thrash.to_context_block(),
            ]
        )

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "signals_ran": self.signals_ran,
            "signals_passed": self.signals_passed,
            "tests_passed": self.tests.passed if self.tests.ran else None,
            "lint_passed": self.lint.passed if self.lint.ran else None,
            "critic_satisfied": self.critic.satisfied if self.critic.ran else None,
            "thrashing": self.thrash.thrashing if self.thrash.ran else None,
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
) -> GateResult:
    tests = run_tests(workdir)
    lint = run_lint(workdir)
    critic = critic_judge(provider, goal=goal, diff=diff, invariants=invariants)
    thrash = thrash_detect(diff, recent_diffs)

    signal_status = []
    if tests.ran:
        signal_status.append(tests.passed)
    if lint.ran:
        signal_status.append(lint.passed)
    if critic.ran:
        signal_status.append(critic.satisfied)
    if thrash.ran:
        signal_status.append(not thrash.thrashing)

    signals_ran = len(signal_status)
    signals_passed = sum(1 for s in signal_status if s)

    if signals_ran == 0:
        passed = True
        summary = "No verification signals available yet — accepted by default."
    else:
        required = min(min_signals_pass, signals_ran)
        passed = signals_passed >= required
        summary = f"{signals_passed}/{signals_ran} verification signals passed (needed {required})."

    return GateResult(
        passed=passed,
        signals_ran=signals_ran,
        signals_passed=signals_passed,
        tests=tests,
        lint=lint,
        critic=critic,
        thrash=thrash,
        summary=summary,
    )
