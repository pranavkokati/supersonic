"""Test Quality Gate — diff-to-function mapping, mutant generation/application,
and an end-to-end run against a real (tiny) project in a tmp_path, exercising
both a weak test (mutant survives) and a real test (mutant killed).

`_detect_test_command` is monkeypatched to `python3 -m pytest -q` in the
end-to-end tests: this sandbox doesn't have a standalone `pytest` executable
on PATH (same pre-existing environment limitation noted in test_verify.py),
but the `pytest` package itself is importable, so `python3 -m pytest` works
and exercises the real subprocess path this module actually uses in
production.
"""

from __future__ import annotations

import supersonic.verify.test_quality as tq
from supersonic.verify.test_quality import (
    TestQualityVerdict,
    _apply_mutation,
    _changed_line_numbers,
    _mutation_candidates,
    _touched_functions,
    run_test_quality_gate,
)

_UTIL_SOURCE = "def is_even(n):\n    return n % 2 == 0\n"

_ADD_TWO_LINE_DIFF = (
    "diff --git a/pkg/util.py b/pkg/util.py\n"
    "index 111..222 100644\n"
    "--- a/pkg/util.py\n"
    "+++ b/pkg/util.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def is_even(n):\n"
    "+    return n % 2 == 0\n"
)


# --------------------------------------------------------------------------
# Diff -> line numbers
# --------------------------------------------------------------------------

def test_changed_line_numbers_maps_added_lines_to_new_file_numbers():
    changed = _changed_line_numbers(_ADD_TWO_LINE_DIFF)
    assert changed["pkg/util.py"] == {1, 2}


def test_changed_line_numbers_empty_diff_is_empty():
    assert _changed_line_numbers("") == {}


def test_changed_line_numbers_ignores_removed_lines():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ a/a.py\n"
        "@@ -1,3 +1,3 @@\n"
        " kept_line\n"
        "-removed_line\n"
        "+added_line\n"
        " trailing\n"
    )
    changed = _changed_line_numbers(diff)
    # Only the one truly-added line should be recorded (line 2 in the new file).
    assert changed["a.py"] == {2}


# --------------------------------------------------------------------------
# Function mapping and mutation candidate discovery
# --------------------------------------------------------------------------

def test_touched_functions_finds_function_overlapping_added_lines():
    funcs = _touched_functions(_UTIL_SOURCE, {1, 2})
    assert len(funcs) == 1
    assert funcs[0].name == "is_even"


def test_touched_functions_empty_when_added_lines_dont_overlap():
    source = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    funcs = _touched_functions(source, {100})
    assert funcs == []


def test_touched_functions_handles_syntax_error_gracefully():
    assert _touched_functions("def broken(:\n", {1}) == []


def test_mutation_candidates_finds_compare_flip():
    funcs = _touched_functions(_UTIL_SOURCE, {1, 2})
    candidates = _mutation_candidates(funcs[0])
    kinds = [c[0] for c in candidates]
    assert "compare" in kinds


def test_mutation_candidates_finds_boolop_flip():
    source = "def f(a, b):\n    return a and b\n"
    funcs = _touched_functions(source, {1, 2})
    candidates = _mutation_candidates(funcs[0])
    assert any(c[0] == "boolop" for c in candidates)


def test_mutation_candidates_finds_boolean_constant_flip():
    source = "def f():\n    return True\n"
    funcs = _touched_functions(source, {1, 2})
    candidates = _mutation_candidates(funcs[0])
    assert any(c[0] == "constant" for c in candidates)


def test_mutation_candidates_capped_at_max_per_function():
    # Six independent comparisons in one function — candidates must be capped.
    lines = "\n".join(f"    if x{i} == {i}: pass" for i in range(6))
    source = f"def f(x0, x1, x2, x3, x4, x5):\n{lines}\n"
    funcs = _touched_functions(source, set(range(1, 8)))
    candidates = _mutation_candidates(funcs[0])
    assert len(candidates) <= tq.MAX_MUTANTS_PER_FUNCTION


# --------------------------------------------------------------------------
# Applying a mutation — position-based, not identity-based
# --------------------------------------------------------------------------

def test_apply_mutation_flips_compare_operator():
    funcs = _touched_functions(_UTIL_SOURCE, {1, 2})
    kind, lineno, col_offset, _desc, new_val = _mutation_candidates(funcs[0])[0]
    mutated = _apply_mutation(_UTIL_SOURCE, kind, lineno, col_offset, new_val)
    assert mutated is not None
    assert "!=" in mutated
    assert "==" not in mutated


def test_apply_mutation_returns_none_for_unmatched_position():
    mutated = _apply_mutation(_UTIL_SOURCE, "compare", 999, 999, tq.ast.NotEq)
    assert mutated is None


# --------------------------------------------------------------------------
# End-to-end run_test_quality_gate against a real tmp_path project
# --------------------------------------------------------------------------

def _write_project(tmp_path, test_body: str):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "util.py").write_text(_UTIL_SOURCE)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_util.py").write_text(test_body)


def test_run_test_quality_gate_no_test_command_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(tq, "_detect_test_command", lambda workdir: None)
    verdict = run_test_quality_gate(tmp_path, _ADD_TWO_LINE_DIFF)
    assert verdict.ran is False


def test_run_test_quality_gate_no_python_files_touched_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(tq, "_detect_test_command", lambda workdir: ["python3", "-m", "pytest", "-q"])
    diff = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -0,0 +1,1 @@\n+hello\n"
    verdict = run_test_quality_gate(tmp_path, diff)
    assert verdict.ran is False


def test_run_test_quality_gate_flags_weak_test_as_surviving(tmp_path, monkeypatch):
    _write_project(tmp_path, "from pkg.util import is_even\n\ndef test_is_even():\n    is_even(4)\n")
    monkeypatch.setattr(tq, "_detect_test_command", lambda workdir: ["python3", "-m", "pytest", "-q"])

    verdict = run_test_quality_gate(tmp_path, _ADD_TWO_LINE_DIFF)

    assert verdict.ran is True
    assert verdict.mutants_generated >= 1
    assert verdict.mutants_killed == 0
    assert verdict.passed is False
    assert len(verdict.survivors) >= 1
    assert verdict.survivors[0].function == "is_even"
    # The real file on disk must be restored to its original content.
    assert (tmp_path / "pkg" / "util.py").read_text() == _UTIL_SOURCE


def test_run_test_quality_gate_passes_when_test_actually_asserts(tmp_path, monkeypatch):
    _write_project(
        tmp_path,
        "from pkg.util import is_even\n\n"
        "def test_is_even():\n"
        "    assert is_even(4) is True\n"
        "    assert is_even(3) is False\n",
    )
    monkeypatch.setattr(tq, "_detect_test_command", lambda workdir: ["python3", "-m", "pytest", "-q"])

    verdict = run_test_quality_gate(tmp_path, _ADD_TWO_LINE_DIFF)

    assert verdict.ran is True
    assert verdict.mutants_killed >= 1
    assert verdict.passed is True
    assert verdict.survivors == []
    assert (tmp_path / "pkg" / "util.py").read_text() == _UTIL_SOURCE


def test_verdict_kill_rate_none_when_no_mutants():
    verdict = TestQualityVerdict(ran=True, mutants_generated=0, mutants_killed=0)
    assert verdict.kill_rate is None


def test_verdict_to_context_block_not_run_mentions_reason():
    verdict = TestQualityVerdict(ran=False, skipped_reason="no test command detected")
    assert "no test command detected" in verdict.to_context_block()
