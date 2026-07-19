"""QA signal detection — regression coverage for a real portability bug.

`shutil.which("pytest")` alone misses pytest in any environment where the
package is installed and fully usable but the console-script entry point
isn't on PATH (pipx-style isolated installs, some sandboxed/containerized
setups, certain venv activation flows — this repo's own CI sandbox hit
exactly this). That silently degraded the Tests signal, and Test Quality's
mutation re-runs which reuse the same detection, to "not run" with zero
error. These tests pin the fix: `python -m pytest` on the interpreter
actually running Supersonic is tried first, via `importlib.util.find_spec`,
before ever falling back to a PATH lookup.
"""

from __future__ import annotations

import sys

from supersonic.verify import qa


def test_pytest_command_prefers_module_invocation_over_path_lookup(monkeypatch):
    # Simulate the exact sandbox bug: pytest is fully importable (the
    # package is installed and usable) but nothing named "pytest" is on
    # PATH — the real condition that broke the Tests signal.
    monkeypatch.setattr(qa.shutil, "which", lambda name: None)
    cmd = qa._pytest_command()
    assert cmd == [sys.executable, "-m", "pytest", "-q", "--maxfail=20"]


def test_pytest_command_falls_back_to_path_when_not_importable(monkeypatch):
    monkeypatch.setattr(qa.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(qa.shutil, "which", lambda name: "/usr/bin/pytest" if name == "pytest" else None)
    cmd = qa._pytest_command()
    assert cmd == ["pytest", "-q", "--maxfail=20"]


def test_pytest_command_returns_none_when_pytest_unavailable_anywhere(monkeypatch):
    monkeypatch.setattr(qa.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(qa.shutil, "which", lambda name: None)
    assert qa._pytest_command() is None


def test_detect_test_command_finds_pytest_even_with_pytest_absent_from_path(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    monkeypatch.setattr(qa.shutil, "which", lambda name: None)

    cmd = qa._detect_test_command(tmp_path)
    assert cmd == [sys.executable, "-m", "pytest", "-q", "--maxfail=20"]


def test_run_tests_actually_runs_and_reports_a_real_failure_with_pytest_absent_from_path(tmp_path, monkeypatch):
    """End-to-end: even with `shutil.which("pytest")` returning None (the
    exact bug), a real failing test suite is still detected, actually run,
    and correctly reported as failed — not silently skipped."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_broken.py").write_text("def test_x():\n    assert 1 == 2\n")
    monkeypatch.setattr(qa.shutil, "which", lambda name: None)

    result = qa.run_tests(tmp_path)
    assert result.ran is True
    assert result.passed is False
    assert result.failures >= 1


def test_run_tests_reports_a_real_pass_with_pytest_absent_from_path(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_x():\n    assert 1 == 1\n")
    monkeypatch.setattr(qa.shutil, "which", lambda name: None)

    result = qa.run_tests(tmp_path)
    assert result.ran is True
    assert result.passed is True
