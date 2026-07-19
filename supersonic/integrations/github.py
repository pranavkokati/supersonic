"""GitHub shipping — native `gh` CLI, no Composio middleman.

Every function here fails soft: if `gh` isn't installed or authenticated,
they return None/False and log a one-line instruction rather than raising,
so a run without GitHub access still completes and ships locally.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from supersonic.integrations import git_ops

logger = logging.getLogger(__name__)


def gh_available() -> bool:
    return shutil.which("gh") is not None


def _run_gh(args, cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def ensure_repo(workdir: Path, name: str, *, private: bool = True, description: str = "") -> Optional[str]:
    """Create (or reuse) a GitHub repo for this project and wire it as `origin`. Returns the repo URL, or None."""
    if not gh_available():
        logger.info("gh CLI not installed — skipping GitHub repo creation (https://cli.github.com)")
        return None
    if git_ops.has_remote(workdir):
        return git_ops.remote_url(workdir)

    auth = _run_gh(["auth", "status"], workdir)
    if auth.returncode != 0:
        logger.info("gh CLI not authenticated — run `gh auth login`. Skipping GitHub repo creation.")
        return None

    args = ["repo", "create", name, "--source", ".", "--remote", "origin", "--private" if private else "--public"]
    if description:
        args += ["--description", description[:350]]
    res = _run_gh(args, workdir, timeout=120)
    if res.returncode != 0:
        logger.warning("gh repo create failed: %s", res.stderr.strip()[:300])
        return None
    return git_ops.remote_url(workdir)


def ship(workdir: Path, *, mode: str = "pr", branch: Optional[str] = None, title: str = "", body: str = "") -> dict:
    """Push the current state. mode="pr" opens a pull request; mode="push" pushes straight to the default branch."""
    pushed = git_ops.push(workdir, branch)
    result = {"pushed": pushed, "url": None}
    if not pushed or not gh_available() or mode != "pr":
        return result

    args = ["pr", "create", "--fill"] if not title else ["pr", "create", "--title", title[:250], "--body", body[:4000]]
    res = _run_gh(args, workdir, timeout=60)
    if res.returncode == 0 and res.stdout.strip():
        result["url"] = res.stdout.strip().splitlines()[-1]
    elif res.returncode != 0:
        logger.info("gh pr create skipped/failed: %s", res.stderr.strip()[:200])
    return result
