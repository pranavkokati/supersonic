"""Checkpoint — git-native snapshotting before every verified turn.

Supersonic's safety net is not a custom snapshot format — it's git doing
what it already does well. Every turn that passes verification gets
committed and tagged; rollback.py resets straight back to the last such tag
when a turn fails. No bespoke serialization, no drift between "what we think
the state is" and what's actually on disk.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

TAG_PREFIX = "sonic-checkpoint"

# Seeded into every fresh project repo. Without this, `git add -A` on a checkpoint
# sweeps up whatever dependency trees the coding agent installs (node_modules,
# .venv, __pycache__, ...) — every commit balloons, diffs become useless for the
# critic and dashboard, and a GitHub push can fail outright on a huge tree.
DEFAULT_GITIGNORE = """\
__pycache__/
*.py[cod]
.venv/
venv/
env/
node_modules/
dist/
build/
*.egg-info/
.DS_Store
.env
.env.*
!.env.example
*.log
.pytest_cache/
.mypy_cache/
.ruff_cache/
coverage/
.next/
target/
"""


class GitError(RuntimeError):
    pass


def run_git(args: List[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


@dataclass
class Checkpoint:
    turn: int
    tag: str
    commit: str
    note: str = ""

    def to_dict(self) -> dict:
        return {"turn": self.turn, "tag": self.tag, "commit": self.commit[:12], "note": self.note}


class CheckpointManager:
    """Owns the git-native checkpoint history for one project workdir."""

    def __init__(self, workdir: Path):
        self.workdir = Path(workdir)
        self._ensure_repo()

    def _ensure_repo(self) -> None:
        if not (self.workdir / ".git").exists():
            run_git(["init", "-q"], self.workdir)
            run_git(["config", "user.email", "sonic@local"], self.workdir, check=False)
            run_git(["config", "user.name", "Supersonic"], self.workdir, check=False)
            gitignore = self.workdir / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(DEFAULT_GITIGNORE, encoding="utf-8")
            run_git(["add", "-A"], self.workdir)
            run_git(["commit", "-q", "-m", "chore: initial state", "--allow-empty"], self.workdir, check=False)

    def create(self, turn: int, note: str) -> Checkpoint:
        """Commit the current working tree and tag it as a checkpoint."""
        run_git(["add", "-A"], self.workdir)
        run_git(["commit", "-q", "-m", f"turn {turn}: {note}"[:200], "--allow-empty"], self.workdir, check=False)
        head = run_git(["rev-parse", "HEAD"], self.workdir).stdout.strip()
        tag = f"{TAG_PREFIX}-{turn}"
        run_git(["tag", "-f", tag, head], self.workdir)
        logger.info("checkpoint created: turn=%s tag=%s commit=%s", turn, tag, head[:8])
        return Checkpoint(turn=turn, tag=tag, commit=head, note=note)

    def list(self) -> List[Checkpoint]:
        res = run_git(["tag", "-l", f"{TAG_PREFIX}-*"], self.workdir, check=False)
        tags = sorted((t for t in res.stdout.splitlines() if t.strip()), key=lambda t: int(t.rsplit("-", 1)[-1]))
        out = []
        for tag in tags:
            turn = int(tag.rsplit("-", 1)[-1])
            commit = run_git(["rev-parse", tag], self.workdir, check=False).stdout.strip()
            out.append(Checkpoint(turn=turn, tag=tag, commit=commit))
        return out

    def diff_since(self, checkpoint: Optional[Checkpoint]) -> str:
        """Everything that's changed in the working tree since `checkpoint`,
        including brand-new files.

        Plain `git diff <commit>` only shows changes to files already in the
        index — a file the coding agent just created is untracked, and git
        diff is silent about untracked files by design (confirmed: `git
        status --short` shows `?? new.txt` while `git diff HEAD` shows
        nothing for the same file). That's the single most common shape of
        change in this product — turn 1 of every build is close to 100% new
        files, and later turns routinely add whole new modules — so every
        diff-based Verify signal (Syntax Shield, Dependency Trust, Secret
        Leak, Test Quality, the critic, the thrash detector) and Review Risk
        would silently see an empty diff on exactly those turns without this
        fix. `git add -A` first (safe: staging isn't committing, and
        `create()` above does the identical `git add -A` immediately before
        every checkpoint commit anyway) so `--cached` diff reflects the
        entire working tree, new files included."""
        run_git(["add", "-A"], self.workdir, check=False)
        args = ["diff", "--cached"]
        if checkpoint:
            args.append(checkpoint.commit)
        res = run_git(args, self.workdir, check=False)
        return res.stdout
