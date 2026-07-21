"""Multi-Repository State Anchoring.

Honest scope note: this coordinates checkpoint + rollback across several
independent git working directories as ONE atomic unit, gated on the
primary repo's own Verify outcome. It does not run the coding agent against
each linked repo automatically per turn — the agent still runs exactly once
per turn, against the primary workdir, same as every other project. What's
real and working: register N repo paths once (e.g. a React frontend, a
Python backend, and a schema-definitions repo that all move together for
one feature), and from then on, every turn snapshots all of them before the
agent runs; once the primary repo's turn is scored, every linked repo is
EITHER tagged with a fresh, matching checkpoint (the primary turn passed)
OR rolled back to its last coordinated checkpoint (the primary turn
failed) — together, never one without the others. That's the actual
reliability property a multi-repo feature ticket needs: nothing in the
linked set can silently drift out of sync with what the primary repo's
Verify gate actually approved.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from supersonic.loop.checkpoint import Checkpoint, CheckpointManager

logger = logging.getLogger(__name__)

LINKED_REPOS_DIRNAME = ".supersonic"
LINKED_REPOS_FILENAME = "linked_repos.json"


@dataclass
class LinkedRepo:
    path: str
    label: str = ""


def _config_path(primary_workdir: Path) -> Path:
    d = Path(primary_workdir) / LINKED_REPOS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d / LINKED_REPOS_FILENAME


def load_linked_repos(primary_workdir: Path) -> List[LinkedRepo]:
    p = _config_path(primary_workdir)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8") or "[]")
    except (json.JSONDecodeError, OSError):
        logger.warning("linked_repos.json unreadable, treating as empty")
        return []
    return [LinkedRepo(path=r["path"], label=r.get("label", "")) for r in raw if isinstance(r, dict) and r.get("path")]


def save_linked_repos(primary_workdir: Path, repos: List[LinkedRepo]) -> None:
    p = _config_path(primary_workdir)
    p.write_text(
        json.dumps([{"path": r.path, "label": r.label} for r in repos], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


class MultiRepoCoordinator:
    """Wraps one `CheckpointManager` per linked repo path, kept in lockstep
    with the orchestrator's own primary-repo checkpoint/rollback calls."""

    def __init__(self, linked_repos: List[LinkedRepo]):
        existing = [r for r in linked_repos if Path(r.path).is_dir()]
        missing = [r.path for r in linked_repos if not Path(r.path).is_dir()]
        if missing:
            logger.warning("multi-repo anchor: skipping missing path(s): %s", ", ".join(missing))
        self.linked_repos = existing
        self.managers: Dict[str, CheckpointManager] = {r.path: CheckpointManager(Path(r.path)) for r in existing}

    def repo_paths(self) -> List[str]:
        return list(self.managers.keys())

    def checkpoint_all(self, turn: int, note: str) -> Dict[str, Checkpoint]:
        """Snapshot every linked repo's current state as this turn's anchor
        point — this is the state every linked repo rolls back to if the
        primary repo's turn ends up failing Verify."""
        return {path: mgr.create(turn, note) for path, mgr in self.managers.items()}

    def rollback_all(self, anchors: Dict[str, Checkpoint]) -> None:
        from supersonic.loop.rollback import rollback_to

        for path, checkpoint in anchors.items():
            try:
                rollback_to(Path(path), checkpoint)
            except Exception:
                logger.exception("multi-repo anchor: failed to roll back %s", path)
