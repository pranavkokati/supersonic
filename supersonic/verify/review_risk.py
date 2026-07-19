"""Review Risk — turns a passing diff into a ranked list of what a human should
actually read, instead of a wall of green checkmarks.

## Why this exists (the actual bet, not another pipeline stage)

Every current-generation coding agent — Supersonic included, before this
module — competes on the same axis: how fast and how autonomously can the
agent produce a change that passes its own checks. That axis is close to
saturated. A controlled 2025 study of experienced open-source developers
using AI coding tools found task time *increased* by ~19% versus a human
baseline, even though the developers *expected* a ~24% speedup — the gap
was verification overhead. The agent got faster; reviewing what it did got
slower, and nobody built for that side of the equation. Separately: the
hardest class of AI-introduced bug is not the one that fails a test — it's
the one that compiles, passes lint, and satisfies a goal-satisfaction critic
while quietly touching an auth check, a payment path, or a delete/migration
with no corresponding test change. Tests/lint/critic/thrash (the existing
Verify gate) are all *pass/fail* signals. None of them tell a human where to
spend their five minutes of review time on a turn that already passed.

That's what this module is for. It runs only on turns that already passed
the Verify gate — a rolled-back turn doesn't need a review brief, it needs
nothing, it's gone. For a *shipped* turn, Review Risk reads the diff and
produces a ranked list: which changed files carry real risk, and the
specific, named reason why (not a generic "review this" — "touches
supersonic/auth/session.py, no corresponding test change, imported by 11
other files").

## How the score is computed (heuristic, and documented as such)

Four independent, cheap-to-compute signals, each contributing points to a
per-file score:

  1. **Blast radius** — how many other files in the repo import this file,
     via a best-effort static reference count (see `compute_blast_radius`).
     Not LSP-grade symbol resolution (same deliberate scope limit as
     `dependency_mapper.py`) — a text-reference count across import
     statements. Good enough to separate "isolated CSS tweak" from "core
     module eleven other files depend on."
  2. **Sensitive-path/content heuristic** — keyword match against auth,
     payment, secrets, permissions, migrations, and destructive-operation
     patterns, checked against both the file path and the *added* lines of
     the diff (not removed — new code is what introduces new risk).
  3. **Test-coverage delta** — did this turn's diff also touch a test file
     whose name plausibly covers the changed source file. A source change
     with zero corresponding test change is exactly the "looks correct,
     nobody would catch it in CI" failure mode described above.
  4. **Diff size** — a large single-file hunk is harder for a human to
     actually read carefully than a two-line change; size alone doesn't
     make something risky, but it lowers the odds a skimmed review catches
     a real problem, so it nudges the score rather than driving it.

Buckets: HIGH (score >= 5), MEDIUM (2-4), LOW (0-1). The exact weights are
in `_score_file` and are intentionally simple integers, not a trained
model — this is a fast static heuristic meant to *redirect* attention, not
a verdict. It will have false positives and false negatives; the point is
that "review these 2 of the 14 changed files closely, skim the rest" beats
"here's a diff, good luck" on average, not that it's infallible.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from supersonic.verify.dependency_trust import PackageFinding

logger = logging.getLogger(__name__)

# Same source-file scope as dependency_mapper.py, for the same reason: cheap,
# regex-based, no per-language tooling dependency.
SOURCE_EXTENSIONS = (".py", ".js", ".jsx", ".ts", ".tsx")

SKIP_DIR_NAMES = {
    ".git", ".dle", ".continuity", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", "target", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}

# Matched case-insensitively against file paths and added diff lines. Grouped
# loosely by category in comments only; the scoring treats any match the same.
_SENSITIVE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"\bauth\w*",
    r"\bsession\w*",
    r"\blogin\b",
    r"\bpassword\b",
    r"\bsecret\w*",
    r"\btoken\b",
    r"\bcredential\w*",
    r"\bpermission\w*",
    r"\bacl\b",
    r"\brole\w*",
    r"\badmin\w*",
    r"\bpayment\w*",
    r"\bbilling\b",
    r"\bstripe\b",
    r"\bcharge\w*",
    r"\bwebhook\w*",
    r"\bencrypt\w*",
    r"\bdecrypt\w*",
    r"\bmigrat\w*",
    r"\bdrop\s+table\b",
    r"\bdelete\s+from\b",
    r"\brm\s+-rf\b",
    r"\.env\b",
)]

_DIFF_FILE_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
_TEST_DIR_OR_NAME_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|test_|_test\.|\.test\.|\.spec\.", re.IGNORECASE)


@dataclass
class FileRisk:
    path: str
    score: int
    level: str  # "high" | "medium" | "low"
    reasons: List[str] = field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0
    blast_radius: int = 0
    has_test_delta: bool = False

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "score": self.score,
            "level": self.level,
            "reasons": self.reasons,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "blast_radius": self.blast_radius,
            "has_test_delta": self.has_test_delta,
        }


@dataclass
class ReviewBrief:
    turn: int
    items: List[FileRisk] = field(default_factory=list)

    @property
    def high_count(self) -> int:
        return sum(1 for i in self.items if i.level == "high")

    @property
    def medium_count(self) -> int:
        return sum(1 for i in self.items if i.level == "medium")

    @property
    def low_count(self) -> int:
        return sum(1 for i in self.items if i.level == "low")

    def to_dict(self) -> dict:
        return {
            "turn": self.turn,
            "high_count": self.high_count,
            "medium_count": self.medium_count,
            "low_count": self.low_count,
            "items": [i.to_dict() for i in self.items],
        }

    def to_context_block(self, limit: int = 8) -> str:
        if not self.items:
            return ""
        lines = [f"## Review Brief — {self.high_count} high / {self.medium_count} medium / {self.low_count} low risk file(s)"]
        for item in self.items[:limit]:
            reason = "; ".join(item.reasons) if item.reasons else "no specific risk factors"
            lines.append(f"- [{item.level.upper()}] {item.path} — {reason}")
        return "\n".join(lines)

    def summary_line(self) -> str:
        if not self.items:
            return "No changed files to review."
        if self.high_count:
            top = next(i for i in self.items if i.level == "high")
            return f"{self.high_count} file(s) need a close look, starting with {top.path}."
        if self.medium_count:
            return f"{self.medium_count} file(s) worth a skim, nothing flagged high-risk."
        return f"All {len(self.items)} changed file(s) are low-risk by this heuristic — safe to skim."


def _parse_changed_files(diff: str) -> Dict[str, str]:
    """Return {relative_path: file's diff block text} for every file touched."""
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


def _count_lines(block: str) -> tuple:
    added = removed = 0
    for line in block.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def _added_text(block: str) -> str:
    return "\n".join(
        line[1:] for line in block.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def _sensitive_hits(path: str, added_text: str) -> List[str]:
    haystack = f"{path}\n{added_text}"
    hits: List[str] = []
    for pattern in _SENSITIVE_PATTERNS:
        m = pattern.search(haystack)
        if m:
            hits.append(m.group(0).strip().lower())
    # de-dupe while preserving order, cap so the reason string stays readable
    seen = set()
    out = []
    for h in hits:
        if h in seen:
            continue
        seen.add(h)
        out.append(h)
    return out[:4]


def _has_test_delta(path: str, all_changed_paths: List[str]) -> bool:
    if _TEST_DIR_OR_NAME_RE.search(path):
        return True  # the file itself IS a test — no delta needed for itself
    stem = Path(path).stem
    if not stem or len(stem) < 3:
        return False
    for other in all_changed_paths:
        if other == path:
            continue
        if _TEST_DIR_OR_NAME_RE.search(other) and stem.lower() in other.lower():
            return True
    return False


def _iter_source_files(workdir: Path) -> List[Path]:
    out: List[Path] = []
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith(".")]
        for name in files:
            if name.endswith(SOURCE_EXTENSIONS):
                out.append(Path(root) / name)
    return out


def compute_blast_radius(workdir: Path, changed_paths: List[str]) -> Dict[str, int]:
    """Best-effort static reference count: for each changed file, how many
    *other* files in the repo textually reference it (by dotted module path,
    relative import path, or bare filename stem). Not symbol-resolution —
    same deliberate scope limit as dependency_mapper.py. A false positive
    here just means a file gets slightly over-prioritized for review, which
    is the safe direction to be wrong in."""
    workdir = Path(workdir)
    source_files = _iter_source_files(workdir)
    file_texts: Dict[Path, str] = {}
    for f in source_files:
        try:
            file_texts[f] = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

    radius: Dict[str, int] = {}
    for changed in changed_paths:
        stem = Path(changed).stem
        if not stem or stem == "__init__" or len(stem) < 3:
            radius[changed] = 0
            continue
        dotted = changed.rsplit(".", 1)[0].replace("/", ".").replace("\\", ".")
        candidates = {stem, dotted}
        count = 0
        for path, text in file_texts.items():
            try:
                rel = str(path.relative_to(workdir))
            except ValueError:
                rel = str(path)
            if rel == changed:
                continue
            if any(c and c in text for c in candidates):
                count += 1
        radius[changed] = count
    return radius


def _dependency_notes_by_file(findings: List[PackageFinding]) -> Dict[str, List[str]]:
    """Map a changed file's path -> reason strings for any dependency-trust
    finding attributed to it (suspicious or, in principle, nonexistent —
    though a `nonexistent` finding fails the Verify gate outright in
    verify/gate.py, so a shipped turn should never actually carry one here).
    An "install command" manifest source (not a real file in the diff) is
    simply never matched by any changed path, which is the correct behavior:
    there's no specific file to point a reviewer at in that case."""
    notes: Dict[str, List[str]] = {}
    for f in findings:
        if f.verdict not in ("suspicious", "nonexistent"):
            continue
        age_note = f" ({f.age_days}d old, {f.release_count} release(s))" if f.age_days is not None else ""
        notes.setdefault(f.manifest, []).append(f"unverified dependency '{f.name}'{age_note}")
    return notes


def _score_file(
    path: str, added: int, removed: int, blast_radius: int,
    sensitive_hits: List[str], has_test_delta: bool,
    dependency_notes: Optional[List[str]] = None,
) -> FileRisk:
    score = 0
    reasons: List[str] = []

    if blast_radius > 10:
        score += 3
        reasons.append(f"imported by {blast_radius} other files")
    elif blast_radius >= 4:
        score += 2
        reasons.append(f"imported by {blast_radius} other files")
    elif blast_radius >= 1:
        score += 1
        reasons.append(f"imported by {blast_radius} other file(s)")

    if sensitive_hits:
        score += 3
        reasons.append(f"touches sensitive surface ({', '.join(sensitive_hits)})")

    if not has_test_delta:
        score += 2
        reasons.append("no corresponding test change in this turn")

    total_lines = added + removed
    if total_lines > 150:
        score += 1
        reasons.append(f"large diff ({total_lines} lines changed)")

    if dependency_notes:
        score += 3
        reasons.extend(dependency_notes)

    level = "high" if score >= 5 else "medium" if score >= 2 else "low"
    return FileRisk(
        path=path, score=score, level=level, reasons=reasons,
        lines_added=added, lines_removed=removed,
        blast_radius=blast_radius, has_test_delta=has_test_delta,
    )


def build_review_brief(
    workdir: Path, turn: int, diff: str, dependency_findings: Optional[List[PackageFinding]] = None,
) -> ReviewBrief:
    """Top-level entry point. Only meaningful for a turn that already passed
    the Verify gate — call this after `gate.passed`, not before.

    `dependency_findings` is optional and comes straight from
    `gate.dependency_trust.suspicious` (plus `.critical`, though a shipped
    turn should never carry one — see `_dependency_notes_by_file`): a file
    that adds a newly-registered, unverified dependency scores as risky
    regardless of what the blast-radius/sensitive-path/test-delta heuristics
    say about it."""
    workdir = Path(workdir)
    blocks = _parse_changed_files(diff)
    if not blocks:
        return ReviewBrief(turn=turn, items=[])

    changed_paths = list(blocks.keys())
    try:
        blast_radii = compute_blast_radius(workdir, changed_paths)
    except Exception:
        logger.exception("blast radius computation failed, continuing with zeros")
        blast_radii = {p: 0 for p in changed_paths}

    dep_notes = _dependency_notes_by_file(dependency_findings or [])

    items: List[FileRisk] = []
    for path, block in blocks.items():
        added, removed = _count_lines(block)
        sensitive = _sensitive_hits(path, _added_text(block))
        test_delta = _has_test_delta(path, changed_paths)
        items.append(_score_file(
            path, added, removed, blast_radii.get(path, 0), sensitive, test_delta,
            dependency_notes=dep_notes.get(path),
        ))

    items.sort(key=lambda i: i.score, reverse=True)
    return ReviewBrief(turn=turn, items=items)
