"""Docker Sandbox (agents/sandbox_runner.py) and its wiring into
CodingAgentRunner.run() via the `dle_docker_sandbox` toggle.

Honest scope covered here: this project's own dev/CI sandbox has no Docker
daemon reachable, so a real container boot-and-run is NOT exercised by any
test in this file (see the module docstring in sandbox_runner.py). What IS
covered with full confidence: availability detection, exact `docker run`
argv construction (mount, cap-drop, resource limits, unique --name), the
timeout -> `docker kill` cleanup path, and the three-tier fallback wiring
in CodingAgentRunner.run() (Docker -> PTY -> plain subprocess) — all via
mocked `subprocess` calls, the same technique test_pty_runner.py already
uses for the PTY-toggle wiring tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from supersonic.agents.runner import AgentResult, CodingAgentRunner
from supersonic.agents.sandbox_runner import (
    DockerUnavailableError,
    _build_docker_argv,
    docker_available,
    run_in_docker,
)
from supersonic.config import UserSecrets


# ---------------------------------------------------------------------------
# docker_available()
# ---------------------------------------------------------------------------

def test_docker_available_false_when_binary_missing():
    with patch("shutil.which", return_value=None):
        assert docker_available() is False


def test_docker_available_false_when_daemon_unreachable():
    with patch("shutil.which", return_value="/usr/bin/docker"), \
         patch("subprocess.run", return_value=MagicMock(returncode=1)):
        assert docker_available() is False


def test_docker_available_false_on_timeout():
    with patch("shutil.which", return_value="/usr/bin/docker"), \
         patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker info", timeout=5)):
        assert docker_available() is False


def test_docker_available_true_when_binary_present_and_daemon_ok():
    with patch("shutil.which", return_value="/usr/bin/docker"), \
         patch("subprocess.run", return_value=MagicMock(returncode=0)):
        assert docker_available() is True


# ---------------------------------------------------------------------------
# argv construction — this is the part that must be exactly right, since it
# can't be caught by an actual container run failing in this environment.
# ---------------------------------------------------------------------------

def test_build_docker_argv_mounts_only_the_workdir(tmp_path: Path):
    argv = _build_docker_argv(
        ["claude", "-p", "do it"], tmp_path, {}, "my-image:latest", "sonic-sandbox-abc123",
        "2g", "2", 256,
    )
    assert "-v" in argv
    mount_idx = argv.index("-v") + 1
    assert argv[mount_idx] == f"{tmp_path.resolve()}:/workspace"
    assert argv[argv.index("-w") + 1] == "/workspace"


def test_build_docker_argv_drops_capabilities_and_blocks_privilege_escalation():
    argv = _build_docker_argv(["claude"], Path("/tmp/x"), {}, "img", "name1", "2g", "2", 256)
    assert "--cap-drop" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in argv
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"


def test_build_docker_argv_applies_resource_limits():
    argv = _build_docker_argv(["claude"], Path("/tmp/x"), {}, "img", "name1", "3g", "4", 512)
    assert argv[argv.index("--memory") + 1] == "3g"
    assert argv[argv.index("--cpus") + 1] == "4"
    assert argv[argv.index("--pids-limit") + 1] == "512"


def test_build_docker_argv_uses_rm_and_the_given_unique_name():
    argv = _build_docker_argv(["claude"], Path("/tmp/x"), {}, "img", "sonic-sandbox-xyz", "2g", "2", 256)
    assert "--rm" in argv
    assert argv[argv.index("--name") + 1] == "sonic-sandbox-xyz"


def test_build_docker_argv_passes_env_vars_and_appends_cmd_after_image():
    argv = _build_docker_argv(
        ["claude", "-p", "hello"], Path("/tmp/x"), {"ANTHROPIC_API_KEY": "sk-test"}, "my-image", "n1", "2g", "2", 256,
    )
    assert "-e" in argv
    e_idx = argv.index("-e")
    assert argv[e_idx + 1] == "ANTHROPIC_API_KEY=sk-test"
    image_idx = argv.index("my-image")
    assert argv[image_idx + 1:] == ["claude", "-p", "hello"]


def test_run_in_docker_two_calls_get_different_container_names(tmp_path: Path):
    # Concurrent turns/runs must never collide on --name.
    names = []

    def fake_popen(argv, **kwargs):
        names.append(argv[argv.index("--name") + 1])
        proc = MagicMock()
        proc.stdout = iter([])
        proc.wait.return_value = 0
        proc.returncode = 0
        return proc

    with patch("supersonic.agents.sandbox_runner.docker_available", return_value=True), \
         patch("subprocess.Popen", side_effect=fake_popen):
        run_in_docker(["claude"], tmp_path, {}, image="img")
        run_in_docker(["claude"], tmp_path, {}, image="img")

    assert len(set(names)) == 2


# ---------------------------------------------------------------------------
# run_in_docker: unavailable / unconfigured -> raises, never hangs or fakes success
# ---------------------------------------------------------------------------

def test_run_in_docker_raises_when_no_image_configured(tmp_path: Path):
    try:
        run_in_docker(["claude"], tmp_path, {}, image="")
        assert False, "expected DockerUnavailableError"
    except DockerUnavailableError as e:
        assert "image" in str(e)


def test_run_in_docker_raises_when_daemon_unreachable(tmp_path: Path):
    with patch("supersonic.agents.sandbox_runner.docker_available", return_value=False):
        try:
            run_in_docker(["claude"], tmp_path, {}, image="some-image")
            assert False, "expected DockerUnavailableError"
        except DockerUnavailableError:
            pass


def test_run_in_docker_success_path_streams_output_and_reports_exit_code(tmp_path: Path):
    def fake_popen(argv, **kwargs):
        proc = MagicMock()
        proc.stdout = iter(["line one\n", "line two\n"])
        proc.wait.return_value = 0
        proc.returncode = 0
        return proc

    lines = []
    with patch("supersonic.agents.sandbox_runner.docker_available", return_value=True), \
         patch("subprocess.Popen", side_effect=fake_popen):
        result = run_in_docker(["claude", "-p", "hi"], tmp_path, {}, image="img", on_line=lines.append)

    assert result.success is True
    assert "line one" in result.output
    assert lines == ["line one", "line two"]


def test_run_in_docker_timeout_force_stops_the_named_container(tmp_path: Path):
    kill_calls = []

    def fake_popen(argv, **kwargs):
        proc = MagicMock()
        proc.stdout = iter([])
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd=argv, timeout=1)
        return proc

    def fake_run(argv, **kwargs):
        kill_calls.append(argv)
        return MagicMock(returncode=0)

    with patch("supersonic.agents.sandbox_runner.docker_available", return_value=True), \
         patch("subprocess.Popen", side_effect=fake_popen), \
         patch("subprocess.run", side_effect=fake_run):
        result = run_in_docker(["claude"], tmp_path, {}, image="img", timeout=1)

    assert result.success is False
    assert "timed out" in result.output.lower()
    assert any(c[:2] == ["docker", "kill"] for c in kill_calls)


# ---------------------------------------------------------------------------
# CodingAgentRunner.run() wiring: Docker takes priority over PTY, and both
# fall back to plain subprocess -- never fail the turn over sandbox infra.
# ---------------------------------------------------------------------------

def test_runner_uses_docker_path_when_toggle_enabled_and_image_configured(tmp_path: Path):
    secrets = UserSecrets(dle_docker_sandbox=True, docker_sandbox_image="my-sandbox:latest", default_agent="claude")
    runner = CodingAgentRunner("claude", secrets)

    def fake_run_in_docker(cmd, workdir, env, **kwargs):
        return AgentResult(agent="claude", success=True, output="docker output", command=" ".join(cmd))

    with patch("supersonic.agents.sandbox_runner.run_in_docker", side_effect=fake_run_in_docker) as mock_docker, \
         patch("supersonic.agents.pty_runner.run_in_pty") as mock_pty, \
         patch("supersonic.agents.runner._run_streaming") as mock_stream:
        result = runner.run("do the thing", tmp_path)

    mock_docker.assert_called_once()
    mock_pty.assert_not_called()
    mock_stream.assert_not_called()
    assert result.output == "docker output"


def test_runner_falls_back_to_subprocess_when_docker_toggle_disabled(tmp_path: Path):
    secrets = UserSecrets(dle_docker_sandbox=False, default_agent="claude")
    runner = CodingAgentRunner("claude", secrets)

    with patch("supersonic.agents.sandbox_runner.run_in_docker") as mock_docker, \
         patch("supersonic.agents.runner._run_streaming") as mock_stream:
        mock_stream.return_value = AgentResult(agent="claude", success=True, output="", command="")
        runner.run("do the thing", tmp_path)

    mock_docker.assert_not_called()
    mock_stream.assert_called_once()


def test_runner_falls_back_to_pty_when_docker_raises_unavailable(tmp_path: Path):
    secrets = UserSecrets(dle_docker_sandbox=True, docker_sandbox_image="", dle_pty_supervision=True, default_agent="claude")
    runner = CodingAgentRunner("claude", secrets)

    def fake_run_in_pty(cmd, workdir, env, **kwargs):
        return AgentResult(agent="claude", success=True, output="pty output", command=" ".join(cmd))

    with patch("supersonic.agents.sandbox_runner.run_in_docker", side_effect=DockerUnavailableError("no image")), \
         patch("supersonic.agents.pty_runner.run_in_pty", side_effect=fake_run_in_pty) as mock_pty:
        result = runner.run("do the thing", tmp_path)

    mock_pty.assert_called_once()
    assert result.output == "pty output"


def test_runner_falls_back_to_plain_subprocess_when_docker_raises_and_pty_disabled(tmp_path: Path):
    secrets = UserSecrets(dle_docker_sandbox=True, docker_sandbox_image="", dle_pty_supervision=False, default_agent="claude")
    runner = CodingAgentRunner("claude", secrets)

    with patch("supersonic.agents.sandbox_runner.run_in_docker", side_effect=DockerUnavailableError("no image")), \
         patch("supersonic.agents.runner._run_streaming") as mock_stream:
        mock_stream.return_value = AgentResult(agent="claude", success=True, output="", command="")
        runner.run("do the thing", tmp_path)

    mock_stream.assert_called_once()


def test_runner_falls_back_when_docker_raises_unexpected_exception(tmp_path: Path):
    secrets = UserSecrets(dle_docker_sandbox=True, docker_sandbox_image="img", default_agent="claude")
    runner = CodingAgentRunner("claude", secrets)

    with patch("supersonic.agents.sandbox_runner.run_in_docker", side_effect=RuntimeError("boom")), \
         patch("supersonic.agents.runner._run_streaming") as mock_stream:
        mock_stream.return_value = AgentResult(agent="claude", success=True, output="", command="")
        runner.run("do the thing", tmp_path)

    mock_stream.assert_called_once()
