"""Git worktree isolation for Agent Racing.

Each racing entrant gets its own working copy checked out from the same
history, so two coding-agent CLIs can run concurrently on the same project
without stepping on each other's file writes. The winner's worktree is
squash-merged back into the base project; the loser's is torn down.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from supersonic.loop.checkpoint import run_git

logger = logging.getLogger(__name__)


class AgentWorktree:
    def __init__(self, base_workdir: Path, agent_name: str):
        self.base = Path(base_workdir)
        self.agent_name = agent_name
        suffix = uuid.uuid4().hex[:6]
        self.branch = f"sonic-race-{agent_name}-{suffix}"
        self.path = self.base.parent / f"{self.base.name}.race-{agent_name}-{suffix}"

    def create(self) -> Path:
        run_git(["worktree", "add", "-b", self.branch, str(self.path), "HEAD"], self.base)
        logger.info("worktree created for %s at %s", self.agent_name, self.path)
        return self.path

    def merge_into(self, base_workdir: Path) -> None:
        run_git(["add", "-A"], self.path)
        run_git(["commit", "-q", "-m", f"race: {self.agent_name} candidate", "--allow-empty"], self.path, check=False)
        run_git(["merge", "--squash", self.branch], base_workdir, check=False)
        run_git(["add", "-A"], base_workdir)
        run_git(["commit", "-q", "-m", f"race winner: {self.agent_name}", "--allow-empty"], base_workdir, check=False)
        self._teardown()

    def discard(self) -> None:
        self._teardown()

    def _teardown(self) -> None:
        remove_res = run_git(["worktree", "remove", "--force", str(self.path)], self.base, check=False)
        if remove_res.returncode != 0:
            logger.warning("failed to remove race worktree %s: %s", self.path, remove_res.stderr.strip()[:300])
        branch_res = run_git(["branch", "-D", self.branch], self.base, check=False)
        if branch_res.returncode != 0:
            logger.warning("failed to delete race branch %s: %s", self.branch, branch_res.stderr.strip()[:300])
