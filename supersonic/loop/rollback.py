"""Rollback — snap back to the last verified-good checkpoint.

This is the ratchet mechanism: forward progress is only retained once a turn
clears Verify. A turn that fails badly is reverted in full rather than left
to compound into a worse state, and the reason gets written to the
Continuity Graph as a `failure` entry so the next attempt starts with that
lesson already in context.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from supersonic.loop.checkpoint import Checkpoint, run_git

logger = logging.getLogger(__name__)

CONTINUITY_DIR = ".continuity"
# Self-Evolving Rules Engine state (rules.json/rules.md) and Multi-Repository
# State Anchoring's linked_repos.json both live here. observe_failure() can
# write a brand-new rules.json in the SAME failed turn that's about to be
# rolled back — without this preserved too, `git clean -fd` would delete a
# rule the instant it's learned, on precisely the turn it was learned on,
# defeating the entire feature. See test_rollback_preserves_supersonic_dir.
SUPERSONIC_DIR = ".supersonic"
_PRESERVED_DIRS = (CONTINUITY_DIR, SUPERSONIC_DIR)


def rollback_to(workdir: Path, checkpoint: Checkpoint) -> None:
    """Hard-reset the workdir to the given checkpoint, discarding everything after it.

    `.continuity/` (the ledger) and `.supersonic/` (Rules Engine + Multi-Repo Anchoring
    state) are both preserved across the reset by copying them out before
    `git reset --hard` and restoring them after. `git reset --hard` reverts *every*
    uncommitted change relative to the target commit — including the ledger write that
    just recorded why this turn failed, and any rule the Rules Engine just learned from
    it — so `git clean`'s path exclusion alone isn't enough to protect them (that only
    governs untracked-file cleanup, not the reset itself). Losing the memory of *why* a
    turn failed, or a rule just learned from that exact failure, along with the code
    that failed would defeat the entire point of rolling back.
    """
    logger.warning("rollback -> turn=%s commit=%s", checkpoint.turn, checkpoint.commit[:8])

    backups: dict[str, Path] = {}
    for dirname in _PRESERVED_DIRS:
        src = workdir / dirname
        if src.exists():
            backup_dir = Path(tempfile.mkdtemp(prefix=f"sonic-{dirname.lstrip('.')}-"))
            shutil.copytree(src, backup_dir, dirs_exist_ok=True)
            backups[dirname] = backup_dir

    run_git(["reset", "--hard", checkpoint.commit], workdir)
    clean_args = ["clean", "-fd"]
    for dirname in _PRESERVED_DIRS:
        clean_args += ["-e", dirname]
    run_git(clean_args, workdir, check=False)

    for dirname, backup_dir in backups.items():
        dest = workdir / dirname
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(backup_dir, dest, dirs_exist_ok=True)
        shutil.rmtree(backup_dir, ignore_errors=True)
