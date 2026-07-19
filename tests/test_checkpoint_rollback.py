"""Checkpoint / Rollback — git-native snapshotting against a real temp repo."""

from __future__ import annotations

from supersonic.loop.checkpoint import CheckpointManager, run_git
from supersonic.loop.rollback import rollback_to


def test_checkpoint_manager_inits_git_repo(tmp_path):
    CheckpointManager(tmp_path)
    assert (tmp_path / ".git").exists()


def test_checkpoint_manager_seeds_gitignore_to_avoid_dependency_bloat(tmp_path):
    CheckpointManager(tmp_path)
    gitignore = (tmp_path / ".gitignore").read_text()
    for pattern in ("node_modules/", ".venv/", "__pycache__/", ".env"):
        assert pattern in gitignore


def test_checkpoint_excludes_node_modules_from_the_commit(tmp_path):
    mgr = CheckpointManager(tmp_path)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "some-dep.js").write_text("junk")
    (tmp_path / "app.js").write_text("console.log('hi')")
    mgr.create(1, "installed deps")

    tracked = run_git(["ls-files"], tmp_path).stdout
    assert "app.js" in tracked
    assert "node_modules" not in tracked


def test_checkpoint_does_not_overwrite_an_existing_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("custom-rule/\n")
    CheckpointManager(tmp_path)
    assert (tmp_path / ".gitignore").read_text() == "custom-rule/\n"


def test_checkpoint_create_commits_and_tags(tmp_path):
    mgr = CheckpointManager(tmp_path)
    (tmp_path / "main.py").write_text("print('v1')\n")
    cp = mgr.create(1, "first turn")

    assert cp.turn == 1
    assert cp.commit
    tags = run_git(["tag", "-l"], tmp_path).stdout.split()
    assert cp.tag in tags


def test_checkpoint_list_returns_in_turn_order(tmp_path):
    mgr = CheckpointManager(tmp_path)
    (tmp_path / "a.txt").write_text("a")
    mgr.create(1, "turn 1")
    (tmp_path / "a.txt").write_text("b")
    mgr.create(2, "turn 2")
    (tmp_path / "a.txt").write_text("c")
    mgr.create(3, "turn 3")

    checkpoints = mgr.list()
    assert [c.turn for c in checkpoints] == [1, 2, 3]


def test_rollback_reverts_file_contents(tmp_path):
    mgr = CheckpointManager(tmp_path)
    target = tmp_path / "main.py"
    target.write_text("print('good state')\n")
    good_checkpoint = mgr.create(1, "known good")

    target.write_text("this is broken syntax +++ (\n")
    mgr.create(2, "bad attempt")
    assert "broken" in target.read_text()

    rollback_to(tmp_path, good_checkpoint)
    assert target.read_text() == "print('good state')\n"


def test_rollback_preserves_continuity_dir(tmp_path):
    mgr = CheckpointManager(tmp_path)
    (tmp_path / "main.py").write_text("v1")
    good_checkpoint = mgr.create(1, "known good")

    continuity = tmp_path / ".continuity"
    continuity.mkdir(exist_ok=True)
    (continuity / "ledger.jsonl").write_text('{"kind": "failure"}\n')

    (tmp_path / "main.py").write_text("v2 broken")
    mgr.create(2, "bad attempt")

    rollback_to(tmp_path, good_checkpoint)
    assert (continuity / "ledger.jsonl").exists()


def test_diff_since_reflects_uncommitted_changes(tmp_path):
    mgr = CheckpointManager(tmp_path)
    (tmp_path / "main.py").write_text("v1\n")
    cp = mgr.create(1, "turn 1")
    (tmp_path / "main.py").write_text("v2\n")

    diff = mgr.diff_since(cp)
    assert "v2" in diff


def test_diff_since_includes_brand_new_untracked_files(tmp_path):
    """Regression test for a real bug: plain `git diff <commit>` is silent
    about files that are new and never staged — it only shows changes to
    files already in the index. That's the single most common shape of
    change in this product (turn 1 of every build is close to 100% new
    files), so every diff-based Verify signal would have silently seen an
    empty diff on exactly those turns without staging first. See
    CheckpointManager.diff_since's docstring for the full explanation."""
    mgr = CheckpointManager(tmp_path)
    cp = mgr.create(1, "empty turn 1")

    (tmp_path / "brand_new_module.py").write_text("def handler():\n    return 42\n")

    diff = mgr.diff_since(cp)
    assert "brand_new_module.py" in diff
    assert "handler" in diff


def test_diff_since_staging_does_not_commit(tmp_path):
    """diff_since() must be a read-only preview — it may stage files (git
    add) to make the diff complete, but it must never create a commit."""
    mgr = CheckpointManager(tmp_path)
    cp = mgr.create(1, "turn 1")
    before = run_git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    (tmp_path / "new_file.py").write_text("x = 1\n")
    mgr.diff_since(cp)

    after = run_git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    assert before == after
    # The file is staged (that's how the diff sees it) but still uncommitted.
    status = run_git(["status", "--short"], tmp_path).stdout
    assert "A  new_file.py" in status
