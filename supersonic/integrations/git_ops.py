"""Native git operations — commit/push/branch, no middleman.

This replaces the previous generation's dependency on Composio for shipping:
plain git plus the `gh` CLI (see github.py) covers everything a solo builder
needs, and almost every developer already has both authenticated.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from supersonic.loop.checkpoint import run_git

logger = logging.getLogger(__name__)


def has_remote(workdir: Path) -> bool:
    res = run_git(["remote"], workdir, check=False)
    return bool(res.stdout.strip())


def remote_url(workdir: Path, name: str = "origin") -> Optional[str]:
    res = run_git(["remote", "get-url", name], workdir, check=False)
    return res.stdout.strip() or None


def add_remote(workdir: Path, url: str, name: str = "origin") -> None:
    if has_remote(workdir):
        run_git(["remote", "set-url", name, url], workdir, check=False)
    else:
        run_git(["remote", "add", name, url], workdir)


def current_branch(workdir: Path) -> str:
    res = run_git(["rev-parse", "--abbrev-ref", "HEAD"], workdir, check=False)
    return res.stdout.strip() or "main"


def commit_all(workdir: Path, message: str) -> bool:
    run_git(["add", "-A"], workdir)
    res = run_git(["commit", "-q", "-m", message[:200]], workdir, check=False)
    return res.returncode == 0


def push(workdir: Path, branch: Optional[str] = None, *, set_upstream: bool = True) -> bool:
    if not has_remote(workdir):
        return False
    branch = branch or current_branch(workdir)
    args = ["push"] + (["-u", "origin", branch] if set_upstream else ["origin", branch])
    res = run_git(args, workdir, check=False)
    if res.returncode != 0:
        logger.warning("git push failed: %s", res.stderr.strip()[:300])
    return res.returncode == 0
