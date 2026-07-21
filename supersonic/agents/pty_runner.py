"""PTY-native coding-agent execution.

Honest scope note, stated up front because it's easy to oversell this: a
pseudo-terminal (PTY) intercepts a child process's stdin/stdout stream — it
does NOT intercept filesystem write syscalls. Real projects that market "PTY
wrapping" for coding agents (Vibeyard's per-session node-pty + xterm.js
panes, ArgusBot's supervisor loop around Codex/Claude Code CLIs) use the PTY
for exactly what it actually does: give the child process a genuine
terminal so `isatty()` is true (many CLIs silently switch to a degraded,
non-interactive, non-colored output mode the instant stdout is a plain
pipe), stream output exactly as a human terminal would render it, and get
clean process-group control. Nothing that calls itself a PTY performs
"kernel-level interception of a file write before it hits disk" — that's a
different, much heavier mechanism (ptrace/seccomp syscall interception, or a
FUSE overlay filesystem) that this module does not implement and does not
claim to.

What this module actually adds over `subprocess.Popen` (see
`supersonic/agents/runner.py::_run_streaming`, which remains the default and
the fallback):
  - The child sees a real TTY, so a coding-agent CLI that special-cases
    "not attached to a terminal" (disabling interactive confirmations,
    progress bars, or ANSI coloring) behaves the same way it would if a
    human had typed the command directly.
  - Output streams byte-for-byte as the child wrote it, including any
    cursor-control / partial-line behavior a plain pipe would buffer
    differently.
  - Real process control: the child is `pty.fork()`ed (POSIX only — the
    `pty` module doesn't exist on Windows, so this mode simply isn't offered
    there; `PTY_AVAILABLE` reflects that), so it can be killed cleanly on
    timeout the same way `_run_streaming` already does for the plain-pipe
    path.

For actually catching a broken file the instant it's written *during* a
turn (independent of which of the two execution paths above ran the agent),
see `supersonic/verify/live_syntax_watch.py` — a concurrent filesystem
watcher, not something derived from the PTY.
"""

from __future__ import annotations

import errno
import logging
import os
import select
import signal
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import pty

    PTY_AVAILABLE = True
except ImportError:  # Windows — no `pty` module in the stdlib.
    pty = None  # type: ignore[assignment]
    PTY_AVAILABLE = False

LineCallback = Callable[[str], None]
LineMapper = Callable[[str], Optional[str]]


class PTYUnavailableError(RuntimeError):
    """Raised when PTY mode is requested on a platform without `pty` support."""


def run_in_pty(
    cmd: List[str],
    workdir: Path,
    env: Dict[str, str],
    on_line: Optional[LineCallback] = None,
    timeout: int = 1800,
    line_mapper: Optional[LineMapper] = None,
    read_chunk_size: int = 65536,
):
    """Run `cmd` inside a real pseudo-terminal instead of a plain pipe.

    Mirrors `supersonic.agents.runner._run_streaming`'s contract exactly
    (same `on_line`/`line_mapper` semantics, same AgentResult shape) so the
    two execution paths are interchangeable from the caller's point of view.
    Imported lazily inside the function to avoid a circular import between
    `runner.py` and this module.
    """
    from supersonic.agents.runner import AgentResult  # local import: avoids a runner<->pty_runner cycle

    if not PTY_AVAILABLE:
        raise PTYUnavailableError("PTY mode requires the stdlib `pty` module (POSIX only)")

    agent = cmd[0]
    full_env = {**os.environ, **env}

    try:
        pid, fd = pty.fork()
    except OSError as e:
        return AgentResult(agent=agent, success=False, output=f"pty.fork() failed: {e}", command=" ".join(cmd))

    if pid == 0:  # child process — never returns
        try:
            os.chdir(str(workdir))
            os.execvpe(cmd[0], cmd, full_env)
        except FileNotFoundError:
            os._exit(127)
        except Exception:
            os._exit(126)
        os._exit(1)  # pragma: no cover - unreachable, execvpe replaces the process on success

    # parent process
    buf = b""
    lines: List[str] = []
    deadline = time.monotonic() + timeout
    timed_out = False

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
        except OSError as e:
            if e.errno == errno.EBADF:
                break  # fd already closed — child exited between iterations
            raise
        if fd not in ready:
            continue
        try:
            chunk = os.read(fd, read_chunk_size)
        except OSError as e:
            if e.errno == errno.EIO:
                break  # PTY closed on the child side — normal end-of-output
            raise
        if not chunk:
            break
        while b"\n" in buf + chunk:
            combined = buf + chunk
            raw_line, chunk = combined.split(b"\n", 1)
            buf = b""
            text = raw_line.decode("utf-8", errors="replace").rstrip("\r")
            out_line = line_mapper(text) if line_mapper else text
            if out_line is not None:
                lines.append(out_line)
                if on_line:
                    on_line(out_line)
        buf += chunk

    if timed_out:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        return AgentResult(
            agent=agent, success=False, output=f"Agent timed out after {timeout}s.", command=" ".join(cmd)
        )

    if buf:
        text = buf.decode("utf-8", errors="replace").rstrip("\r")
        out_line = line_mapper(text) if line_mapper else text
        if out_line is not None:
            lines.append(out_line)
            if on_line:
                on_line(out_line)

    exit_code = 1
    try:
        _, status = os.waitpid(pid, 0)
        exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
    except ChildProcessError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass

    out = "\n".join(lines)[-12000:]
    return AgentResult(agent=agent, success=exit_code == 0, output=out, command=" ".join(cmd))
