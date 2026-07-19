"""Continuity Graph — structured, retrieval-driven project memory.

Replaces the token-eviction "compress a flat transcript" approach with an
append-only ledger of decisions, invariants, failures, and distilled lessons,
queried per-turn for exactly what's relevant. See ledger.py, graph.py,
distill.py, and schema.py.
"""

from supersonic.memory.distill import distill, should_distill
from supersonic.memory.graph import ContinuityGraph, RetrievalResult
from supersonic.memory.ledger import ContinuityLedger
from supersonic.memory.schema import EntryKind, LedgerEntry, new_entry

__all__ = [
    "ContinuityLedger",
    "ContinuityGraph",
    "RetrievalResult",
    "LedgerEntry",
    "EntryKind",
    "new_entry",
    "distill",
    "should_distill",
]
