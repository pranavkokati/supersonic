"""Test Quality Gate — catches a test that passes but doesn't actually test
anything, the one failure mode "tests: PASS" cannot see by definition.

## Why this exists

Every signal in this codebase up to this point answers "did the tests pass."
None of them answer the harder, more important question: if this new code
had a real bug in it, would the tests the agent just wrote actually notice?
An LLM-authored test that calls a function and asserts it didn't raise, or
asserts a value equals whatever the implementation currently returns, adds
a green checkmark and zero real coverage. "Tests: PASS" and "tests: PASS but
provide no protection at all" render identically to every other signal in
this gate — including the human skimming a Review Brief.

This is not a hypothetical concern. Mutation-guided evaluation of
LLM-generated test suites is an active research area as of 2026 — Meta has
an internal system (Automated Compliance Hardening) built specifically to
mutation-test LLM-authored tests before trusting them, and a June 2026 paper
(MutGen, arXiv:2506.02954) is dedicated to using mutation feedback to make
LLM test generation better in the first place. Nobody has shipped this as a
signal inside an open, standalone coding-agent loop; that's the gap this
module fills.

## What this module does

Classic mutation testing, deliberately scoped down to stay cheap:

  1. Parse the turn's diff, find every Python file it touched, and for each
     one, map the *added* line numbers back to the top-level function(s)
     they fall inside (via `ast.parse` + `FunctionDef`/`AsyncFunctionDef`
     line ranges). Only touched functions are candidates — this is not
     whole-repo mutation testing, which would be far too slow to run on
     every turn.
  2. For each touched function, generate a small, bounded set of single-point
     mutants: flip a comparison operator (`==` -> `!=`, `<` -> `>=`, ...),
     flip a boolean operator (`and` -> `or`), flip a boolean/numeric
     constant, or negate a condition. One AST node changed per mutant.
  3. For each mutant: write the mutated source to the real file (the
     original bytes are always restored afterward, in a `finally`, even on
     a crash or timeout — this function never leaves the working tree in a
     mutated state), then re-run the project's already-detected test
     command. If the suite still passes, the mutant *survived* — the tests
     didn't notice a real behavioral change. If it fails, the mutant was
     *killed* — proof the tests do exercise that logic.

Hard caps keep this affordable: at most `MAX_MUTANTS_PER_FUNCTION` mutants
per function, `MAX_TOTAL_MUTANTS` per turn, and a wall-clock budget — if the
budget runs out, the remaining candidates are simply not tested (reported
honestly as "skipped: budget exceeded", never silently treated as killed).

This is a soft signal, not a veto. Unlike Dependency Trust and Secret Leak,
a surviving mutant is not proof of a bug — it's evidence the tests around
one function are weaker than they look, which is exactly the kind of thing
that belongs in a human's Review Brief, not an automatic rollback. It
participates in the Verify gate's normal N-of-M vote like the original four
signals; it does not fail a turn outright.
"""

from __future__ import annotations

import ast
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from supersonic.verify.qa import _detect_test_command, _run

logger = logging.getLogger(__name__)

MAX_MUTANTS_PER_FUNCTION = 4
MAX_TOTAL_MUTANTS = 16
PER_MUTANT_TIMEOUT_SECONDS = 30
TOTAL_BUDGET_SECONDS = 90

_DIFF_FILE_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)

_COMPARE_FLIPS = {
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE, ast.GtE: ast.Lt,
    ast.Gt: ast.LtE, ast.LtE: ast.Gt,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
}
_BOOLOP_FLIPS = {ast.And: ast.Or, ast.Or: ast.And}


@dataclass
class MutationFinding:
    path: str
    function: str
    mutation: str  # human-readable description, e.g. "== -> !="
    survived: bool

    def to_dict(self) -> dict:
        return {"path": self.path, "function": self.function, "mutation": self.mutation, "survived": self.survived}


@dataclass
class TestQualityVerdict:
    __test__ = False  # not a pytest test class, despite the name — silence collection warnings

    ran: bool = False
    passed: bool = True
    mutants_generated: int = 0
    mutants_killed: int = 0
    survivors: List[MutationFinding] = field(default_factory=list)
    budget_exceeded: bool = False
    skipped_reason: str = ""

    @property
    def kill_rate(self) -> Optional[float]:
        if self.mutants_generated == 0:
            return None
        return self.mutants_killed / self.mutants_generated

    def to_dict(self) -> dict:
        return {
            "ran": self.ran,
            "passed": self.passed,
            "mutants_generated": self.mutants_generated,
            "mutants_killed": self.mutants_killed,
            "kill_rate": self.kill_rate,
            "survivors": [s.to_dict() for s in self.survivors],
            "budget_exceeded": self.budget_exceeded,
        }

    def to_context_block(self) -> str:
        if not self.ran:
            reason = self.skipped_reason or "no touched function had a mutatable branch/comparison"
            return f"## Test Quality Gate\nNot run ({reason})."
        rate = f"{self.kill_rate:.0%}" if self.kill_rate is not None else "n/a"
        lines = [
            f"## Test Quality Gate — {'PASS' if self.passed else 'WEAK'} "
            f"({self.mutants_killed}/{self.mutants_generated} mutants killed, {rate})"
        ]
        for s in self.survivors:
            lines.append(f"- [SURVIVED] {s.path}::{s.function} — a mutant ({s.mutation}) didn't change the test outcome")
        if self.budget_exceeded:
            lines.append("- (mutation budget exceeded — not every candidate was tested)")
        return "\n".join(lines)


def _changed_line_numbers(diff: str) -> Dict[str, set]:
    """Map {path: {added line numbers in the NEW file}} from a unified diff."""
    if not diff.strip():
        return {}
    matches = list(_DIFF_FILE_HEADER_RE.finditer(diff))
    result: Dict[str, set] = {}
    for idx, m in enumerate(matches):
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(diff)
        path = m.group(2) or m.group(1)
        block = diff[start:end]
        lines_added: set = set()
        cursor = None
        for line in block.splitlines():
            hunk = _HUNK_HEADER_RE.match(line)
            if hunk:
                cursor = int(hunk.group(1))
                continue
            if cursor is None:
                continue
            if line.startswith("+") and not line.startswith("+++"):
                lines_added.add(cursor)
                cursor += 1
            elif line.startswith("-") and not line.startswith("---"):
                continue  # removed line consumes no line number in the new file
            else:
                cursor += 1
        if lines_added:
            result[path] = lines_added
    return result


def _touched_functions(source: str, added_lines: set) -> List[ast.FunctionDef]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    touched = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", None) or node.lineno
        if any(node.lineno <= ln <= end for ln in added_lines):
            touched.append(node)
    return touched


def _mutation_candidates(func_node: ast.AST) -> List[Tuple[str, int, int, str, object]]:
    """Return (kind, lineno, col_offset, description, new_op_or_value) tuples,
    one per mutatable point found in this function's subtree. Position
    (lineno/col_offset), not a node reference, is the handle used to find
    this exact spot again later — `_apply_mutation` re-parses the source
    from scratch for every mutant (needed so each mutant starts from the
    pristine, unmutated tree), so a node object from this walk would be
    from a different AST entirely and could never be found again via `is`."""
    candidates: List[Tuple[str, int, int, str, object]] = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            op_type = type(node.ops[0])
            if op_type in _COMPARE_FLIPS:
                candidates.append((
                    "compare", node.lineno, node.col_offset,
                    f"{op_type.__name__} -> {_COMPARE_FLIPS[op_type].__name__}", _COMPARE_FLIPS[op_type],
                ))
        elif isinstance(node, ast.BoolOp):
            op_type = type(node.op)
            if op_type in _BOOLOP_FLIPS:
                candidates.append((
                    "boolop", node.lineno, node.col_offset,
                    f"{op_type.__name__} -> {_BOOLOP_FLIPS[op_type].__name__}", _BOOLOP_FLIPS[op_type],
                ))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            candidates.append(("constant", node.lineno, node.col_offset, f"{node.value} -> {not node.value}", not node.value))
    return candidates[:MAX_MUTANTS_PER_FUNCTION]


def _apply_mutation(source: str, kind: str, lineno: int, col_offset: int, new_value: object) -> Optional[str]:
    """Return the full mutated source text, or None if the target position
    couldn't be relocated or unparsing failed. Re-parses `source` fresh (the
    pristine original, read from disk right before this call) and finds the
    node at (lineno, col_offset) rather than relying on object identity."""
    tree = ast.parse(source)
    target = None
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) != lineno or getattr(node, "col_offset", None) != col_offset:
            continue
        if kind == "compare" and isinstance(node, ast.Compare) and len(node.ops) == 1:
            node.ops = [new_value()]
            target = node
            break
        if kind == "boolop" and isinstance(node, ast.BoolOp):
            node.op = new_value()
            target = node
            break
        if kind == "constant" and isinstance(node, ast.Constant):
            node.value = new_value
            target = node
            break
    if target is None:
        return None
    try:
        return ast.unparse(tree)
    except Exception:
        logger.debug("mutation testing: ast.unparse failed, skipping this mutant")
        return None


def run_test_quality_gate(workdir: Path, diff: str, min_kill_rate: float = 0.7) -> TestQualityVerdict:
    """Top-level entry point. Only meaningful once the real test suite is
    already known to pass — call this after `tests.passed` in the normal
    Verify gate, never on a turn whose real tests are already broken (a
    mutant "surviving" against an already-broken suite is meaningless
    noise, not a finding)."""
    workdir = Path(workdir)
    test_cmd = _detect_test_command(workdir)
    if not test_cmd:
        return TestQualityVerdict(ran=False, skipped_reason="no test command detected")

    changed = _changed_line_numbers(diff)
    py_changed = {p: lines for p, lines in changed.items() if p.endswith(".py")}
    if not py_changed:
        return TestQualityVerdict(ran=False, skipped_reason="no touched Python files")

    all_candidates: List[Tuple[Path, str, str, int, int, str, object]] = []
    for rel_path, added_lines in py_changed.items():
        full_path = workdir / rel_path
        if not full_path.exists():
            continue
        try:
            source = full_path.read_text(encoding="utf-8")
        except OSError:
            continue
        for func in _touched_functions(source, added_lines):
            for kind, lineno, col_offset, desc, new_val in _mutation_candidates(func):
                all_candidates.append((full_path, rel_path, kind, lineno, col_offset, f"{func.name}: {desc}", new_val))
                if len(all_candidates) >= MAX_TOTAL_MUTANTS:
                    break
            if len(all_candidates) >= MAX_TOTAL_MUTANTS:
                break
        if len(all_candidates) >= MAX_TOTAL_MUTANTS:
            break

    if not all_candidates:
        return TestQualityVerdict(ran=False, skipped_reason="no mutatable branch/comparison in touched functions")

    killed = 0
    survivors: List[MutationFinding] = []
    budget_exceeded = False
    start = time.monotonic()

    for full_path, rel_path, kind, lineno, col_offset, description, new_val in all_candidates:
        if time.monotonic() - start > TOTAL_BUDGET_SECONDS:
            budget_exceeded = True
            break
        original = full_path.read_text(encoding="utf-8")
        func_name = description.split(":", 1)[0]
        mutation_desc = description.split(":", 1)[1].strip() if ":" in description else description
        try:
            mutated = _apply_mutation(original, kind, lineno, col_offset, new_val)
            if mutated is None:
                continue
            full_path.write_text(mutated, encoding="utf-8")
            try:
                res = _run(test_cmd, workdir, timeout=PER_MUTANT_TIMEOUT_SECONDS)
                mutant_survived = res.returncode == 0
            except Exception:
                # A crash/timeout running the mutant is itself evidence something
                # noticed it (the suite didn't cleanly pass) — treat as killed.
                mutant_survived = False
        finally:
            full_path.write_text(original, encoding="utf-8")

        if mutant_survived:
            survivors.append(MutationFinding(path=rel_path, function=func_name, mutation=mutation_desc, survived=True))
        else:
            killed += 1

    generated = len(survivors) + killed
    verdict = TestQualityVerdict(
        ran=True,
        passed=(generated == 0) or (killed / generated >= min_kill_rate),
        mutants_generated=generated,
        mutants_killed=killed,
        survivors=survivors,
        budget_exceeded=budget_exceeded,
    )
    return verdict
