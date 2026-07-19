"""QA signals — auto-detected test suite + lint/typecheck.

Two of the Verify gate's four independent signals. Both auto-detect the
right tool for the project (pytest/npm test, ruff/tsc) and degrade to
"not run" rather than erroring when nothing is detected — a turn is never
penalized for a signal that had no way to run.
"""

from __future__ import annotations

import json as _json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    ran: bool = False
    passed: bool = False
    command: str = ""
    output: str = ""
    failures: int = 0

    def to_context_block(self) -> str:
        if not self.ran:
            return f"## {self.name}\nNot run (no detected command)."
        status = "PASS" if self.passed else "FAIL"
        lines = [f"## {self.name} — {status}", f"`{self.command}`"]
        if self.output.strip():
            lines.append(f"```\n{self.output[-1500:]}\n```")
        return "\n".join(lines)


def _run(cmd: List[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def _detect_test_command(workdir: Path) -> Optional[List[str]]:
    has_py_tests = (workdir / "tests").exists() or list(workdir.glob("test_*.py"))
    if has_py_tests and shutil.which("pytest"):
        return ["pytest", "-q", "--maxfail=20"]
    pkg = workdir / "package.json"
    if pkg.exists() and shutil.which("npm"):
        try:
            data = _json.loads(pkg.read_text())
            if "test" in (data.get("scripts") or {}):
                return ["npm", "test", "--silent"]
        except (OSError, ValueError):
            pass
    return None


def run_tests(workdir: Path) -> CheckResult:
    cmd = _detect_test_command(workdir)
    if not cmd:
        return CheckResult(name="Tests", ran=False)
    try:
        res = _run(cmd, workdir)
    except subprocess.TimeoutExpired:
        return CheckResult(name="Tests", ran=True, passed=False, command=" ".join(cmd), output="Test run timed out.")
    except FileNotFoundError:
        return CheckResult(name="Tests", ran=False)
    output = (res.stdout or "") + "\n" + (res.stderr or "")
    failed = len(re.findall(r"\bFAILED\b", output))
    return CheckResult(
        name="Tests", ran=True, passed=res.returncode == 0, command=" ".join(cmd), output=output, failures=failed
    )


def _detect_lint_command(workdir: Path) -> Optional[List[str]]:
    if shutil.which("ruff") and any(workdir.rglob("*.py")):
        return ["ruff", "check", "."]
    pkg = workdir / "package.json"
    tsconfig = workdir / "tsconfig.json"
    if pkg.exists() and tsconfig.exists() and shutil.which("npx"):
        return ["npx", "--no-install", "tsc", "--noEmit"]
    return None


def run_lint(workdir: Path) -> CheckResult:
    cmd = _detect_lint_command(workdir)
    if not cmd:
        return CheckResult(name="Lint/typecheck", ran=False)
    try:
        res = _run(cmd, workdir, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return CheckResult(name="Lint/typecheck", ran=False)
    output = (res.stdout or "") + "\n" + (res.stderr or "")
    return CheckResult(name="Lint/typecheck", ran=True, passed=res.returncode == 0, command=" ".join(cmd), output=output)
