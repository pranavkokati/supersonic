"""PTY-native execution (agents/pty_runner.py) and its wiring into
CodingAgentRunner.run() via the `dle_pty_supervision` toggle.

Honest scope covered by these tests: a PTY governs stdin/stdout, not
filesystem writes — see the module docstring. What we verify here is the
actual, narrower contract: `run_in_pty` runs a real child process end to end
(exit code, streamed output, timeout/kill path) and `CodingAgentRunner.run`
only takes the PTY path when the toggle is on, falling back to the plain
subprocess path on any PTY failure or when unavailable.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from supersonic.agents import pty_runner
from supersonic.agents.pty_runner import PTYUnavailableError, run_in_pty
from supersonic.agents.runner import CodingAgentRunner
from supersonic.config import UserSecrets


pytestmark = pytest.mark.skipif(not pty_runner.PTY_AVAILABLE, reason="PTY mode is POSIX-only (stdlib `pty` module)")


def test_run_in_pty_captures_stdout_and_success_exit_code(tmp_path: Path):
    lines = []
    result = run_in_pty(
        [sys.executable, "-c", "print('hello from pty')"],
        workdir=tmp_path,
        env={},
        on_line=lines.append,
        timeout=10,
    )
    assert result.success is True
    assert "hello from pty" in result.output
    assert "hello from pty" in lines


def test_run_in_pty_reports_nonzero_exit_code_as_failure(tmp_path: Path):
    result = run_in_pty(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        workdir=tmp_path,
        env={},
        timeout=10,
    )
    assert result.success is False


def test_run_in_pty_missing_binary_does_not_hang(tmp_path: Path):
    result = run_in_pty(
        ["definitely-not-a-real-binary-xyz"],
        workdir=tmp_path,
        env={},
        timeout=10,
    )
    assert result.success is False


def test_run_in_pty_kills_on_timeout(tmp_path: Path):
    result = run_in_pty(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        workdir=tmp_path,
        env={},
        timeout=1,
    )
    assert result.success is False
    assert "timed out" in result.output.lower()


def test_run_in_pty_line_mapper_can_filter_lines(tmp_path: Path):
    lines = []
    result = run_in_pty(
        [sys.executable, "-c", "print('keep'); print('drop')"],
        workdir=tmp_path,
        env={},
        on_line=lines.append,
        line_mapper=lambda text: text if text == "keep" else None,
        timeout=10,
    )
    assert lines == ["keep"]
    assert result.success is True


class _FakeAgentResult:
    def __init__(self, success):
        self.success = success


def test_runner_uses_pty_path_when_toggle_enabled(tmp_path: Path):
    secrets = UserSecrets(dle_pty_supervision=True, default_agent="claude")
    runner = CodingAgentRunner("claude", secrets)
    called = {}

    def fake_run_in_pty(cmd, workdir, env, **kwargs):
        called["used_pty"] = True
        from supersonic.agents.runner import AgentResult
        return AgentResult(agent="claude", success=True, output="pty output", command=" ".join(cmd))

    with patch("supersonic.agents.pty_runner.run_in_pty", side_effect=fake_run_in_pty):
        result = runner.run("do the thing", tmp_path)

    assert called.get("used_pty") is True
    assert result.success is True
    assert result.output == "pty output"


def test_runner_falls_back_to_subprocess_when_toggle_disabled(tmp_path: Path):
    secrets = UserSecrets(dle_pty_supervision=False, default_agent="claude")
    runner = CodingAgentRunner("claude", secrets)

    with patch("supersonic.agents.pty_runner.run_in_pty") as mock_pty, \
         patch("supersonic.agents.runner._run_streaming") as mock_stream:
        from supersonic.agents.runner import AgentResult
        mock_stream.return_value = AgentResult(agent="claude", success=True, output="", command="")
        runner.run("do the thing", tmp_path)

    mock_pty.assert_not_called()
    mock_stream.assert_called_once()


def test_runner_falls_back_to_subprocess_when_pty_raises(tmp_path: Path):
    secrets = UserSecrets(dle_pty_supervision=True, default_agent="claude")
    runner = CodingAgentRunner("claude", secrets)

    with patch("supersonic.agents.pty_runner.run_in_pty", side_effect=PTYUnavailableError("no pty here")), \
         patch("supersonic.agents.runner._run_streaming") as mock_stream:
        from supersonic.agents.runner import AgentResult
        mock_stream.return_value = AgentResult(agent="claude", success=True, output="", command="")
        runner.run("do the thing", tmp_path)

    mock_stream.assert_called_once()


def test_runner_falls_back_to_subprocess_on_unexpected_pty_exception(tmp_path: Path):
    secrets = UserSecrets(dle_pty_supervision=True, default_agent="claude")
    runner = CodingAgentRunner("claude", secrets)

    with patch("supersonic.agents.pty_runner.run_in_pty", side_effect=RuntimeError("boom")), \
         patch("supersonic.agents.runner._run_streaming") as mock_stream:
        from supersonic.agents.runner import AgentResult
        mock_stream.return_value = AgentResult(agent="claude", success=True, output="", command="")
        runner.run("do the thing", tmp_path)

    mock_stream.assert_called_once()
