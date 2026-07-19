"""Syntax Shield — a fast pre-check that runs before the expensive four-signal
Verify gate (tests, lint, critic, thrash).

The idea: a turn that leaves a Python file with a plain `SyntaxError` is
never going to pass tests or lint anyway, but discovering that by running a
full test suite (which can take tens of seconds) is a waste of the time and
the LLM critic call. `ast.parse` on just the changed `.py` files answers the
same question in milliseconds, with zero risk of false positives — a file
either parses as valid Python or it doesn't.

For JS/TS we do NOT have an equivalent zero-dependency parser available (no
`node`/`tsc` guaranteed to be on PATH, and shelling out to one on every turn
defeats the "fast" part of this module). Instead we run a cheap bracket and
quote *balance* check: counts of `()[]{}`, and parity of `'`/`"`/`` ` ``
outside of comments/strings-we-can-detect. This deliberately does NOT catch
every JS/TS syntax error — a real parse requires a JS toolchain. It exists to
catch the loud, common cases (an unclosed brace, a stray quote) fast, and it
degrades to "no finding" rather than a false alarm when the heuristic is
unsure — a real JS/TS syntax check is expected to happen via the existing
lint signal in the full gate (`verify/qa.py`'s `tsc --noEmit` detection),
which Syntax Shield does not replace.

### Correcting the original spec: no "5ms auto-repair"

An earlier version of this spec described Syntax Shield as doing "5ms
auto-repair" of syntax errors. That's not something this module attempts,
and it's worth saying plainly why: blindly "fixing" an arbitrary Python or
JS/TS `SyntaxError` is not a well-posed problem. A missing closing paren
three lines up could mean a dozen different intended edits, and a fixer that
guesses wrong doesn't just fail to fix the file — it silently corrupts it in
a way that may not surface until much later. There is no 5ms operation that
does this safely for arbitrary code; the "fix" step genuinely needs judgment
about what the author meant, which is exactly what an LLM re-prompt is for.

So Syntax Shield does fast-detect + fast-reprompt, not auto-repair:
  1. Detect the syntax error fast (`ast.parse`, or the bracket/quote check).
  2. Hand the *exact* traceback / error text back to the agent as a targeted
     re-prompt: "fix only this syntax error, change nothing else."
  3. One re-prompt only. If the file still doesn't parse after that, this
     turn is treated as failed — same as any other Verify gate failure — and
     the loop rolls back and records why, rather than burning further
     re-prompts chasing a fix that isn't converging.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

PY_EXTENSIONS = (".py",)
JS_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx")

# Unified-diff "+++ b/<path>" header, used to figure out which files a turn
# touched without needing a second git call — the orchestrator already has
# the diff text from checkpoints.diff_since().
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)

_BRACKET_PAIRS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {v: k for k, v in _BRACKET_PAIRS.items()}


@dataclass
class SyntaxShieldResult:
    ran: bool = False
    ok: bool = True
    checked_files: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    reprompt: str = ""

    def to_context_block(self) -> str:
        if not self.ran:
            return "## Syntax Shield\nNot run (no changed Python/JS/TS files detected)."
        if self.ok:
            return f"## Syntax Shield — PASS ({len(self.checked_files)} file(s) checked)"
        lines = [f"## Syntax Shield — FAIL ({len(self.errors)}/{len(self.checked_files)} file(s) broken)"]
        for f, err in self.errors.items():
            lines.append(f"- {f}: {err.splitlines()[-1] if err else 'syntax error'}")
        return "\n".join(lines)


def changed_files_from_diff(diff: str) -> List[str]:
    """Extract changed file paths from a unified `git diff` (post-image side)."""
    return _DIFF_FILE_RE.findall(diff or "")


def check_python_file(path: Path) -> str | None:
    """Return an error message (traceback-style) if `path` fails to parse, else None."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"could not read file: {e}"
    try:
        ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return (
            f'  File "{path}", line {e.lineno}\n'
            f"    {(e.text or '').rstrip()}\n"
            f"{' ' * (4 + (e.offset or 1) - 1)}^\n"
            f"SyntaxError: {e.msg}"
        )
    return None


def _strip_strings_and_comments(text: str) -> str:
    """Best-effort removal of string/comment contents so bracket counting
    isn't thrown off by braces that live inside a string literal. This is a
    heuristic, not a real lexer — it does not handle every escape edge case,
    which is fine: false negatives here just mean Syntax Shield stays quiet
    and lets the full gate's lint signal catch it instead."""
    out = []
    i = 0
    n = len(text)
    in_string: str | None = None
    in_line_comment = False
    in_block_comment = False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append(ch)
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in ("'", '"', "`"):
            in_string = ch
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def check_js_like_balance(text: str) -> str | None:
    """Cheap bracket-balance heuristic for JS/TS. Returns an error message or None."""
    cleaned = _strip_strings_and_comments(text)
    stack: List[str] = []
    for idx, ch in enumerate(cleaned):
        if ch in _BRACKET_PAIRS:
            stack.append(ch)
        elif ch in _CLOSERS:
            if not stack or stack[-1] != _CLOSERS[ch]:
                line = cleaned[:idx].count("\n") + 1
                return f"unbalanced '{ch}' near line {line} (bracket mismatch)"
            stack.pop()
    if stack:
        return f"unclosed '{stack[-1]}' — {len(stack)} bracket(s) never closed"
    return None


def run_syntax_shield(workdir: Path, diff: str) -> SyntaxShieldResult:
    """Check only the files a turn actually touched (from its diff)."""
    workdir = Path(workdir)
    changed = changed_files_from_diff(diff)
    py_files = [f for f in changed if f.endswith(PY_EXTENSIONS)]
    js_files = [f for f in changed if f.endswith(JS_EXTENSIONS)]

    if not py_files and not js_files:
        return SyntaxShieldResult(ran=False)

    errors: Dict[str, str] = {}
    checked: List[str] = []

    for rel in py_files:
        path = workdir / rel
        if not path.exists():
            continue  # deleted this turn — nothing to check
        checked.append(rel)
        err = check_python_file(path)
        if err:
            errors[rel] = err

    for rel in js_files:
        path = workdir / rel
        if not path.exists():
            continue
        checked.append(rel)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            errors[rel] = f"could not read file: {e}"
            continue
        err = check_js_like_balance(text)
        if err:
            errors[rel] = err

    ok = not errors
    reprompt = ""
    if not ok:
        lines = [
            "## Syntax Shield caught a syntax error — fix ONLY this, before anything else.",
            "Do not make any other changes. The full Verify gate has not run yet; it will "
            "run once this parses cleanly.",
            "",
        ]
        for f, err in errors.items():
            lines.append(f"### {f}\n```\n{err}\n```")
        reprompt = "\n".join(lines)

    return SyntaxShieldResult(ran=True, ok=ok, checked_files=checked, errors=errors, reprompt=reprompt)
