"""Review Risk — ranks a shipped turn's changed files by what a human should
actually look at, using blast-radius, sensitive-path, and test-delta heuristics."""

from __future__ import annotations

from supersonic.verify.dependency_trust import PackageFinding
from supersonic.verify.review_risk import (
    build_review_brief,
    compute_blast_radius,
    _dependency_notes_by_file,
    _has_test_delta,
    _parse_changed_files,
    _score_file,
    _sensitive_hits,
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
