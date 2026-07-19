"""Continuity Graph retrieval — build minimal, high-signal context per turn.

No embeddings, no vector database: a lightweight TF-IDF-style scorer over the
ledger's own text, plus two hard rules that override relevance ranking —
invariants and open failures are *always* included regardless of score,
because silently dropping a constraint is far more expensive than a few
extra tokens of context.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import List, Tuple

from supersonic.memory.ledger import ContinuityLedger
from supersonic.memory.schema import LedgerEntry

_WORD_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")
_STOP = {
    "the", "and", "for", "that", "this", "with", "from", "into", "have",
    "will", "should", "must", "not", "are", "was", "were", "been", "being",
    "turn", "please", "make", "sure", "then", "also", "using", "each",
}


def _tokenize(text: str) -> List[str]:
    return [w.lower() for w in _WORD_RE.findall(text) if w.lower() not in _STOP]


@dataclass
class RetrievalResult:
    entries: List[LedgerEntry]
    context_block: str
    total_available: int
    included: int

    @property
    def compression_ratio(self) -> float:
        if self.total_available == 0:
            return 1.0
        return self.total_available / max(self.included, 1)


class ContinuityGraph:
    """Query surface over a ContinuityLedger."""

    def __init__(self, ledger: ContinuityLedger):
        self.ledger = ledger

    def retrieve(self, query: str, token_budget: int = 6000, current_turn: int = 0) -> RetrievalResult:
        entries = self.ledger.all(include_superseded=False)
        total = len(entries)
        if not entries:
            return RetrievalResult([], "", 0, 0)

        must_include = [e for e in entries if e.kind in ("invariant", "failure")]
        candidates = [e for e in entries if e.kind not in ("invariant", "failure")]

        scored = self._score(query, candidates, current_turn)
        scored.sort(key=lambda pair: pair[1], reverse=True)

        selected = list(must_include)
        budget_chars = token_budget * 4  # ~4 chars/token; good enough without a tokenizer dependency
        used = sum(len(e.body) + len(e.title) for e in selected)

        for entry, _score in scored:
            cost = len(entry.body) + len(entry.title)
            if used + cost > budget_chars:
                continue
            selected.append(entry)
            used += cost

        selected.sort(key=lambda e: e.turn)
        return RetrievalResult(
            entries=selected,
            context_block=self._render(selected),
            total_available=total,
            included=len(selected),
        )

    def _score(self, query: str, entries: List[LedgerEntry], current_turn: int) -> List[Tuple[LedgerEntry, float]]:
        if not entries:
            return []
        query_terms = Counter(_tokenize(query))

        df: Counter = Counter()
        doc_terms = []
        for e in entries:
            terms = set(_tokenize(e.title + " " + e.body + " " + " ".join(e.tags)))
            doc_terms.append(terms)
            df.update(terms)
        n_docs = max(len(entries), 1)

        results = []
        for entry, terms in zip(entries, doc_terms):
            overlap = 0.0
            for term, qcount in query_terms.items():
                if term in terms:
                    idf = math.log((n_docs + 1) / (df[term] + 1)) + 1.0
                    overlap += qcount * idf
            recency = 1.0 / (1.0 + max(0, current_turn - entry.turn) * 0.15)
            score = overlap * 0.7 + recency * 0.2 + entry.importance * 0.1
            results.append((entry, score))
        return results

    def _render(self, entries: List[LedgerEntry]) -> str:
        if not entries:
            return ""
        by_kind: dict = {}
        for e in entries:
            by_kind.setdefault(e.kind, []).append(e)

        section_titles = {
            "invariant": "Invariants (must not violate)",
            "failure": "Known failures (do not repeat)",
            "decision": "Prior decisions",
            "lesson": "Distilled lessons",
            "fact": "Facts",
        }
        lines = ["## Continuity Graph (retrieved context)"]
        for kind in ("invariant", "failure", "decision", "lesson", "fact"):
            group = by_kind.get(kind)
            if not group:
                continue
            lines.append(f"\n### {section_titles[kind]}")
            lines += [f"- (turn {e.turn}) **{e.title}** — {e.body}" for e in group]
        return "\n".join(lines)
