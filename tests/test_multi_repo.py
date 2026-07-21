"""Multi-Repository State Anchoring (loop/multi_repo.py).

Honest scope under test: this coordinates checkpoint + rollback across
several independent git working directories as one atomic unit — it does
NOT run the coding agent against each linked repo. Tests use real temp git
repos (same pattern as test_checkpoint_rollback.py) rather than mocks, since
the whole point of this module is coordinating real CheckpointManager /
rollback_to calls across multiple directories.
"""

from __future__ import annotations

from pathlib import Path

from supersonic.loop.checkpoint import run_git
from supersonic.loop.multi_repo import (
    LinkedRepo,
    MultiRepoCoordinator,
    load_linked_repos,
    save_linked_repos,
)


def test_load_linked_repos_returns_empty_when_no_config(tmp_path: Path):
    assert load_linked_repos(tmp_path) == []


def test_save_and_load_round_trip(tmp_path: Path):
    repos = [LinkedRepo(path="/tmp/frontend", label="React frontend"), LinkedRepo(path="/tmp/backend", label="")]
    save_linked_repos(tmp_path, repos)

    loaded = load_linked_repos(tmp_path)
    assert [(r.path, r.label) for r in loaded] == [("/tmp/frontend", "React frontend"), ("/tmp/backend", "")]
    assert (tmp_path / ".supersonic" / "linked_repos.json").exists()


def test_load_linked_repos_treats_malformed_json_as_empty(tmp_path: Path):
    cfg = tmp_path / ".supersonic" / "linked_repos.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{ not valid json", encoding="utf-8")
    assert load_linked_repos(tmp_path) == []


def test_coordinator_skips_paths_that_dont_exist(tmp_path: Path):
    real_repo = tmp_path / "real"
    real_repo.mkdir()
    missing_repo = tmp_path / "does-not-exist"

    coordinator = MultiRepoCoordinator([LinkedRepo(path=str(real_repo)), LinkedRepo(path=str(missing_repo))])

    assert coordinator.repo_paths() == [str(real_repo)]


def test_checkpoint_all_snapshots_every_linked_repo(tmp_path: Path):
    frontend = tmp_path / "frontend"
    backend = tmp_path / "backend"
    frontend.mkdir()
    backend.mkdir()
    (frontend / "App.jsx").write_text("export default function App() {}\n")
    (backend / "main.py").write_text("def handler(): return 1\n")

    coordinator = MultiRepoCoordinator([LinkedRepo(path=str(frontend)), LinkedRepo(path=str(backend))])
    anchors = coordinator.checkpoint_all(1, "feature: add widget")

    assert set(anchors.keys()) == {str(frontend), str(backend)}
    for path, checkpoint in anchors.items():
        tags = run_git(["tag", "-l"], Path(path)).stdout.split()
        assert checkpoint.tag in tags


def test_rollback_all_reverts_every_linked_repo_together(tmp_path: Path):
    frontend = tmp_path / "frontend"
    backend = tmp_path / "backend"
    frontend.mkdir()
    backend.mkdir()
    (frontend / "App.jsx").write_text("v1 frontend\n")
    (backend / "main.py").write_text("v1 backend\n")

    coordinator = MultiRepoCoordinator([LinkedRepo(path=str(frontend)), LinkedRepo(path=str(backend))])
    good_anchors = coordinator.checkpoint_all(1, "known good")

    # Simulate a downstream failure: both repos drift forward...
    (frontend / "App.jsx").write_text("v2 frontend BROKEN\n")
    (backend / "main.py").write_text("v2 backend BROKEN\n")
    coordinator.checkpoint_all(2, "bad turn — will be rolled back")

    # ...then the primary repo's Verify gate fails, so everything rolls back together.
    coordinator.rollback_all(good_anchors)

    assert frontend.joinpath("App.jsx").read_text() == "v1 frontend\n"
    assert backend.joinpath("main.py").read_text() == "v1 backend\n"


def test_rollback_all_is_resilient_to_one_repo_failing(tmp_path: Path):
    """A rollback failure in one linked repo (e.g. a permissions issue, or
    the repo having been deleted out from under Supersonic mid-run) must not
    prevent the others from rolling back — see the try/except per-repo in
    MultiRepoCoordinator.rollback_all."""
    good_repo = tmp_path / "good"
    good_repo.mkdir()
    (good_repo / "f.py").write_text("v1\n")

    coordinator = MultiRepoCoordinator([LinkedRepo(path=str(good_repo))])
    anchors = coordinator.checkpoint_all(1, "known good")

    (good_repo / "f.py").write_text("v2 broken\n")
    coordinator.checkpoint_all(2, "bad turn")

    # Inject a bogus anchor for a path with no real CheckpointManager entry —
    # rollback_all must not raise even though this one entry can't resolve.
    from supersonic.loop.checkpoint import Checkpoint

    bogus_path = str(tmp_path / "vanished")
    anchors_with_bogus = {**anchors, bogus_path: Checkpoint(turn=1, tag="sonic-checkpoint-1", commit="deadbeef")}

    coordinator.rollback_all(anchors_with_bogus)  # must not raise
    assert (good_repo / "f.py").read_text() == "v1\n"
