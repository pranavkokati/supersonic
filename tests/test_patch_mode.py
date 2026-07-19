"""DLE — patch-diff mode: diff extraction, git apply, and the real fallback chain."""

from __future__ import annotations



from supersonic.agents.patch_mode import extract_diff, run_patch_diff_turn, try_git_apply
from supersonic.agents.runner import AgentResult
from supersonic.loop.checkpoint import run_git


def _init_repo(tmp_path):
    run_git(["init", "-q"], tmp_path)
    run_git(["config", "user.email", "t@example.com"], tmp_path)
    run_git(["config", "user.name", "Test"], tmp_path)
    (tmp_path / "main.py").write_text("print('v1')\n")
    run_git(["add", "-A"], tmp_path)
    run_git(["commit", "-q", "-m", "init"], tmp_path)


def _make_diff(tmp_path) -> str:
    """Produce a real, valid unified diff by actually editing + diffing a repo."""
    (tmp_path / "main.py").write_text("print('v2')\n")
    diff = run_git(["diff"], tmp_path).stdout
    # revert so callers can re-apply it from a clean state
    run_git(["checkout", "--", "main.py"], tmp_path)
    return diff


class _FakeRunner:
    """Stands in for CodingAgentRunner — returns pre-scripted AgentResults in order."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def run(self, prompt, workdir, on_line=None, model=None):
        self.calls += 1
        self.last_model = model
        return self._results.pop(0)


def test_extract_diff_from_fenced_block():
    text = "Here you go:\n```diff\ndiff --git a/x.py b/x.py\n+print(1)\n```\n"
    diff = extract_diff(text)
    assert diff is not None
    assert diff.startswith("diff --git")


def test_extract_diff_from_raw_text_without_fence():
    text = "diff --git a/x.py b/x.py\nindex 111..222 100644\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
    diff = extract_diff(text)
    assert diff is not None
    assert "--- a/x.py" in diff


def test_extract_diff_returns_none_when_no_diff_present():
    assert extract_diff("I made the change directly, no diff to show.") is None
    assert extract_diff("") is None


def test_try_git_apply_succeeds_on_valid_diff(tmp_path):
    _init_repo(tmp_path)
    diff = _make_diff(tmp_path)

    check = try_git_apply(diff, tmp_path, check_only=True)
    assert check.returncode == 0

    applied = try_git_apply(diff, tmp_path)
    assert applied.returncode == 0
    assert "v2" in (tmp_path / "main.py").read_text()


def test_try_git_apply_fails_on_garbage_diff(tmp_path):
    _init_repo(tmp_path)
    bogus = "diff --git a/nope.py b/nope.py\n--- a/nope.py\n+++ b/nope.py\n@@ -1,5 +1,5 @@\n-this does not exist\n+neither does this\n"
    result = try_git_apply(bogus, tmp_path, check_only=True)
    assert result.returncode != 0
    assert result.stderr.strip() != ""


def test_run_patch_diff_turn_applies_on_first_try(tmp_path):
    _init_repo(tmp_path)
    diff = _make_diff(tmp_path)
    runner = _FakeRunner([AgentResult(agent="claude", success=True, output=f"```diff\n{diff}```", command="claude")])

    result = run_patch_diff_turn(runner, "do the thing", tmp_path)

    assert result.used_patch_mode is True
    assert result.applied is True
    assert result.attempts == 1
    assert runner.calls == 1
    assert "v2" in (tmp_path / "main.py").read_text()


def test_run_patch_diff_turn_falls_back_when_no_diff_ever_produced(tmp_path):
    _init_repo(tmp_path)
    runner = _FakeRunner([
        AgentResult(agent="claude", success=True, output="I edited the files directly, no diff.", command="claude"),
        AgentResult(agent="claude", success=True, output="Still no diff, sorry.", command="claude"),
    ])

    result = run_patch_diff_turn(runner, "do the thing", tmp_path)

    assert result.applied is False
    assert result.attempts == 2
    assert runner.calls == 2
    assert "unified diff" in result.fallback_reason


def test_run_patch_diff_turn_recovers_on_second_attempt(tmp_path):
    _init_repo(tmp_path)
    good_diff = _make_diff(tmp_path)
    bad_diff = "diff --git a/nope.py b/nope.py\n--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n-x\n+y\n"

    runner = _FakeRunner([
        AgentResult(agent="claude", success=True, output=f"```diff\n{bad_diff}```", command="claude"),
        AgentResult(agent="claude", success=True, output=f"```diff\n{good_diff}```", command="claude"),
    ])

    result = run_patch_diff_turn(runner, "do the thing", tmp_path)

    assert result.applied is True
    assert result.attempts == 2
    assert runner.calls == 2
    assert "v2" in (tmp_path / "main.py").read_text()


def test_run_patch_diff_turn_gives_up_after_one_reprompt(tmp_path):
    _init_repo(tmp_path)
    bad_diff = "diff --git a/nope.py b/nope.py\n--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n-x\n+y\n"

    runner = _FakeRunner([
        AgentResult(agent="claude", success=True, output=f"```diff\n{bad_diff}```", command="claude"),
        AgentResult(agent="claude", success=True, output=f"```diff\n{bad_diff}```", command="claude"),
    ])

    result = run_patch_diff_turn(runner, "do the thing", tmp_path)

    assert result.applied is False
    assert result.attempts == 2
    assert runner.calls == 2  # exactly one re-prompt, not an open-ended retry loop
    assert "git apply --check" in result.fallback_reason
