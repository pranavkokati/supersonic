"""Review Risk — ranks a shipped turn's changed files by what a human should
actually look at, using blast-radius, sensitive-path, and test-delta heuristics."""

from __future__ import annotations

from supersonic.verify.dependency_trust import PackageFinding
from supersonic.verify.secret_leak import SecretFinding
from supersonic.verify.test_quality import MutationFinding
from supersonic.verify.review_risk import (
    build_review_brief,
    compute_blast_radius,
    _dependency_notes_by_file,
    _has_test_delta,
    _parse_changed_files,
    _score_file,
    _secret_notes_by_file,
    _sensitive_hits,
    _test_quality_notes_by_file,
)

_TWO_FILE_DIFF = """diff --git a/app/auth/session.py b/app/auth/session.py
index 111..222 100644
--- a/app/auth/session.py
+++ b/app/auth/session.py
@@ -1,2 +1,4 @@
 def check_session(token):
+    if not token:
+        return False
     return True
diff --git a/app/styles/theme.css b/app/styles/theme.css
index 333..444 100644
--- a/app/styles/theme.css
+++ b/app/styles/theme.css
@@ -1,1 +1,2 @@
 body { color: black; }
+.btn { color: blue; }
"""


def test_parse_changed_files_extracts_both_paths():
    blocks = _parse_changed_files(_TWO_FILE_DIFF)
    assert set(blocks.keys()) == {"app/auth/session.py", "app/styles/theme.css"}


def test_parse_changed_files_empty_diff_returns_empty():
    assert _parse_changed_files("") == {}


def test_sensitive_hits_detects_auth_keyword_in_path():
    hits = _sensitive_hits("app/auth/session.py", "if not token:\n    return False")
    assert "auth" in hits or any("auth" in h for h in hits)


def test_sensitive_hits_empty_for_plain_css():
    hits = _sensitive_hits("app/styles/theme.css", ".btn { color: blue; }")
    assert hits == []


def test_has_test_delta_true_when_matching_test_file_present():
    changed = ["app/auth/session.py", "tests/test_session.py"]
    assert _has_test_delta("app/auth/session.py", changed) is True


def test_has_test_delta_false_when_no_matching_test_file():
    changed = ["app/auth/session.py", "app/styles/theme.css"]
    assert _has_test_delta("app/auth/session.py", changed) is False


def test_has_test_delta_true_for_the_test_file_itself():
    changed = ["tests/test_session.py"]
    assert _has_test_delta("tests/test_session.py", changed) is True


def test_score_file_sensitive_no_test_delta_scores_high():
    risk = _score_file("app/auth/session.py", added=2, removed=0, blast_radius=0,
                        sensitive_hits=["auth"], has_test_delta=False)
    assert risk.level in ("high", "medium")
    assert risk.score >= 5 or risk.level == "medium"
    assert any("sensitive" in r for r in risk.reasons)
    assert any("no corresponding test" in r for r in risk.reasons)


def test_score_file_plain_css_with_no_signals_scores_low():
    risk = _score_file("app/styles/theme.css", added=1, removed=0, blast_radius=0,
                        sensitive_hits=[], has_test_delta=True)
    assert risk.level == "low"
    assert risk.score <= 1


def test_score_file_high_blast_radius_alone_raises_score():
    risk = _score_file("supersonic/config.py", added=1, removed=0, blast_radius=15,
                        sensitive_hits=[], has_test_delta=True)
    assert risk.blast_radius == 15
    assert risk.score >= 3


def test_compute_blast_radius_counts_reverse_references(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "utils.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "pkg" / "a.py").write_text("from pkg.utils import helper\n")
    (tmp_path / "pkg" / "b.py").write_text("from pkg.utils import helper\n")
    (tmp_path / "pkg" / "unrelated.py").write_text("print('hello')\n")

    radius = compute_blast_radius(tmp_path, ["pkg/utils.py"])

    assert radius["pkg/utils.py"] == 2


def test_compute_blast_radius_zero_when_nothing_references_it(tmp_path):
    (tmp_path / "lonely.py").write_text("x = 1\n")
    radius = compute_blast_radius(tmp_path, ["lonely.py"])
    assert radius["lonely.py"] == 0


def test_compute_blast_radius_python_ignores_substring_in_comment_not_a_real_import(tmp_path):
    # The old substring-containment heuristic would have counted this as a
    # reference (the word "utils" appears in the text); real AST-based
    # import resolution must not, since there's no actual import statement.
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "utils.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "pkg" / "unrelated.py").write_text("# this file has nothing to do with pkg.utils, just mentions it\nx = 1\n")

    radius = compute_blast_radius(tmp_path, ["pkg/utils.py"])
    assert radius["pkg/utils.py"] == 0


def test_compute_blast_radius_python_resolves_relative_import(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "utils.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "pkg" / "caller.py").write_text("from . import utils\nutils.helper()\n")

    radius = compute_blast_radius(tmp_path, ["pkg/utils.py"])
    assert radius["pkg/utils.py"] == 1


def test_compute_blast_radius_python_resolves_parent_relative_import(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "sub").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "target.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "pkg" / "sub" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "sub" / "caller.py").write_text("from ..target import helper\nhelper()\n")

    radius = compute_blast_radius(tmp_path, ["pkg/target.py"])
    assert radius["pkg/target.py"] == 1


def test_compute_blast_radius_python_named_import_from_target_counts(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "utils.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "pkg" / "caller.py").write_text("from pkg.utils import helper\nhelper()\n")

    radius = compute_blast_radius(tmp_path, ["pkg/utils.py"])
    assert radius["pkg/utils.py"] == 1


def test_compute_blast_radius_python_star_import_requires_actual_usage(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "utils.py").write_text("def helper():\n    return 1\n\ndef other():\n    return 2\n")
    (tmp_path / "pkg" / "star_user.py").write_text("from pkg.utils import *\nhelper()\n")
    (tmp_path / "pkg" / "star_ignorer.py").write_text("from pkg.utils import *\n# never actually calls anything from it\nx = 1\n")

    radius = compute_blast_radius(tmp_path, ["pkg/utils.py"])
    assert radius["pkg/utils.py"] == 1  # only star_user.py actually uses a defined name


def test_compute_blast_radius_python_init_file_dotted_path_is_package_name(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("VERSION = '1.0'\n")
    (tmp_path / "caller.py").write_text("import pkg\nprint(pkg.VERSION)\n")

    radius = compute_blast_radius(tmp_path, ["pkg/__init__.py"])
    assert radius["pkg/__init__.py"] == 1


def test_compute_blast_radius_python_skips_unparsable_candidate_file(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "utils.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "pkg" / "broken.py").write_text("def not valid python (((\n")
    (tmp_path / "pkg" / "caller.py").write_text("from pkg.utils import helper\nhelper()\n")

    # Must not raise despite the syntactically broken candidate file, and
    # must still count the valid importer.
    radius = compute_blast_radius(tmp_path, ["pkg/utils.py"])
    assert radius["pkg/utils.py"] == 1


def test_build_review_brief_ranks_sensitive_file_above_css(tmp_path):
    brief = build_review_brief(tmp_path, turn=3, diff=_TWO_FILE_DIFF)

    assert len(brief.items) == 2
    assert brief.items[0].path == "app/auth/session.py"
    assert brief.items[0].score >= brief.items[1].score


def test_build_review_brief_empty_diff_returns_no_items():
    brief = build_review_brief(workdir="/tmp/doesnotmatter", turn=1, diff="")
    assert brief.items == []
    assert brief.high_count == 0
    assert "No changed files" in brief.summary_line()


def test_review_brief_summary_line_mentions_top_high_risk_file():
    brief = build_review_brief(workdir="/tmp/doesnotmatter", turn=1, diff=_TWO_FILE_DIFF)
    summary = brief.summary_line()
    assert "file(s)" in summary or "risk" in summary


def test_review_brief_to_dict_has_counts_and_items():
    brief = build_review_brief(workdir="/tmp/doesnotmatter", turn=5, diff=_TWO_FILE_DIFF)
    d = brief.to_dict()
    assert d["turn"] == 5
    assert "high_count" in d and "medium_count" in d and "low_count" in d
    assert isinstance(d["items"], list) and len(d["items"]) == 2


def test_dependency_notes_by_file_maps_suspicious_finding_to_its_manifest():
    finding = PackageFinding(
        name="brand-new-pkg", ecosystem="pypi", manifest="requirements.txt",
        verdict="suspicious", age_days=2, release_count=1,
    )
    notes = _dependency_notes_by_file([finding])
    assert "requirements.txt" in notes
    assert "brand-new-pkg" in notes["requirements.txt"][0]


def test_dependency_notes_by_file_ignores_trusted_findings():
    finding = PackageFinding(name="requests", ecosystem="pypi", manifest="requirements.txt", verdict="trusted")
    assert _dependency_notes_by_file([finding]) == {}


def test_build_review_brief_dependency_finding_forces_css_file_to_score_high(tmp_path):
    # The CSS-only diff normally scores "low" (see
    # test_score_file_plain_css_with_no_signals_scores_low); attributing an
    # unverified dependency finding to that same file must push it up.
    css_only_diff = """diff --git a/app/styles/theme.css b/app/styles/theme.css
index 333..444 100644
--- a/app/styles/theme.css
+++ b/app/styles/theme.css
@@ -1,1 +1,2 @@
 body { color: black; }
+.btn { color: blue; }
"""
    finding = PackageFinding(
        name="sketchy-pkg", ecosystem="pypi", manifest="app/styles/theme.css",
        verdict="suspicious", age_days=1, release_count=1,
    )
    brief = build_review_brief(tmp_path, turn=1, diff=css_only_diff, dependency_findings=[finding])
    assert brief.items[0].level in ("medium", "high")
    assert any("unverified dependency" in r for r in brief.items[0].reasons)


def test_secret_notes_by_file_maps_suspicious_finding_to_its_path():
    finding = SecretFinding(path="app/settings.py", kind="credential-shaped assignment", verdict="suspicious", line_excerpt="aB3f…4x (generic)")
    notes = _secret_notes_by_file([finding])
    assert "app/settings.py" in notes
    assert "possible hardcoded credential" in notes["app/settings.py"][0]


def test_secret_notes_by_file_ignores_critical_findings():
    # A `critical` finding fails the Verify gate outright (verify/gate.py), so
    # it should never actually reach a shipped-turn Review Brief in practice —
    # but the mapper itself should still only surface `suspicious` on purpose.
    finding = SecretFinding(path="app/config.py", kind="AWS access key ID", verdict="critical")
    assert _secret_notes_by_file([finding]) == {}


def test_build_review_brief_secret_finding_forces_css_file_to_score_high(tmp_path):
    # Same shape as the dependency-finding test above: a CSS-only diff
    # normally scores "low"; attributing a suspicious secret finding to that
    # same file must push it up.
    css_only_diff = """diff --git a/app/styles/theme.css b/app/styles/theme.css
index 333..444 100644
--- a/app/styles/theme.css
+++ b/app/styles/theme.css
@@ -1,1 +1,2 @@
 body { color: black; }
+.btn { color: blue; }
"""
    finding = SecretFinding(path="app/styles/theme.css", kind="credential-shaped assignment", verdict="suspicious", line_excerpt="aB3f…4x (generic)")
    brief = build_review_brief(tmp_path, turn=1, diff=css_only_diff, secret_findings=[finding])
    assert brief.items[0].level in ("medium", "high")
    assert any("possible hardcoded credential" in r for r in brief.items[0].reasons)


def test_test_quality_notes_by_file_maps_surviving_mutant_to_its_path():
    finding = MutationFinding(path="pkg/util.py", function="is_even", mutation="Eq -> NotEq", survived=True)
    notes = _test_quality_notes_by_file([finding])
    assert "pkg/util.py" in notes
    assert "weak test coverage in is_even()" in notes["pkg/util.py"][0]


def test_test_quality_notes_by_file_ignores_killed_mutants():
    finding = MutationFinding(path="pkg/util.py", function="is_even", mutation="Eq -> NotEq", survived=False)
    assert _test_quality_notes_by_file([finding]) == {}


def test_build_review_brief_test_quality_finding_forces_css_file_to_score_high(tmp_path):
    # Same shape as the dependency/secret tests above: a CSS-only diff
    # normally scores "low"; attributing a surviving mutant to that same
    # file must push it up — this is the one signal that can flag a file
    # that otherwise looks completely clean.
    css_only_diff = """diff --git a/app/styles/theme.css b/app/styles/theme.css
index 333..444 100644
--- a/app/styles/theme.css
+++ b/app/styles/theme.css
@@ -1,1 +1,2 @@
 body { color: black; }
+.btn { color: blue; }
"""
    finding = MutationFinding(path="app/styles/theme.css", function="paint", mutation="True -> False", survived=True)
    brief = build_review_brief(tmp_path, turn=1, diff=css_only_diff, test_quality_findings=[finding])
    assert brief.items[0].level in ("medium", "high")
    assert any("weak test coverage" in r for r in brief.items[0].reasons)
