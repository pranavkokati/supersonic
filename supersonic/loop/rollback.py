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


def rollback_to(workdir: Path, checkpoint: Checkpoint) -> None:
    """Hard-reset the workdir to the given checkpoint, discarding everything after it.

    `.continuity/` (the ledger itself) is preserved across the reset by copying it out
    before `git reset --hard` and restoring it after. `git reset --hard` reverts *every*
    uncommitted change relative to the target commit — including the ledger write that
    just recorded why this turn failed — so `git clean`'s path exclusion alone isn't
    enough to protect it (that only governs untracked-file cleanup, not the reset
    itself). Losing the memory of *why* a turn failed along with the code that failed
    would defeat the entire point of rolling back.
    """
    logger.warning("rollback -> turn=%s commit=%s", checkpoint.turn, checkpoint.commit[:8])

    continuity_src = workdir / CONTINUITY_DIR
    backup_dir: Path | None = None
    if continuity_src.exists():
        backup_dir = Path(tempfile.mkdtemp(prefix="sonic-continuity-"))
        shutil.copytree(continuity_src, backup_dir, dirs_exist_ok=True)

    run_git(["reset", "--hard", checkpoint.commit], workdir)
    run_git(["clean", "-fd", "-e", CONTINUITY_DIR], workdir, check=False)

    if backup_dir is not None:
        continuity_src.mkdir(parents=True, exist_ok=True)
        shutil.copytree(backup_dir, continuity_src, dirs_exist_ok=True)
        shutil.rmtree(backup_dir, ignore_errors=True)
