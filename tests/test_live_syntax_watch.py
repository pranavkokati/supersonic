"""Live Syntax Watch (verify/live_syntax_watch.py) — the concurrent
filesystem watcher that re-parses a touched .py file within a poll interval
of it being saved. Explicitly NOT a claim of write-syscall interception; see
the module docstring. The real contract: a file that already existed the
instant the watcher started is never flagged just for existing (a
pre-existing broken file is never misreported) -- only once it's actually
edited after that point. A file that did NOT exist at start time (the agent
creates it fresh during the turn) is checked on its very first sighting,
since there's no "pre-existing" state to protect it from being flagged for.

This second half of the contract was a real bug, found and fixed in this
same session: the original implementation only had one "first sighting"
concept for both pre-existing and brand-new files, so a file written
exactly once during a turn -- the common case for a coding agent -- was
silently never parsed at all (its lone write was mistaken for a baseline to
record rather than a change to check). `test_new_file_broken_on_first_write_is_flagged`
below is the regression test for that; the rest of this file's tests cover
the pre-existing-file half of the contract, which was already correct.
"""

from __future__ import annotations

import time
from pathlib import Path

from supersonic.verify.live_syntax_watch import LiveSyntaxWatcher


def _wait_until(predicate, timeout=3.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_pre_existing_broken_file_is_never_flagged(tmp_path: Path):
    bad = tmp_path / "already_broken.py"
    bad.write_text("def f(:\n    pass\n")  # broken before the watcher ever starts

    with LiveSyntaxWatcher(tmp_path, poll_interval=0.05) as watch:
        time.sleep(0.3)  # give it a few scan cycles with the file untouched
        findings = watch.latest_findings()

    assert findings == []


def test_new_file_broken_on_first_write_is_flagged(tmp_path: Path):
    # Regression test: a file that did NOT exist when the watcher started,
    # written exactly once with broken syntax -- the ordinary case for a
    # coding agent creating a new file during a turn -- must be caught on
    # that single write, not silently skipped as a "first sighting."
    target = tmp_path / "brand_new.py"

    with LiveSyntaxWatcher(tmp_path, poll_interval=0.05) as watch:
        time.sleep(0.1)  # watcher's synchronous baseline scan has already run; file doesn't exist yet
        target.write_text("def f(:\n    pass\n")
        found = _wait_until(lambda: bool(watch.latest_findings()))
        findings = watch.latest_findings()

    assert found is True
    assert len(findings) == 1
    assert findings[0].path == "brand_new.py"


def test_new_file_valid_on_first_write_is_not_flagged(tmp_path: Path):
    target = tmp_path / "brand_new_ok.py"

    with LiveSyntaxWatcher(tmp_path, poll_interval=0.05) as watch:
        time.sleep(0.1)
        target.write_text("def f():\n    return 1\n")
        time.sleep(0.3)
        findings = watch.latest_findings()

    assert findings == []


def test_file_broken_after_first_sighting_is_flagged(tmp_path: Path):
    target = tmp_path / "module.py"
    target.write_text("def f():\n    return 1\n")

    with LiveSyntaxWatcher(tmp_path, poll_interval=0.05) as watch:
        time.sleep(0.2)  # let the watcher register the first sighting
        target.write_text("def f(:\n    return 1\n")  # now break it
        found = _wait_until(lambda: bool(watch.latest_findings()))
        findings = watch.latest_findings()

    assert found is True
    assert len(findings) == 1
    assert findings[0].path == "module.py"
    assert findings[0].lineno >= 1


def test_valid_edit_after_first_sighting_is_not_flagged(tmp_path: Path):
    target = tmp_path / "module.py"
    target.write_text("def f():\n    return 1\n")

    with LiveSyntaxWatcher(tmp_path, poll_interval=0.05) as watch:
        time.sleep(0.2)
        target.write_text("def f():\n    return 2\n")  # still valid syntax
        time.sleep(0.3)
        findings = watch.latest_findings()

    assert findings == []


def test_ignored_directories_are_never_scanned(tmp_path: Path):
    venv_dir = tmp_path / ".venv" / "lib"
    venv_dir.mkdir(parents=True)
    bad = venv_dir / "broken.py"
    bad.write_text("def f():\n    return 1\n")

    with LiveSyntaxWatcher(tmp_path, poll_interval=0.05) as watch:
        time.sleep(0.2)
        bad.write_text("def f(:\n    return 1\n")
        time.sleep(0.3)
        findings = watch.latest_findings()

    assert findings == []


def test_dedupes_repeated_findings_for_same_file(tmp_path: Path):
    target = tmp_path / "module.py"
    target.write_text("def f():\n    return 1\n")

    with LiveSyntaxWatcher(tmp_path, poll_interval=0.05) as watch:
        time.sleep(0.2)
        target.write_text("def f(:\n    return 1\n")
        _wait_until(lambda: bool(watch.latest_findings()))
        target.write_text("def f(:\n    return 2\n")  # still broken, different line
        time.sleep(0.3)
        findings = watch.latest_findings()

    assert len(findings) == 1
