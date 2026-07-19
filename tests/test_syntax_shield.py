"""DLE — Syntax Shield: fast pre-check before the four-signal Verify gate."""

from __future__ import annotations

from supersonic.verify.syntax_shield import (
    changed_files_from_diff,
    check_js_like_balance,
    check_python_file,
    run_syntax_shield,
)

_SAMPLE_DIFF = """diff --git a/app/main.py b/app/main.py
index 111..222 100644
--- a/app/main.py
+++ b/app/main.py
@@ -1,2 +1,3 @@
 def hello():
+    print("hi")
     pass
diff --git a/app/widget.js b/app/widget.js
index 333..444 100644
--- a/app/widget.js
+++ b/app/widget.js
@@ -1,2 +1,3 @@
 function widget() {
+  return true;
 }
"""


def test_changed_files_from_diff_extracts_post_image_paths():
    files = changed_files_from_diff(_SAMPLE_DIFF)
    assert "app/main.py" in files
    assert "app/widget.js" in files


def test_changed_files_from_diff_empty_diff_returns_empty():
    assert changed_files_from_diff("") == []


def test_check_python_file_valid_syntax_returns_none(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text("def hello():\n    return 1\n")
    assert check_python_file(f) is None


def test_check_python_file_invalid_syntax_returns_error(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def hello(:\n    return 1\n")
    err = check_python_file(f)
    assert err is not None
    assert "SyntaxError" in err


def test_check_js_like_balance_valid_returns_none():
    text = "function widget() {\n  if (true) {\n    return 1;\n  }\n}\n"
    assert check_js_like_balance(text) is None


def test_check_js_like_balance_unclosed_brace_detected():
    text = "function widget() {\n  if (true) {\n    return 1;\n"
    err = check_js_like_balance(text)
    assert err is not None
    assert "unclosed" in err


def test_check_js_like_balance_ignores_braces_inside_strings():
    text = 'const s = "{ this looks unbalanced ("; function f() { return s; }\n'
    assert check_js_like_balance(text) is None


def test_run_syntax_shield_not_run_when_no_python_or_js_files_changed():
    diff = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n"
    result = run_syntax_shield("/tmp/doesnotmatter", diff)
    assert result.ran is False
    assert result.ok is True


def test_run_syntax_shield_passes_on_valid_python(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("def hello():\n    return 1\n")
    diff = "diff --git a/app/main.py b/app/main.py\n--- a/app/main.py\n+++ b/app/main.py\n@@ -1 +1 @@\n-x\n+y\n"

    result = run_syntax_shield(tmp_path, diff)

    assert result.ran is True
    assert result.ok is True
    assert result.reprompt == ""


def test_run_syntax_shield_fails_and_builds_reprompt_on_broken_python(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("def hello(:\n    return 1\n")
    diff = "diff --git a/app/main.py b/app/main.py\n--- a/app/main.py\n+++ b/app/main.py\n@@ -1 +1 @@\n-x\n+y\n"

    result = run_syntax_shield(tmp_path, diff)

    assert result.ran is True
    assert result.ok is False
    assert "app/main.py" in result.errors
    assert "Syntax Shield" in result.reprompt
    assert "app/main.py" in result.reprompt


def test_run_syntax_shield_skips_files_deleted_this_turn(tmp_path):
    # File listed in the diff but no longer present (deleted turn) — should not error.
    diff = "diff --git a/app/gone.py b/app/gone.py\n--- a/app/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    result = run_syntax_shield(tmp_path, diff)
    # Nothing to check ends up "ran" with an empty checked list, not a crash.
    assert result.errors == {}
