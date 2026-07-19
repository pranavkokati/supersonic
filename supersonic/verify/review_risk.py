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

  1. **Blast radius** — how many other files in the repo import this file
     (see `compute_blast_radius`). For Python, this is real `ast.walk`-based
     import-graph resolution: actual `Import`/`ImportFrom` node matching,
     with relative imports (`from . import x`, `from ..pkg import y`)
     resolved against the importing file's own package path, plus a
     `Call`/`Name`-node fallback for the one case import resolution can't
     settle alone (`from target import *`). For JS/TS, no in-repo AST parser
     is available, so it falls back to the original substring-containment
     text heuristic — same deliberate scope limit as `dependency_mapper.py`.
     Neither is LSP-grade symbol resolution; both are good enough to
     separate "isolated CSS tweak" from "core module eleven other files
     depend on."
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

import ast
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from supersonic.verify.dependency_trust import PackageFinding
from supersonic.verify.secret_leak import SecretFinding

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


# --------------------------------------------------------------------------
# Blast radius — Python: real ast.walk-based import-graph resolution.
#
# This replaced a substring-containment text search (does the changed file's
# stem or dotted path appear anywhere in another file's raw text). That
# approach flagged plain-English comments and string literals that happened
# to contain a module name as "references", and had no way to resolve
# relative imports (`from . import x`, `from ..pkg import y`) back to the
# file they actually point at. The version below parses every Python file
# once with `ast.parse` and walks real `Import`/`ImportFrom` nodes, resolving
# relative imports against the importing file's own package path. A `from
# target import *` is the one case import resolution alone can't settle
# (the star could bind zero or many names) — for that case only, it falls
# through to a `Call`/`Name` node walk checking whether the file actually
# uses one of the target module's top-level defined function/class names.
#
# This is still not full symbol resolution — it doesn't track re-exports,
# `importlib`-based dynamic imports, or `TYPE_CHECKING`-guarded imports, and
# a syntactically broken candidate file (mid-edit, say) is simply excluded
# from the count rather than crashing the pass. Those are the same kind of
# deliberate scope limits `dependency_mapper.py` documents; the point is
# "meaningfully more precise than a substring guess," not "an IDE's Find
# References." JS/TS files still use the original substring heuristic below
# (`_blast_radius_text_heuristic`) — no JS/TS AST parser is available in
# this codebase, and pretending otherwise would be a worse bug than the
# heuristic it would replace.
# --------------------------------------------------------------------------

PY_SUFFIX = ".py"


def _module_dotted_path(workdir: Path, path: Path) -> str:
    """`pkg/sub/mod.py` -> `pkg.sub.mod`; `pkg/sub/__init__.py` -> `pkg.sub`
    (a package's own dotted name, not `pkg.sub.__init__` — nobody imports
    the literal `__init__` segment)."""
    try:
        rel = path.relative_to(workdir)
    except ValueError:
        rel = path
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _importer_package(importer_dotted: str, is_init: bool) -> str:
    """The dotted package a file lives in, for relative-import resolution.
    A package's own `__init__.py` IS that package (level=1 relative imports
    inside it resolve against itself); a regular module's package is
    everything before its last dotted segment."""
    if is_init:
        return importer_dotted
    if "." in importer_dotted:
        return importer_dotted.rsplit(".", 1)[0]
    return ""


def _resolve_relative_import(importer_package: str, level: int, module: Optional[str]) -> Optional[str]:
    """Resolve `from . import x` (level=1) / `from .. import x` (level=2) /
    `from .sub import x` etc. to an absolute dotted module path, given the
    dotted package the importing file lives in. Returns None if the level
    walks above the repo root — can't resolve, so it's simply not counted
    as a reference rather than guessed at."""
    parts = importer_package.split(".") if importer_package else []
    strip = level - 1
    if strip > len(parts):
        return None
    base_parts = parts[: len(parts) - strip] if strip else parts
    base = ".".join(base_parts)
    if module:
        return f"{base}.{module}" if base else module
    return base or None


def _top_level_defined_names(tree: Optional[ast.Module]) -> set:
    """Function/class names (and simple module-level assignments) defined
    directly in this file — the candidate set a `from target import *` +
    later `Call`/`Name` usage is checked against."""
    if tree is None:
        return set()
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _python_file_references_target(
    tree: ast.Module, importer_dotted: str, importer_is_init: bool,
    target_dotted: str, target_defined_names: set,
) -> bool:
    """Does this parsed candidate file actually import `target_dotted` (or a
    name from it), correctly resolving relative imports? For the one case
    import resolution alone can't settle — `from target import *` — fall
    back to walking Call/Name nodes for actual usage of a name the target
    module defines."""
    if not target_dotted:
        return False
    importer_package = _importer_package(importer_dotted, importer_is_init)
    star_imported_from_target = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                full = alias.name
                if full == target_dotted or full.startswith(target_dotted + "."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            resolved = (
                _resolve_relative_import(importer_package, node.level, node.module)
                if node.level else node.module
            )
            if not resolved:
                continue
            if resolved == target_dotted or resolved.startswith(target_dotted + "."):
                for alias in node.names:
                    if alias.name == "*":
                        star_imported_from_target = True
                    else:
                        return True  # a specifically-named import is direct, unambiguous evidence
            elif "." in target_dotted and resolved == target_dotted.rsplit(".", 1)[0]:
                # `from pkg import target_module` — the target itself is the imported name.
                target_leaf = target_dotted.rsplit(".", 1)[-1]
                if any(alias.name == target_leaf for alias in node.names):
                    return True

    if not star_imported_from_target:
        return False
    if not target_defined_names:
        # Star-imported a module with nothing we could identify as a
        # top-level def/class (e.g. a pure-constants module) — still a real
        # import, just can't be confirmed by usage, so count it.
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in target_defined_names:
            return True
        if isinstance(node, ast.Name) and node.id in target_defined_names:
            return True
    return False


def _blast_radius_python(workdir: Path, changed: str, py_trees: Dict[Path, ast.Module]) -> int:
    target_path = next((p for p in py_trees if _safe_rel(p, workdir) == changed), None)
    target_dotted = _module_dotted_path(workdir, workdir / changed)
    if not target_dotted:
        return 0
    target_defined_names = _top_level_defined_names(py_trees.get(target_path)) if target_path else set()

    count = 0
    for path, tree in py_trees.items():
        rel = _safe_rel(path, workdir)
        if rel == changed:
            continue
        importer_dotted = _module_dotted_path(workdir, path)
        importer_is_init = path.name == "__init__.py"
        if _python_file_references_target(tree, importer_dotted, importer_is_init, target_dotted, target_defined_names):
            count += 1
    return count


def _safe_rel(path: Path, workdir: Path) -> str:
    try:
        return str(path.relative_to(workdir))
    except ValueError:
        return str(path)


def _blast_radius_text_heuristic(workdir: Path, changed: str, file_texts: Dict[Path, str]) -> int:
    """Original substring-containment heuristic, kept as-is for non-Python
    source files (JS/TS): does the changed file's stem or dotted path appear
    anywhere in another file's raw text. No JS/TS AST parser is available
    in this codebase, so this stays a heuristic — same deliberate scope
    limit as `dependency_mapper.py`. A false positive here just means a file
    gets slightly over-prioritized for review, the safe direction to be
    wrong in."""
    stem = Path(changed).stem
    if not stem or len(stem) < 3:
        return 0
    dotted = changed.rsplit(".", 1)[0].replace("/", ".").replace("\\", ".")
    candidates = {stem, dotted}
    count = 0
    for path, text in file_texts.items():
        if _safe_rel(path, workdir) == changed:
            continue
        if any(c and c in text for c in candidates):
            count += 1
    return count


def compute_blast_radius(workdir: Path, changed_paths: List[str]) -> Dict[str, int]:
    """For each changed file, how many *other* files in the repo reference
    it. Python files get real import-graph resolution (see
    `_python_file_references_target`); everything else falls back to the
    original text heuristic (see `_blast_radius_text_heuristic`)."""
    workdir = Path(workdir)
    source_files = _iter_source_files(workdir)

    py_trees: Dict[Path, ast.Module] = {}
    file_texts: Dict[Path, str] = {}
    for f in source_files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        file_texts[f] = text
        if f.suffix == PY_SUFFIX:
            try:
                py_trees[f] = ast.parse(text)
            except (SyntaxError, ValueError):
                logger.debug("blast radius: skipping unparsable python file %s", f)

    radius: Dict[str, int] = {}
    for changed in changed_paths:
        if changed.endswith(PY_SUFFIX):
            radius[changed] = _blast_radius_python(workdir, changed, py_trees)
        else:
            radius[changed] = _blast_radius_text_heuristic(workdir, changed, file_texts)
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


def _secret_notes_by_file(findings: List[SecretFinding]) -> Dict[str, List[str]]:
    """Map a changed file's path -> reason strings for any Secret Leak Gate
    finding attributed to it. A shipped turn should never carry a `critical`
    finding (that verdict fails the Verify gate outright in verify/gate.py),
    so in practice this only ever surfaces `suspicious` matches — the
    generic credential-shaped-assignment heuristic that's deliberately too
    false-positive-prone to block a turn on its own, but is exactly the kind
    of thing a human reviewer should get pointed at rather than have to spot
    themselves in a wall of diff."""
    notes: Dict[str, List[str]] = {}
    for f in findings:
        if f.verdict != "suspicious":
            continue
        notes.setdefault(f.path, []).append(f"possible hardcoded credential ({f.kind}: {f.line_excerpt})")
    return notes


def _score_file(
    path: str, added: int, removed: int, blast_radius: int,
    sensitive_hits: List[str], has_test_delta: bool,
    dependency_notes: Optional[List[str]] = None,
    secret_notes: Optional[List[str]] = None,
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

    if secret_notes:
        score += 3
        reasons.extend(secret_notes)

    level = "high" if score >= 5 else "medium" if score >= 2 else "low"
    return FileRisk(
        path=path, score=score, level=level, reasons=reasons,
        lines_added=added, lines_removed=removed,
        blast_radius=blast_radius, has_test_delta=has_test_delta,
    )


def build_review_brief(
    workdir: Path, turn: int, diff: str,
    dependency_findings: Optional[List[PackageFinding]] = None,
    secret_findings: Optional[List[SecretFinding]] = None,
) -> ReviewBrief:
    """Top-level entry point. Only meaningful for a turn that already passed
    the Verify gate — call this after `gate.passed`, not before.

    `dependency_findings` is optional and comes straight from
    `gate.dependency_trust.suspicious` (plus `.critical`, though a shipped
    turn should never carry one — see `_dependency_notes_by_file`): a file
    that adds a newly-registered, unverified dependency scores as risky
    regardless of what the blast-radius/sensitive-path/test-delta heuristics
    say about it.

    `secret_findings` is optional and comes straight from
    `gate.secret_leak.suspicious` (a shipped turn should never carry a
    `.critical` finding — see `_secret_notes_by_file`): a file with a
    credential-shaped assignment scores as risky regardless of what the
    other heuristics say."""
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
    secret_notes = _secret_notes_by_file(secret_findings or [])

    items: List[FileRisk] = []
    for path, block in blocks.items():
        added, removed = _count_lines(block)
        sensitive = _sensitive_hits(path, _added_text(block))
        test_delta = _has_test_delta(path, changed_paths)
        items.append(_score_file(
            path, added, removed, blast_radii.get(path, 0), sensitive, test_delta,
            dependency_notes=dep_notes.get(path),
            secret_notes=secret_notes.get(path),
        ))

    items.sort(key=lambda i: i.score, reverse=True)
    return ReviewBrief(turn=turn, items=items)
