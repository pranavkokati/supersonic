"""Live Syntax Watch — a concurrent filesystem watcher, not a PTY trick.

Syntax Shield (`syntax_shield.py`) checks the whole turn's diff for broken
syntax once the coding agent has finished running. That's correct but
necessarily late: on a long turn the agent might write ten more files after
breaking one, and Supersonic only finds out at the end.

This module runs a lightweight background thread *while the agent is still
writing files* — no kernel hooks, no interception of the write syscall
itself (nothing short of ptrace/seccomp or a FUSE overlay filesystem can do
that, and this project doesn't implement either): it polls each tracked
source file's mtime every `poll_interval` seconds, and the instant a file's
mtime changes, re-parses it with `ast.parse`. A parse failure is recorded
immediately, independent of whichever process actually ran the agent
(plain subprocess or the PTY path in `pty_runner.py`).

What it buys, honestly: visibility roughly `poll_interval` seconds after a
bad file is saved instead of only after the whole turn ends, and — via
`latest_findings()` — the exact file and line a corrective re-prompt should
name, computed before the turn's full diff is even available. It does not
pause or interrupt the agent process itself; that would require either
killing it outright (discarding whatever else it's mid-way through writing)
or a per-agent-specific way to inject a mid-stream correction, which isn't
something that generalizes safely across five different third-party CLIs.
Supersonic still runs the authoritative, diff-based Syntax Shield check
after the turn completes — this is a faster-feedback observability layer
in front of it, not a replacement for it.
"""

from __future__ import annotations

import ast
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

_IGNORED_DIR_NAMES = {
    "__pycache__", ".venv", "venv", "env", "node_modules", ".git", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".continuity", ".dle", ".supersonic",
}


@dataclass
class LiveSyntaxFinding:
    path: str  # workdir-relative
    error: str
    lineno: int


def _is_ignored(path: Path) -> bool:
    return any(part in _IGNORED_DIR_NAMES for part in path.parts)


class LiveSyntaxWatcher:
    """Use as a context manager around the coding agent's run:

        with LiveSyntaxWatcher(workdir) as watch:
            runner.run(prompt, workdir, on_line=...)
        findings = watch.latest_findings()
    """

    def __init__(self, workdir: Path, poll_interval: float = 0.25):
        self.workdir = Path(workdir)
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._findings: List[LiveSyntaxFinding] = []
        # None on first sighting of a path this run — we only flag a file
        # once we've observed it change AT LEAST once while watching, so a
        # pre-existing (already-broken, already-committed) file never gets
        # misreported as something the agent "just" wrote.
        self._seen_mtimes: Dict[Path, float] = {}

    def __enter__(self) -> "LiveSyntaxWatcher":
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def latest_findings(self) -> List[LiveSyntaxFinding]:
        with self._lock:
            return list(self._findings)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception:
                logger.exception("live syntax watch scan failed, continuing")
            self._stop.wait(self.poll_interval)

    def _scan_once(self) -> None:
        for path in self.workdir.rglob("*.py"):
            if _is_ignored(path.relative_to(self.workdir)):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            prev = self._seen_mtimes.get(path)
            self._seen_mtimes[path] = mtime
            if prev is None or prev == mtime:
                continue  # first sighting, or unchanged since last scan
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
                ast.parse(source)
            except SyntaxError as e:
                rel = str(path.relative_to(self.workdir))
                with self._lock:
                    if not any(f.path == rel for f in self._findings):
                        self._findings.append(LiveSyntaxFinding(path=rel, error=str(e), lineno=e.lineno or 0))
            except (OSError, UnicodeDecodeError, ValueError):
                continue
