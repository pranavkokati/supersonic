"""Live Syntax Watch (verify/live_syntax_watch.py) — the concurrent
filesystem watcher that re-parses a touched .py file within a poll interval
of it being saved. Explicitly NOT a claim of write-syscall interception; see
the module docstring. These tests exercise the real contract: a file must be
observed changing at least once while the watcher is running before it can
ever be flagged (so a pre-existing broken file is never misreported), and a
genuine syntax error introduced after that first sighting is caught.
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
