"""Dependency Mapper — cheap static factoring so a turn's diff stays scoped.

Part of the Deterministic Loop Engine (DLE). Before an agent turn runs, we
build a small "target graph": which files in the workdir plausibly relate to
the turn's goal, based on their import statements and keyword overlap with
the goal text. That graph is folded into the turn's prompt as a hint (`See
also:` block) so the agent factors changes toward the files that actually
matter instead of grep-and-hope across the whole tree.

Method: this is a *textual* dependency map, not a semantic one.
  1. Extract keywords from the turn's goal (strip stopwords/punctuation,
     keep tokens >= 4 chars).
  2. Search the workdir for import statements in Python/JS/TS/JSX/TSX files
     using ripgrep (`rg`) when it's on PATH — it's fast even on large trees.
  3. If `rg` is unavailable, fall back to a plain Python `os.walk` + regex
     scan over the same file types. Slower, but has zero external
     dependencies, so this module never hard-fails a turn just because the
     sandbox lacks ripgrep.
  4. Files are "in scope" if their path or their import lines mention one of
     the goal's keywords; edges record `file -> imported module/path` for
     every import statement found in an in-scope file (so the agent — and a
     future turn — can see what an in-scope file pulls in, even if the thing
     it pulls in didn't itself match a keyword).
  5. The resulting graph is cached at `<workdir>/.dle/target_graph.json` so
     repeated turns on a similar goal don't re-walk the tree; the cache key
     is the sorted keyword set, so a materially different goal invalidates
     it automatically.

Deliberately out of scope for v1: full LSP-grade symbol resolution (jump to
definition, cross-file type inference, monorepo package-boundary awareness).
That's a real project in its own right — a language server per ecosystem,
kept warm, kept in sync with on-disk edits — and it buys accuracy this loop
doesn't need yet. Supersonic's Verify gate (tests + lint + critic + thrash)
is what actually catches a wrong file being touched; this module only needs
to be a fast, good-enough *hint* for where to look, not a source of truth.
If DLE ever needs precise call-graph or type-aware refactors, that's the
natural place to bring in `pyright --outputjson`, `tsserver`, or similar —
not a rewrite of this module, an additional signal alongside it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

CACHE_DIRNAME = ".dle"
CACHE_FILENAME = "target_graph.json"

SOURCE_EXTENSIONS = (".py", ".js", ".jsx", ".ts", ".tsx")

# Skip the usual noise so we never scan (or cache-bust on) generated trees.
SKIP_DIR_NAMES = {
    ".git", ".dle", ".continuity", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", "target", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}

_STOPWORDS = {
    "this", "that", "with", "from", "into", "onto", "have", "will", "should",
    "make", "build", "continue", "toward", "plan", "turn", "goal", "please",
    "the", "and", "for", "add", "fix", "update", "create", "implement", "ensure",
}

_PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.MULTILINE)
_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[\w*{}\s,]+\s+from\s+)?|export\s+(?:[\w*{}\s,]+\s+from\s+)?|require\()\s*['"]([^'"]+)['"]""",
)

# ripgrep pattern matching either style of import statement, used only to
# narrow candidate *lines* — the precise regexes above still do the parsing.
_RG_IMPORT_PATTERN = r"^\s*(import |from |export .*from|.*require\()"


@dataclass
class DependencyEdge:
    src: str
    target: str

    def to_dict(self) -> dict:
        return {"src": self.src, "target": self.target}


@dataclass
class TargetGraph:
    goal_keywords: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    edges: List[DependencyEdge] = field(default_factory=list)
    method: str = "ripgrep"
    generated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "goal_keywords": self.goal_keywords,
            "files": self.files,
            "edges": [e.to_dict() for e in self.edges],
            "method": self.method,
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TargetGraph":
        return cls(
            goal_keywords=list(data.get("goal_keywords") or []),
            files=list(data.get("files") or []),
            edges=[DependencyEdge(**e) for e in (data.get("edges") or [])],
            method=data.get("method", "ripgrep"),
            generated_at=data.get("generated_at", ""),
        )

    def to_context_block(self, limit: int = 20) -> str:
        if not self.files:
            return ""
        lines = [f"## Likely-relevant files (static import scan, keywords: {', '.join(self.goal_keywords) or 'none'})"]
        for f in self.files[:limit]:
            targets = [e.target for e in self.edges if e.src == f]
            if targets:
                lines.append(f"- {f} → imports: {', '.join(targets[:8])}")
            else:
                lines.append(f"- {f}")
        return "\n".join(lines)


def extract_keywords(goal: str) -> List[str]:
    """Tokenize a goal string into lowercase keywords, longest-first, deduped."""
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", goal or "")
    seen: Set[str] = set()
    out: List[str] = []
    for tok in tokens:
        low = tok.lower()
        if len(low) < 4 or low in _STOPWORDS or low in seen:
            continue
        seen.add(low)
        out.append(low)
    return out


def _iter_source_files(workdir: Path) -> List[Path]:
    out: List[Path] = []
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith(".")]
        for name in files:
            if name.endswith(SOURCE_EXTENSIONS):
                out.append(Path(root) / name)
    return out


def _ripgrep_available() -> bool:
    return shutil.which("rg") is not None


def _scan_with_ripgrep(workdir: Path) -> Dict[str, str]:
    """Return {relative_file_path: full text of import-ish lines} via `rg`."""
    try:
        proc = subprocess.run(
            ["rg", "--no-heading", "--line-number", "-e", _RG_IMPORT_PATTERN, "-g", "*.py",
             "-g", "*.js", "-g", "*.jsx", "-g", "*.ts", "-g", "*.tsx", str(workdir)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("ripgrep scan failed, falling back to python walk")
        return {}
    if proc.returncode not in (0, 1):  # 1 == no matches, still fine
        logger.warning("ripgrep exited %s: %s", proc.returncode, proc.stderr[:300])
        return {}

    per_file: Dict[str, List[str]] = {}
    for line in proc.stdout.splitlines():
        # format: <path>:<lineno>:<text>
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        path, _, text = parts
        try:
            rel = str(Path(path).resolve().relative_to(workdir.resolve()))
        except ValueError:
            rel = path
        per_file.setdefault(rel, []).append(text)
    return {f: "\n".join(lines) for f, lines in per_file.items()}


def _scan_with_python_walk(workdir: Path) -> Dict[str, str]:
    """Fallback for environments without ripgrep on PATH."""
    per_file: Dict[str, str] = {}
    for path in _iter_source_files(workdir):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        import_lines = [
            ln for ln in text.splitlines()
            if _PY_IMPORT_RE.match(ln) or _JS_IMPORT_RE.search(ln)
        ]
        if import_lines:
            rel = str(path.relative_to(workdir))
            per_file[rel] = "\n".join(import_lines)
    return per_file


def _parse_imports(import_text: str) -> List[str]:
    targets: List[str] = []
    for m in _PY_IMPORT_RE.finditer(import_text):
        targets.append(m.group(1) or m.group(2) or "")
    for m in _JS_IMPORT_RE.finditer(import_text):
        targets.append(m.group(1))
    return [t for t in targets if t]


def _cache_path(workdir: Path) -> Path:
    return workdir / CACHE_DIRNAME / CACHE_FILENAME


def _load_cache(workdir: Path, keywords: List[str]) -> Optional[TargetGraph]:
    path = _cache_path(workdir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    graph = TargetGraph.from_dict(data)
    if sorted(graph.goal_keywords) == sorted(keywords):
        return graph
    return None


def _save_cache(workdir: Path, graph: TargetGraph) -> None:
    path = _cache_path(workdir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(graph.to_dict(), indent=2), encoding="utf-8")
    except OSError:
        logger.warning("could not write dependency mapper cache at %s", path)


def build_target_graph(workdir: Path, goal: str, *, use_cache: bool = True) -> TargetGraph:
    """Build (or load a cached) target graph scoped to `goal`'s keywords."""
    workdir = Path(workdir)
    keywords = extract_keywords(goal)

    if use_cache:
        cached = _load_cache(workdir, keywords)
        if cached is not None:
            return cached

    method = "ripgrep" if _ripgrep_available() else "python-walk"
    per_file = _scan_with_ripgrep(workdir) if method == "ripgrep" else {}
    if not per_file and method == "ripgrep":
        # ripgrep present but yielded nothing usable (e.g. empty repo, or the
        # subprocess call itself failed) — still try the walk fallback rather
        # than reporting zero relevant files.
        per_file = _scan_with_python_walk(workdir)
        method = "python-walk"
    elif method == "python-walk":
        per_file = _scan_with_python_walk(workdir)

    files: List[str] = []
    edges: List[DependencyEdge] = []
    for rel_path, import_text in per_file.items():
        haystack = f"{rel_path}\n{import_text}".lower()
        in_scope = not keywords or any(kw in haystack for kw in keywords)
        if not in_scope:
            continue
        files.append(rel_path)
        for target in _parse_imports(import_text):
            edges.append(DependencyEdge(src=rel_path, target=target))

    files.sort()
    graph = TargetGraph(
        goal_keywords=keywords,
        files=files,
        edges=edges,
        method=method,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if use_cache:
        _save_cache(workdir, graph)
    return graph
