"""DLE — Dependency Mapper: static import scoping, ripgrep + python-walk fallback."""

from __future__ import annotations

import json

from supersonic.loop.dependency_mapper import (
    build_target_graph,
    extract_keywords,
    _scan_with_python_walk,
    _scan_with_ripgrep,
)


def _make_project(tmp_path):
    (tmp_path / "billing").mkdir()
    (tmp_path / "billing" / "invoice.py").write_text(
        "import stripe\nfrom billing.util import fmt\n\ndef charge():\n    pass\n"
    )
    (tmp_path / "billing" / "util.py").write_text("import json\n\ndef fmt(x):\n    return str(x)\n")
    (tmp_path / "landing.py").write_text("import random\n\ndef splash():\n    pass\n")
    return tmp_path


def test_extract_keywords_drops_stopwords_and_short_tokens():
    keywords = extract_keywords("Please fix the billing invoice charge flow for this turn")
    assert "billing" in keywords
    assert "invoice" in keywords
    assert "charge" in keywords
    assert "the" not in keywords
    assert "for" not in keywords


def test_extract_keywords_empty_goal_returns_empty_list():
    assert extract_keywords("") == []
    assert extract_keywords("   ") == []


def test_build_target_graph_scopes_to_goal_keywords(tmp_path):
    _make_project(tmp_path)
    graph = build_target_graph(tmp_path, "fix the billing invoice charge logic", use_cache=False)

    assert any("invoice.py" in f for f in graph.files)
    # landing.py has nothing to do with "billing"/"invoice"/"charge" — should not be pulled in.
    assert not any("landing.py" in f for f in graph.files)


def test_build_target_graph_records_import_edges(tmp_path):
    _make_project(tmp_path)
    graph = build_target_graph(tmp_path, "billing invoice charge", use_cache=False)

    invoice_edges = [e for e in graph.edges if e.src.endswith("invoice.py")]
    targets = {e.target for e in invoice_edges}
    assert "stripe" in targets or "billing.util" in targets


def test_build_target_graph_no_keywords_scopes_to_everything(tmp_path):
    _make_project(tmp_path)
    graph = build_target_graph(tmp_path, "", use_cache=False)
    # With no keywords extracted, every file with an import statement is in scope.
    assert any("invoice.py" in f for f in graph.files)
    assert any("landing.py" in f for f in graph.files)


def test_build_target_graph_writes_and_reuses_cache(tmp_path):
    _make_project(tmp_path)
    goal = "billing invoice charge"
    graph1 = build_target_graph(tmp_path, goal, use_cache=True)

    cache_file = tmp_path / ".dle" / "target_graph.json"
    assert cache_file.exists()
    data = json.loads(cache_file.read_text())
    assert data["goal_keywords"] == graph1.goal_keywords

    # Mutate the file after caching — a cache hit should return the stale
    # (cached) result rather than re-scanning, proving the cache path is used.
    (tmp_path / "billing" / "new_file.py").write_text("import billing\n")
    graph2 = build_target_graph(tmp_path, goal, use_cache=True)
    assert not any("new_file.py" in f for f in graph2.files)


def test_build_target_graph_different_goal_invalidates_cache(tmp_path):
    _make_project(tmp_path)
    build_target_graph(tmp_path, "billing invoice charge", use_cache=True)
    graph2 = build_target_graph(tmp_path, "landing splash page", use_cache=True)
    assert any("landing.py" in f for f in graph2.files)
    assert graph2.goal_keywords != []


def test_python_walk_fallback_finds_same_files_as_ripgrep(tmp_path):
    _make_project(tmp_path)
    rg_result = _scan_with_ripgrep(tmp_path)
    walk_result = _scan_with_python_walk(tmp_path)
    assert set(rg_result.keys()) == set(walk_result.keys())


def test_to_context_block_empty_when_no_files():
    from supersonic.loop.dependency_mapper import TargetGraph

    graph = TargetGraph()
    assert graph.to_context_block() == ""


def test_build_target_graph_never_raises_on_unreadable_tree(tmp_path):
    # Empty dir, no source files at all — should just come back empty, not raise.
    graph = build_target_graph(tmp_path, "anything", use_cache=False)
    assert graph.files == []
