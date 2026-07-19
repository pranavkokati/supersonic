"""Bandit-Gated Agent Racing — Thompson sampling over (agent, task-type) pairs.

Racing every coding-agent backend on every turn would double agent-call cost
for the entire run. Instead, agent selection is a contextual multi-armed
bandit: a Beta(alpha, beta) posterior is tracked per (agent, task_type),
updated after every race. Before a turn, we sample from each configured
agent's posterior for the current task type — if the samples are still
close (we don't yet know which agent wins at this kind of work), we race, to
generate a real observation. Once one agent's posterior has pulled
decisively ahead, we stop racing and just run it alone.

Thompson sampling has a standard, provable property: O(log T) cumulative
regret, meaning the frequency of "wasted" exploration shrinks over the
course of a run rather than staying constant. A hard `max_race_turns`
ceiling in config.py backstops this regardless of what the bandit wants.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

STATE_FILENAME = "bandit_state.json"

_TASK_KEYWORDS: Dict[str, tuple] = {
    "frontend": ("ui", "component", "css", "style", "page", "layout", "react", "frontend", "button", "form"),
    "backend": ("api", "endpoint", "route", "server", "database", "schema", "migration", "backend", "auth"),
    "test": ("test", "pytest", "spec", "coverage", "assert"),
    "infra": ("deploy", "docker", "ci", "config", "build", "pipeline", "infra"),
    "bugfix": ("fix", "bug", "error", "crash", "regression", "broken"),
}


def classify_task(goal: str) -> str:
    """Cheap keyword classifier — no LLM call, costs nothing to run every turn."""
    lowered = goal.lower()
    scores = {kind: sum(1 for kw in kws if kw in lowered) for kind, kws in _TASK_KEYWORDS.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "general"


@dataclass
class ArmStats:
    alpha: float = 1.0  # prior wins + 1
    beta: float = 1.0   # prior losses + 1

    @property
    def trials(self) -> int:
        return int(self.alpha + self.beta - 2)

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)


class AgentBandit:
    """Persists per-(agent, task_type) Beta posteriors to disk across runs of one project."""

    def __init__(self, workdir: Path, agents: List[str], seed: Optional[int] = None):
        self.workdir = Path(workdir)
        self.agents = agents
        self.path = self.workdir / ".continuity" / STATE_FILENAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rng = random.Random(seed)
        self._state: Dict[str, Dict[str, ArmStats]] = self._load()

    def _load(self) -> Dict[str, Dict[str, ArmStats]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        out: Dict[str, Dict[str, ArmStats]] = {}
        for task_type, arms in raw.items():
            out[task_type] = {agent: ArmStats(**stats) for agent, stats in arms.items()}
        return out

    def _save(self) -> None:
        raw = {
            task_type: {agent: {"alpha": a.alpha, "beta": a.beta} for agent, a in arms.items()}
            for task_type, arms in self._state.items()
        }
        self.path.write_text(json.dumps(raw, indent=2))

    def _arms(self, task_type: str) -> Dict[str, ArmStats]:
        return self._state.setdefault(task_type, {agent: ArmStats() for agent in self.agents})

    def should_race(self, task_type: str, *, overlap_margin: float = 0.15, min_trials: int = 3) -> bool:
        """True while we're still uncertain which agent wins at this task type (explore).
        False once one agent has pulled decisively ahead (exploit — run it alone)."""
        arms = self._arms(task_type)
        if len(arms) < 2:
            return False
        if min(a.trials for a in arms.values()) < min_trials:
            return True
        samples = sorted((self.rng.betavariate(a.alpha, a.beta) for a in arms.values()), reverse=True)
        return (samples[0] - samples[1]) < overlap_margin

    def best_agent(self, task_type: str) -> str:
        arms = self._arms(task_type)
        return max(arms, key=lambda name: arms[name].mean)

    def record_result(self, task_type: str, winner: str, participants: List[str]) -> None:
        arms = self._arms(task_type)
        for agent in participants:
            stats = arms.setdefault(agent, ArmStats())
            if agent == winner:
                stats.alpha += 1
            else:
                stats.beta += 1
        self._save()

    def win_rates(self) -> Dict[str, Dict[str, float]]:
        return {
            task_type: {agent: round(a.mean, 3) for agent, a in arms.items()}
            for task_type, arms in self._state.items()
        }
