"""Dependency Trust Gate — catches hallucinated / newly-registered packages
before a turn ships, not after.

## Why this exists

Coding agents recommend package names with real confidence even when the
package does not exist. A 2025 USENIX Security study measured roughly a 20%
hallucination rate for open-source models and about 5% for commercial models
across generated install statements, and found that ~43% of hallucinated
names repeat consistently across repeated queries — which is exactly what
makes them a viable attack: a hallucinated name is *predictable* enough for
an attacker to pre-register on the real registry, ship malware under it, and
wait for an agent (any agent, not just this one) to install it. This is
called "slopsquatting," and by mid-2026 it had moved from a research finding
to an actively exploited vector, with malicious packages under hallucinated
names accumulating tens of thousands of downloads before takedown. None of
the mainstream coding-agent products (Cursor, Claude Code, Codex, Copilot,
Devin, OpenHands) or the enterprise agent-orchestration layer Supersonic was
forked from verify package existence before code ships — the install just
happens, and whatever ran, ran.

## What this module actually does, and what it can't

Supersonic does not sandbox or intercept the coding agent's own shell
commands — the agent CLI (Claude Code, Codex, etc.) runs as its own process
with its own tool-use loop, and by the time this module sees anything, a
`pip install`/`npm install` the agent issued mid-turn has already executed.
That is an intentional, documented architectural limit, not an oversight: it
matches the same "bring your own CLI, don't sandbox it" model as the rest of
Supersonic (see `agents/runner.py`).

So this gate cannot stop a malicious install from executing once, inside a
single turn's working tree. What it *can* do, and what nothing else in this
space does: read the turn's diff for every newly-added dependency (manifest
entries in requirements.txt/pyproject.toml/Pipfile/package.json, plus raw
`pip install`/`npm install`/`poetry add`/`yarn add`/`pnpm add` command lines
anywhere in the diff), check each one against the *real* registry (PyPI or
npm), and treat a package that does not exist there as an automatic Verify
gate failure — same severity as a syntax error. The turn is never
checkpointed, never shipped, never pushed to GitHub, and the agent gets one
corrective re-prompt naming the exact bad package before the turn is
abandoned. A newly-registered-but-real package (published very recently,
very few releases) is flagged as suspicious in the Review Brief rather than
hard-failed, since a young but legitimate package is a real, non-malicious
possibility and hard-failing on it would punish normal ecosystem churn.

## Scoring, in full

Per candidate package name + ecosystem:
  - **Registry lookup fails outright** (404) -> `nonexistent`. Near-certain
    hallucination — the agent invented a name that isn't real. This is the
    one verdict that fails the turn.
  - **Exists, but first published < 60 days ago AND <= 2 releases total**
    -> `suspicious`. Could be a legitimate new package; could be a
    freshly-registered slopsquat target. Surfaced, not blocked.
  - **Exists, older or more established** -> `trusted`. No finding surfaced.
  - **Registry unreachable** (network/timeout/non-2xx-non-404) -> excluded
    entirely. A sandbox with no internet must never manufacture a false
    "hallucinated package" finding just because it couldn't check — same
    "adapts down on missing evidence" rule as every other Verify signal.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import httpx
except ImportError:  # pragma: no cover - httpx is a core dependency; defensive only
    httpx = None  # type: ignore[assignment]

# A young package needs both conditions to read as "suspicious" — age alone
# punishes normal new-package churn, release count alone punishes packages
# that simply don't ship often.
_SUSPICIOUS_MAX_AGE_DAYS = 60
_SUSPICIOUS_MAX_RELEASES = 2

_DIFF_FILE_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)

_PY_KV_RE = re.compile(r'^\s*([A-Za-z][A-Za-z0-9._-]*)\s*=\s*["\']?[\^~><=0-9*][^\n]*$')
_PY_QUOTED_RE = re.compile(r'^\s*["\']([A-Za-z][A-Za-z0-9._-]*)\s*(?:[=<>!~^][^"\']*)?["\']\s*,?\s*$')
_NPM_JSON_KV_RE = re.compile(r'^\s*"(@?[A-Za-z0-9][\w.\-/]*)"\s*:\s*"[\^~]?[0-9][^"]*"\s*,?\s*$')

_INSTALL_CMD_PATTERNS = {
    "pypi": [
        re.compile(r"\bpip3?\s+install\s+([^\n#|;&]*)"),
        re.compile(r"\bpoetry\s+add\s+([^\n#|;&]*)"),
        re.compile(r"\bpipenv\s+install\s+([^\n#|;&]*)"),
    ],
    "npm": [
        re.compile(r"\bnpm\s+(?:install|i)\s+([^\n#|;&]*)"),
        re.compile(r"\byarn\s+add\s+([^\n#|;&]*)"),
        re.compile(r"\bpnpm\s+add\s+([^\n#|;&]*)"),
    ],
}


@dataclass
class PackageFinding:
    name: str
    ecosystem: str  # "pypi" | "npm"
    manifest: str  # file path it was found in, or "install command"
    verdict: str  # "nonexistent" | "suspicious" | "trusted"
    reason: str = ""
    age_days: Optional[int] = None
    release_count: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ecosystem": self.ecosystem,
            "manifest": self.manifest,
            "verdict": self.verdict,
            "reason": self.reason,
            "age_days": self.age_days,
            "release_count": self.release_count,
        }


@dataclass
class DependencyTrustVerdict:
    ran: bool = False
    ok: bool = True
    critical: List[PackageFinding] = field(default_factory=list)
    suspicious: List[PackageFinding] = field(default_factory=list)
    trusted: List[PackageFinding] = field(default_factory=list)
    unresolved: List[str] = field(default_factory=list)
    reprompt: str = ""

    def to_dict(self) -> dict:
        return {
            "ran": self.ran,
            "ok": self.ok,
            "critical": [f.to_dict() for f in self.critical],
            "suspicious": [f.to_dict() for f in self.suspicious],
            "trusted_count": len(self.trusted),
            "unresolved": self.unresolved,
        }

    def to_context_block(self) -> str:
        if not self.ran:
            return "## Dependency Trust Gate\nNot run (no new dependencies detected in this turn)."
        if self.ok and not self.suspicious:
            return f"## Dependency Trust Gate — PASS ({len(self.trusted)} package(s) verified)"
        lines = [f"## Dependency Trust Gate — {'FAIL' if not self.ok else 'PASS with warnings'}"]
        for f in self.critical:
            lines.append(f"- [NONEXISTENT] {f.name} ({f.ecosystem}, {f.manifest}): {f.reason}")
        for f in self.suspicious:
            lines.append(f"- [SUSPICIOUS] {f.name} ({f.ecosystem}, {f.manifest}): {f.reason}")
        if self.unresolved:
            lines.append(f"- (could not verify {len(self.unresolved)} package(s) — registry unreachable, not held against the turn)")
        return "\n".join(lines)


def _changed_blocks(diff: str) -> Dict[str, str]:
    if not diff.strip():
        return {}
    matches = list(_DIFF_FILE_HEADER_RE.finditer(diff))
    blocks: Dict[str, str] = {}
    for idx, m in enumerate(matches):
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(diff)
        path = m.group(2) or m.group(1)
        blocks[path] = diff[start:end]
    return blocks


def _added_lines(block: str) -> List[str]:
    return [line[1:] for line in block.splitlines() if line.startswith("+") and not line.startswith("+++")]


def _extract_python_packages(filename: str, added_lines: List[str]) -> List[str]:
    base = Path(filename).name.lower()
    names: List[str] = []
    if base.startswith("requirements") and base.endswith(".txt"):
        for line in added_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith(("-e ", "-r ", "--")):
                continue
            if "://" in stripped:
                continue
            m = re.match(r"^([A-Za-z][A-Za-z0-9._-]*)", stripped)
            if m:
                names.append(m.group(1))
    elif base in ("pyproject.toml", "pipfile"):
        for line in added_lines:
            stripped = line.strip().rstrip(",")
            m = _PY_KV_RE.match(stripped)
            if m:
                names.append(m.group(1))
                continue
            m = _PY_QUOTED_RE.match(stripped)
            if m:
                names.append(m.group(1))
    return names


def _extract_npm_packages(filename: str, added_lines: List[str]) -> List[str]:
    if Path(filename).name.lower() != "package.json":
        return []
    names = []
    for line in added_lines:
        m = _NPM_JSON_KV_RE.match(line.rstrip().rstrip(","))
        if m:
            names.append(m.group(1))
    return names


def _tokenize_install_args(argstr: str) -> List[str]:
    out = []
    for tok in argstr.split():
        if tok.startswith("-"):
            continue
        name = re.split(r"[=@<>!~^]", tok, maxsplit=1)[0].strip()
        if name and re.match(r"^[A-Za-z0-9][\w.\-]*$", name):
            out.append(name)
    return out


def _extract_install_command_packages(diff: str) -> Dict[str, List[str]]:
    """Lower-confidence signal than a manifest entry: scans every added line
    in the whole diff (Dockerfiles, CI workflows, bootstrap scripts, not just
    manifests) for install-command syntax."""
    added_all = [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
    found: Dict[str, List[str]] = {"pypi": [], "npm": []}
    for line in added_all:
        for eco, patterns in _INSTALL_CMD_PATTERNS.items():
            for pattern in patterns:
                for m in pattern.finditer(line):
                    found[eco].extend(_tokenize_install_args(m.group(1)))
    return found


def _age_days(iso_str: Optional[str]) -> Optional[int]:
    if not iso_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(iso_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0, (datetime.now(timezone.utc) - dt).days)
        except ValueError:
            continue
    return None


def _npm_url(name: str) -> str:
    if name.startswith("@") and "/" in name:
        scope, _, rest = name.partition("/")
        return f"https://registry.npmjs.org/{scope}%2F{rest}"
    return f"https://registry.npmjs.org/{name}"


def _check_pypi(name: str, client: "httpx.Client") -> Optional[PackageFinding]:
    try:
        resp = client.get(f"https://pypi.org/pypi/{name}/json")
    except Exception:
        return None
    if resp.status_code == 404:
        return PackageFinding(
            name=name, ecosystem="pypi", manifest="", verdict="nonexistent",
            reason=f"'{name}' does not exist on PyPI — likely a hallucinated package name.",
        )
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    releases = data.get("releases") or {}
    upload_times = [
        f.get("upload_time_iso_8601") for files in releases.values() for f in files if f.get("upload_time_iso_8601")
    ]
    age = _age_days(min(upload_times)) if upload_times else None
    release_count = len(releases)
    if age is not None and age < _SUSPICIOUS_MAX_AGE_DAYS and release_count <= _SUSPICIOUS_MAX_RELEASES:
        return PackageFinding(
            name=name, ecosystem="pypi", manifest="", verdict="suspicious",
            reason=f"published {age} day(s) ago with only {release_count} release(s) on PyPI — unusually new for a dependency an agent chose confidently.",
            age_days=age, release_count=release_count,
        )
    return PackageFinding(name=name, ecosystem="pypi", manifest="", verdict="trusted", age_days=age, release_count=release_count)


def _check_npm(name: str, client: "httpx.Client") -> Optional[PackageFinding]:
    try:
        resp = client.get(_npm_url(name))
    except Exception:
        return None
    if resp.status_code == 404:
        return PackageFinding(
            name=name, ecosystem="npm", manifest="", verdict="nonexistent",
            reason=f"'{name}' does not exist on the npm registry — likely a hallucinated package name.",
        )
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    versions = data.get("versions") or {}
    created = (data.get("time") or {}).get("created")
    age = _age_days(created)
    release_count = len(versions)
    if age is not None and age < _SUSPICIOUS_MAX_AGE_DAYS and release_count <= _SUSPICIOUS_MAX_RELEASES:
        return PackageFinding(
            name=name, ecosystem="npm", manifest="", verdict="suspicious",
            reason=f"published {age} day(s) ago with only {release_count} release(s) on npm — unusually new for a dependency an agent chose confidently.",
            age_days=age, release_count=release_count,
        )
    return PackageFinding(name=name, ecosystem="npm", manifest="", verdict="trusted", age_days=age, release_count=release_count)


def _collect_candidates(diff: str) -> List[Tuple[str, str, str]]:
    """Returns [(name, ecosystem, manifest_source), ...], not yet de-duped."""
    candidates: List[Tuple[str, str, str]] = []
    for path, block in _changed_blocks(diff).items():
        added = _added_lines(block)
        for name in _extract_python_packages(path, added):
            candidates.append((name, "pypi", path))
        for name in _extract_npm_packages(path, added):
            candidates.append((name, "npm", path))
    install_cmd = _extract_install_command_packages(diff)
    for name in install_cmd["pypi"]:
        candidates.append((name, "pypi", "install command"))
    for name in install_cmd["npm"]:
        candidates.append((name, "npm", "install command"))
    return candidates


def run_dependency_trust(
    workdir: Path, diff: str, timeout_per_request: float = 4.0, max_workers: int = 8,
) -> DependencyTrustVerdict:
    """Top-level entry point. Cheap to call on every turn — it's a no-op
    (`ran=False`) unless the diff actually touches a dependency manifest or
    contains an install-command line, and every registry call degrades to
    "unresolved" rather than raising if the network isn't there."""
    del workdir  # not needed today; kept for signature symmetry with the other pre-flight gates
    if httpx is None or not diff.strip():
        return DependencyTrustVerdict(ran=False)

    candidates = _collect_candidates(diff)
    if not candidates:
        return DependencyTrustVerdict(ran=False)

    unique: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for name, eco, manifest in candidates:
        key = (name.lower(), eco)
        if key not in unique:
            unique[key] = (name, manifest)

    findings: List[PackageFinding] = []
    unresolved: List[str] = []
    try:
        with httpx.Client(timeout=timeout_per_request, headers={"User-Agent": "supersonic-dependency-trust-gate/1.0"}) as client:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {}
                for (_, eco), (name, manifest) in unique.items():
                    fn = _check_pypi if eco == "pypi" else _check_npm
                    futures[pool.submit(fn, name, client)] = (name, manifest)
                for fut in as_completed(futures, timeout=timeout_per_request * 4):
                    name, manifest = futures[fut]
                    try:
                        result = fut.result()
                    except Exception:
                        result = None
                    if result is None:
                        unresolved.append(name)
                        continue
                    result.manifest = manifest
                    findings.append(result)
    except Exception:
        logger.exception("dependency trust gate failed unexpectedly, skipping this signal")
        return DependencyTrustVerdict(ran=False)

    critical = [f for f in findings if f.verdict == "nonexistent"]
    suspicious = [f for f in findings if f.verdict == "suspicious"]
    trusted = [f for f in findings if f.verdict == "trusted"]
    ok = not critical

    reprompt = ""
    if critical:
        lines = [
            "## Dependency Trust Gate caught a package that does not exist — fix ONLY this, before anything else.",
            "The package name(s) below could not be found on the real package registry. This is the exact",
            "pattern behind 'slopsquatting' supply-chain attacks: an attacker pre-registers a hallucinated name",
            "and ships malware under it, betting an AI agent will install it with confidence. Replace each with",
            "the correct, real package name (or remove it if it was never actually needed):",
            "",
        ]
        for f in critical:
            lines.append(f"- `{f.name}` ({f.ecosystem}, found in {f.manifest}): {f.reason}")
        reprompt = "\n".join(lines)

    return DependencyTrustVerdict(
        ran=True, ok=ok, critical=critical, suspicious=suspicious, trusted=trusted,
        unresolved=unresolved, reprompt=reprompt,
    )
