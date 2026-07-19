"""Continuity Graph — ledger read/write and relevance-ranked retrieval."""

from __future__ import annotations

from supersonic.memory import ContinuityGraph, ContinuityLedger
from supersonic.memory.distill import DISTILL_THRESHOLD, KEEP_RECENT_TURNS, distill, should_distill


def test_ledger_append_and_read_back(tmp_path):
    ledger = ContinuityLedger(tmp_path)
    ledger.record_decision(1, "Chose SQLite", "Simplest storage for a single-user local tool.")
    ledger.record_invariant(0, "Keep the tree buildable", "Every turn must leave a runnable project.")
    ledger.record_failure(3, "Turn 3 broke the build", "Import error in main.py.")

    entries = ledger.all()
    assert len(entries) == 3
    assert {e.kind for e in entries} == {"decision", "invariant", "failure"}


def test_ledger_stats_and_recent(tmp_path):
    ledger = ContinuityLedger(tmp_path)
    for i in range(5):
        ledger.record_decision(i, f"Decision {i}", "body")
    stats = ledger.stats()
    assert stats["total"] == 5
    assert stats["by_kind"]["decision"] == 5
    assert len(ledger.recent(3)) == 3


def test_render_brain_includes_invariants_and_failures(tmp_path):
    ledger = ContinuityLedger(tmp_path)
    ledger.record_invariant(0, "Never delete user data", "Destructive migrations are forbidden.")
    ledger.record_failure(2, "Turn 2 failed", "Tests broke on the auth module.")
    text = ledger.render_brain()
    assert "Never delete user data" in text
    assert "Turn 2 failed" in text
    assert ledger.brain_path.exists()


def test_graph_retrieve_always_includes_invariants_and_failures(tmp_path):
    ledger = ContinuityLedger(tmp_path)
    ledger.record_invariant(0, "Keep API backward compatible", "Do not break v1 routes.")
    ledger.record_failure(2, "Turn 2 regression", "Broke the /health endpoint.")
    ledger.record_decision(1, "Unrelated frontend choice", "Picked a color palette for the landing page.")

    graph = ContinuityGraph(ledger)
    result = graph.retrieve("add a new database migration", token_budget=6000, current_turn=5)

    kinds = {e.kind for e in result.entries}
    assert "invariant" in kinds
    assert "failure" in kinds


def test_graph_retrieve_ranks_relevant_decisions_higher(tmp_path):
    ledger = ContinuityLedger(tmp_path)
    ledger.record_decision(1, "Chose PostgreSQL for the database layer", "Relational data with migrations.")
    ledger.record_decision(1, "Picked a font for the landing page", "Went with a serif typeface.")

    graph = ContinuityGraph(ledger)
    # A tight budget forces the graph to pick only one of the two same-cost entries —
    # it must be the one relevant to the query, not whichever came first.
    result = graph.retrieve("add a database migration for the users table", token_budget=20, current_turn=2)

    assert result.included == 1
    assert "postgresql" in result.entries[0].title.lower() or "database" in result.entries[0].title.lower()


def test_should_distill_respects_threshold_and_recency(tmp_path):
    ledger = ContinuityLedger(tmp_path)
    current_turn = KEEP_RECENT_TURNS + 5
    for i in range(DISTILL_THRESHOLD - 1):
        ledger.record_decision(0, f"Old decision {i}", "body")
    assert should_distill(ledger, current_turn) is False

    ledger.record_decision(0, "One more old decision", "body")
    assert should_distill(ledger, current_turn) is True


def test_distill_folds_old_entries_into_a_lesson(tmp_path):
    ledger = ContinuityLedger(tmp_path)
    current_turn = KEEP_RECENT_TURNS + 5
    for i in range(DISTILL_THRESHOLD):
        ledger.record_decision(0, f"Old decision {i}", f"Detail {i}")

    lesson = distill(ledger, provider=None, current_turn=current_turn)
    assert lesson is not None
    assert lesson.kind == "lesson"

    remaining_decisions = ledger.by_kind("decision")
    assert len(remaining_decisions) == 0
    lessons = ledger.by_kind("lesson")
    assert len(lessons) == 1
