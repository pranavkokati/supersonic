"""Agent Racing — bandit-gated, worktree-isolated, concurrent execution.

Only invoked when AgentBandit.should_race() says the outcome for this task
type is still uncertain. Each participant gets its own git worktree so
neither can clobber the other's changes; both run concurrently via threads
(subprocess I/O releases the GIL, so wall-clock cost is roughly
max(agent_a, agent_b), not the sum). The Verify gate scores both diffs; the
winner's worktree is merged back, the loser's is discarded, and the outcome
updates the bandit's posteriors.
"""

from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from supersonic.agents.runner import AgentResult, CodingAgentRunner
from supersonic.agents.worktree import AgentWorktree
from supersonic.config import UserSecrets
from supersonic.loop.checkpoint import run_git
from supersonic.providers.base import LLMProvider
from supersonic.verify.gate import GateResult, run_gate

logger = logging.getLogger(__name__)


@dataclass
class RaceEntrant:
    agent: str
    worktree: AgentWorktree
    result: Optional[AgentResult] = None
    gate: Optional[GateResult] = None
    score: float = 0.0


@dataclass
class RaceOutcome:
    task_type: str
    entrants: List[RaceEntrant]
    winner: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "task_type": self.task_type,
            "winner": self.winner,
            "reason": self.reason,
            "entrants": [
                {
                    "agent": e.agent,
                    "score": round(e.score, 2),
                    "success": bool(e.result and e.result.success),
                    "gate": e.gate.to_dict() if e.gate else None,
                }
                for e in self.entrants
            ],
        }


def _score(entrant: RaceEntrant) -> float:
    if entrant.result is None or not entrant.result.success:
        return -1.0
    if entrant.gate is None:
        return 0.0
    score = float(entrant.gate.signals_passed)
    if entrant.gate.critic.ran:
        score += entrant.gate.critic.confidence
    return score


def run_race(
    *,
    base_workdir: Path,
    task_type: str,
    agents: List[str],
    secrets: UserSecrets,
    prompt: str,
    provider: Optional[LLMProvider],
    goal: str,
    invariants: List[str],
    recent_diffs: List[str],
    min_signals_pass: int,
    challenger_turn_cap: int,
    on_line: Optional[Callable[[str, str], None]] = None,
) -> RaceOutcome:
    entrants = [RaceEntrant(agent=name, worktree=AgentWorktree(base_workdir, name)) for name in agents]

    def _run_one(entrant: RaceEntrant) -> RaceEntrant:
        entrant.worktree.create()
        runner = CodingAgentRunner(entrant.agent, secrets, turn_cap=challenger_turn_cap)
        line_cb = (lambda line, _a=entrant.agent: on_line(_a, line)) if on_line else None
        entrant.result = runner.run(prompt, entrant.worktree.path, on_line=line_cb)
        diff = run_git(["diff"], entrant.worktree.path, check=False).stdout
        entrant.gate = run_gate(
            entrant.worktree.path,
            provider=provider,
            goal=goal,
            diff=diff,
            invariants=invariants,
            recent_diffs=recent_diffs,
            min_signals_pass=min_signals_pass,
        )
        entrant.score = _score(entrant)
        return entrant

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(entrants), 1)) as pool:
        list(pool.map(_run_one, entrants))

    entrants.sort(key=lambda e: e.score, reverse=True)
    winner = entrants[0]
    reason = f"{winner.agent} scored {winner.score:.2f} vs " + ", ".join(
        f"{e.agent}={e.score:.2f}" for e in entrants[1:]
    )

    winner.worktree.merge_into(base_workdir)
    for e in entrants:
        if e is not winner:
            e.worktree.discard()

    return RaceOutcome(task_type=task_type, entrants=entrants, winner=winner.agent, reason=reason)
