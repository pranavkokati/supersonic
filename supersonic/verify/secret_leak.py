"""Secret Leak Gate — catches a hardcoded credential before a turn ships,
not after someone scrapes it off GitHub.

## Why this exists

GitGuardian's State of Secrets Sprawl 2026 report — built from scanning
billions of commits across public GitHub — found 29 million hardcoded
secrets exposed in 2025 alone, a 34% year-over-year increase and the
largest single-year jump on record. The same report is the first to isolate
AI involvement as a variable: AI-assisted commits leak a secret at roughly
3.2% of commits, versus roughly 1.6% for human-only commits — almost
exactly double. And once a secret leaks, it tends to stay dangerous: 64% of
secrets GitGuardian confirmed valid in 2022 were *still* exploitable when
they re-checked in January 2026, because nobody rotated them. An autonomous
loop that writes and ships code unattended is exactly the profile that
statistic describes, and nothing in this codebase checked for it before
this module — Review Risk's sensitive-path heuristic flags files that
*touch* auth/payment surfaces, it does not look for an actual credential
*value* sitting in the diff.

## What this module does

A pre-flight check, same family as Syntax Shield and the Dependency Trust
Gate: scan only the *added* lines of a turn's diff for the structural
signatures of a real credential. Two tiers:

  - **Critical** — a value matching a well-known, high-specificity secret
    format (AWS access key ID, a PEM private key block, a GitHub/Slack/
    Stripe/Anthropic/OpenAI/Google token prefix). These formats are
    specific enough that a match is a real secret, not a coincidence.
    Fails the turn outright, same severity as a syntax error or a
    hallucinated dependency — one corrective re-prompt naming the exact
    file and line, then rollback if it's still there.
  - **Suspicious** — a generic heuristic: a variable/key name that reads
    like a credential (`api_key`, `secret`, `token`, `password`, ...)
    assigned a long, high-entropy quoted string. This is the same
    technique tools like gitleaks and detect-secrets use for the
    catch-all case a fixed format can't cover, and it is much more
    false-positive-prone by nature (a genuinely random-looking test
    fixture ID will trip it) — so it's surfaced in the Review Brief, not
    blocked outright.

A turn adding or modifying a real `.env` file (not `.env.example`/
`.env.sample`/`.env.template`) is flagged on its own regardless of content,
since that file existing in a diff at all is a process problem independent
of whether this pass happened to catch a specific value inside it.

Known placeholder conventions (`your_api_key_here`, `xxxxxxxx`, `EXAMPLE`,
`dummy`, `changeme`, `REDACTED`, repeated `0`/`x` fill) are filtered out
before scoring — otherwise every `.env.example` template trips this gate on
every turn that touches one, which would train people to ignore it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

_DIFF_FILE_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)

_PLACEHOLDER_RE = re.compile(
    r"your[_-]?api[_-]?key|xxxx|00000000|11111111|example|dummy|changeme|redacted|"
    r"replace[_-]?me|<[a-z_-]+>|\{\{.*\}\}|placeholder|sample[_-]?key|fake[_-]?key|"
    r"test[_-]?only|not[_-]?a[_-]?real",
    re.IGNORECASE,
)

_ENV_FILE_RE = re.compile(r"(^|/)\.env$|(^|/)\.env\.[a-z]+$", re.IGNORECASE)
_ENV_EXAMPLE_RE = re.compile(r"\.env\.(example|sample|template|dist)$", re.IGNORECASE)

# High-specificity structured formats — a match is a real secret, not luck.
_CRITICAL_PATTERNS: List[tuple] = [
    ("AWS access key ID", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("PEM private key block", re.compile(r"-----BEGIN\s?(RSA|EC|OPENSSH|DSA|PGP)?\s?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("Slack webhook URL", re.compile(r"hooks\.slack\.com/services/T[0-9A-Za-z]+/B[0-9A-Za-z]+/[0-9A-Za-z]+")),
    ("Stripe live secret key", re.compile(r"\bsk_live_[0-9a-zA-Z]{16,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b")),
    ("OpenAI API key", re.compile(r"\bsk-[A-Za-z0-9]{20,}T3BlbkFJ[A-Za-z0-9]{20,}\b|\bsk-proj-[A-Za-z0-9\-_]{20,}\b")),
]

# A credential-shaped variable name holding a long opaque string. Much
# noisier than the structured patterns above by design — surfaced, not
# blocked. Requires both a suggestive name AND a value that looks opaque
# (long, no spaces, not obviously a placeholder) to keep the false-positive
# rate down to something worth a human's five minutes rather than every
# turn's worth of noise.
_GENERIC_SECRET_RE = re.compile(
    r"""["']?\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|
    client[_-]?secret|private[_-]?key|password|passwd)\b["']?\s*[:=]\s*
    ["']([A-Za-z0-9_\-/+=]{20,})["']""",
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class SecretFinding:
    path: str
    kind: str  # human-readable pattern name, e.g. "AWS access key ID"
    verdict: str  # "critical" | "suspicious" | "env_file"
    line_excerpt: str = ""

    def to_dict(self) -> dict:
        return {"path": self.path, "kind": self.kind, "verdict": self.verdict, "line_excerpt": self.line_excerpt}


@dataclass
class SecretLeakVerdict:
    ran: bool = False
    ok: bool = True
    critical: List[SecretFinding] = field(default_factory=list)
    suspicious: List[SecretFinding] = field(default_factory=list)
    reprompt: str = ""

    def to_dict(self) -> dict:
        return {
            "ran": self.ran,
            "ok": self.ok,
            "critical": [f.to_dict() for f in self.critical],
            "suspicious": [f.to_dict() for f in self.suspicious],
        }

    def to_context_block(self) -> str:
        if not self.ran:
            return "## Secret Leak Gate\nNot run (no added lines matched a credential shape)."
        if self.ok and not self.suspicious:
            return "## Secret Leak Gate — PASS (no credential patterns detected)"
        lines = [f"## Secret Leak Gate — {'FAIL' if not self.ok else 'PASS with warnings'}"]
        for f in self.critical:
            lines.append(f"- [CRITICAL] {f.path}: {f.kind} — `{f.line_excerpt}`")
        for f in self.suspicious:
            lines.append(f"- [SUSPICIOUS] {f.path}: {f.kind} — `{f.line_excerpt}`")
        return "\n".join(lines)


def _redact(value: str, kind: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-2:]} ({kind})"


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


def _is_placeholder(text: str) -> bool:
    if _PLACEHOLDER_RE.search(text):
        return True
    # repeated-character fill (aaaaaaaa, 0000000000, xxxxxxxxxx) is always a stub
    stripped = re.sub(r"[^A-Za-z0-9]", "", text)
    if stripped and len(set(stripped.lower())) <= 2:
        return True
    return False


def scan_file(path: str, added_lines: List[str]) -> List[SecretFinding]:
    findings: List[SecretFinding] = []

    if _ENV_FILE_RE.search(path) and not _ENV_EXAMPLE_RE.search(path):
        findings.append(SecretFinding(path=path, kind="new/modified .env file committed", verdict="env_file"))

    for line in added_lines:
        if _is_placeholder(line):
            continue
        for kind, pattern in _CRITICAL_PATTERNS:
            m = pattern.search(line)
            if m:
                findings.append(SecretFinding(path=path, kind=kind, verdict="critical", line_excerpt=_redact(m.group(0), kind)))
                break  # one finding per line is enough signal; avoid duplicate noise from overlapping patterns
        else:
            m = _GENERIC_SECRET_RE.search(line)
            if m and not _is_placeholder(m.group(1)):
                findings.append(SecretFinding(
                    path=path, kind="credential-shaped assignment", verdict="suspicious",
                    line_excerpt=_redact(m.group(1), "generic"),
                ))
    return findings


def run_secret_leak_gate(workdir: Path, diff: str) -> SecretLeakVerdict:
    """Top-level entry point. `workdir` isn't needed today — kept for
    signature symmetry with the other pre-flight gates, which may need it
    if a future revision wants to re-check the committed file rather than
    just the diff text."""
    del workdir
    if not diff.strip():
        return SecretLeakVerdict(ran=False)

    all_findings: List[SecretFinding] = []
    for path, block in _changed_blocks(diff).items():
        all_findings.extend(scan_file(path, _added_lines(block)))

    if not all_findings:
        return SecretLeakVerdict(ran=False)

    critical = [f for f in all_findings if f.verdict in ("critical", "env_file")]
    suspicious = [f for f in all_findings if f.verdict == "suspicious"]
    ok = not critical

    reprompt = ""
    if critical:
        lines = [
            "## Secret Leak Gate caught a likely hardcoded credential — fix ONLY this, before anything else.",
            "AI-assisted commits leak a real secret at roughly double the rate of human-only commits (GitGuardian,",
            "2026) — this turn matched that exact pattern. Move each value below to an environment variable or a",
            "gitignored .env file (never commit .env itself), and reference it via os.environ / process.env instead:",
            "",
        ]
        for f in critical:
            lines.append(f"- {f.path}: {f.kind} — `{f.line_excerpt}`")
        reprompt = "\n".join(lines)

    return SecretLeakVerdict(ran=True, ok=ok, critical=critical, suspicious=suspicious, reprompt=reprompt)
