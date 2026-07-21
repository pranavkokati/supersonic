"""Docker-sandboxed coding-agent execution.

Why this exists: `runner.py::_run_streaming` and `pty_runner.py::run_in_pty`
both run the coding-agent CLI directly on the host machine, as a plain child
process of the Supersonic server. Checkpoint/rollback (`loop/rollback.py`)
protects the *git-tracked workdir* — a hard `git reset --hard` plus
`git clean -fd` undoes anything the agent changed inside that one directory.
It protects nothing else. A coding-agent CLI is an LLM-driven shell-and-file
tool with a real shell at its disposal; there is nothing stopping it from
running a command that reads, writes, or deletes anything else the host OS
user can reach — `rm -rf ~`, credentials in `~/.aws`, another project's
directory, `/etc/hosts`, whatever `os.environ` exposes to it. Checkpoint
rollback would restore the workdir and have nothing to say about the rest.
That is the real gap this module closes.

## What this actually does

Runs the exact same `cmd` list `runner.py` already builds (e.g.
`["claude", "-p", text, "--dangerously-skip-permissions"]`) inside a Docker
container instead of directly on the host, with:

  - Only the project's `workdir` bind-mounted in (`-v workdir:/workspace`,
    `-w /workspace`) — nothing else from the host filesystem is reachable
    from inside the container.
  - `--cap-drop ALL --security-opt no-new-privileges` — the container gets
    no Linux capabilities beyond the unprivileged default and cannot
    escalate.
  - `--pids-limit`, `--memory`, `--cpus` caps, so a runaway fork-bomb or
    infinite-loop turn can't consume the whole host.
  - A unique `--name`, so a timed-out run can be force-stopped
    server-side (`docker kill <name>`) even though killing the local
    `docker run` client process does not reliably stop the container --
    SIGKILL sent to the client is not proxied to the daemon-side container
    the way SIGTERM/SIGINT are, so relying on `proc.kill()` alone would
    leak a still-running orphaned container on every timeout.
  - `--rm` — the container's own filesystem (everything except the
    mounted workdir) is thrown away the instant it exits, successful or
    not.

## What this deliberately does NOT do

  - **No network isolation.** The container runs with Docker's default
    bridge network, with full outbound internet access. The coding agent
    still needs to reach its own LLM provider's API (Anthropic, OpenAI,
    etc.) — sandboxing that away would break the agent's basic function,
    not just its blast radius. A concrete consequence: a malicious or
    badly-hallucinating agent can still exfiltrate data or hit unexpected
    endpoints over the network from inside the container. Real network
    containment (an egress allowlist/proxy) is a harder, separate problem
    this feature does not attempt to solve.
  - **Not zero-config.** Unlike `dle_pty_supervision` (which needs nothing
    beyond the stdlib), this needs a real Docker image built or pulled
    ahead of time, named in `docker_sandbox_image` (Settings). See
    `docker/sandbox.Dockerfile` in this repo for a reference image with
    Claude Code, Codex, and Aider preinstalled — the three coding agents
    whose install commands are confirmed here. OpenCode and Cursor Agent
    are NOT preinstalled in that reference image (their exact package
    install commands aren't confirmed with the same confidence); using
    either of those agents under sandboxing requires customizing the
    image yourself.
  - **Not independently end-to-end verified against a real Docker daemon
    in this codebase's own CI/dev environment as of this writing** —
    `docker_available()`, the exact `docker run` argv construction, and
    the fallback-when-unavailable path are covered by tests that mock
    `subprocess`; an actual container boot-and-run was not exercised
    because no Docker daemon was reachable in the sandbox this code was
    written in. Treat the container-execution path itself as reviewed and
    carefully constructed, not as "observed working," until run once for
    real by anyone with a Docker install.

If `docker_sandbox_image` is empty, or `docker` isn't on PATH, or the
daemon doesn't respond, this mode no-ops back to the existing PTY/plain-
subprocess path in `runner.py` — exactly the same "never fail a turn over
an optional execution-mode dependency" rule `dle_pty_supervision` already
follows, and for the same reason.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

LineCallback = Callable[[str], None]
LineMapper = Callable[[str], Optional[str]]

CONTAINER_WORKDIR = "/workspace"


class DockerUnavailableError(RuntimeError):
    """Raised when Docker sandboxing is requested but can't actually run:
    binary missing, daemon unreachable, or no image configured."""


def docker_available() -> bool:
    """Best-effort, cheap check: is `docker` on PATH, and does the daemon
    actually answer? Not cached — a Docker daemon can be started or
    stopped on a dev machine between turns, and this is called at most
    once per turn, so the cost of re-checking is negligible."""
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5, text=True,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _build_docker_argv(
    cmd: List[str],
    workdir: Path,
    env: Dict[str, str],
    image: str,
    container_name: str,
    memory_limit: str,
    cpu_limit: str,
    pids_limit: int,
) -> List[str]:
    argv = [
        "docker", "run", "--rm",
        "--name", container_name,
        "-v", f"{Path(workdir).resolve()}:{CONTAINER_WORKDIR}",
        "-w", CONTAINER_WORKDIR,
        "--memory", memory_limit,
        "--cpus", cpu_limit,
        "--pids-limit", str(pids_limit),
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
    ]
    for key, value in env.items():
        argv += ["-e", f"{key}={value}"]
    argv += [image, *cmd]
    return argv


def run_in_docker(
    cmd: List[str],
    workdir: Path,
    env: Dict[str, str],
    image: str,
    on_line: Optional[LineCallback] = None,
    timeout: int = 1800,
    line_mapper: Optional[LineMapper] = None,
    memory_limit: str = "2g",
    cpu_limit: str = "2",
    pids_limit: int = 256,
):
    """Run `cmd` inside a throwaway Docker container with only `workdir`
    mounted in. Mirrors `runner._run_streaming`'s contract (same
    `on_line`/`line_mapper` semantics, same `AgentResult` shape) so all
    three execution paths (plain subprocess, PTY, Docker) are
    interchangeable from `CodingAgentRunner.run()`'s point of view.
    Imported lazily to avoid a circular import with `runner.py`.
    """
    from supersonic.agents.runner import AgentResult  # local import: avoids a runner<->sandbox_runner cycle

    if not image.strip():
        raise DockerUnavailableError("no docker_sandbox_image configured")
    if not docker_available():
        raise DockerUnavailableError("docker CLI/daemon not available")

    agent = cmd[0]
    container_name = f"sonic-sandbox-{uuid.uuid4().hex[:12]}"
    docker_cmd = _build_docker_argv(
        cmd, workdir, env, image, container_name, memory_limit, cpu_limit, pids_limit,
    )

    lines: List[str] = []
    try:
        proc = subprocess.Popen(
            docker_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except FileNotFoundError:
        raise DockerUnavailableError("docker binary disappeared between the availability check and the run")

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            out_line = line_mapper(line) if line_mapper else line
            if out_line is None:
                continue
            lines.append(out_line)
            if on_line:
                on_line(out_line)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Killing the local `docker run` client process (SIGKILL) is not
        # reliably proxied to the daemon-side container the way SIGTERM/
        # SIGINT would be from a foreground signal -- so the container
        # itself could otherwise keep running, orphaned, after --rm never
        # gets a chance to fire. Stop it explicitly by name instead.
        proc.kill()
        subprocess.run(["docker", "kill", container_name], capture_output=True, timeout=10, check=False)
        t.join(timeout=2)
        return AgentResult(
            agent=agent, success=False,
            output=f"Agent timed out after {timeout}s (sandboxed container {container_name} force-stopped).",
            command=" ".join(cmd),
        )
    t.join(timeout=2)
    out = "\n".join(lines)[-12000:]
    return AgentResult(agent=agent, success=proc.returncode == 0, output=out, command=" ".join(cmd))
