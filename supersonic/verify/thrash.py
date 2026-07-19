"""Thrash detector — catches the loop oscillating instead of converging.

Compares each turn's diff against recent turns' diffs with a fast, local
sequence-similarity measure (no LLM call, no dependency). Consistently high
similarity across consecutive turns means the agent is redoing or undoing
the same change rather than making progress — a failure mode a one-turn-
lookahead router has no way to see.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import List


@dataclass
class ThrashVerdict:
    ran: bool = False
    thrashing: bool = False
    similarity: float = 0.0
    window: int = 0

    def to_context_block(self) -> str:
        if not self.ran:
            return "## Thrash detector\nNot enough turn history yet."
        state = "THRASHING" if self.thrashing else "progressing"
        return f"## Thrash detector — {state} (similarity {self.similarity:.2f} over last {self.window} diffs)"


def detect(current_diff: str, recent_diffs: List[str], threshold: float = 0.85) -> ThrashVerdict:
    scored = [prev for prev in recent_diffs if prev.strip()]
    if not current_diff.strip() or not scored:
        return ThrashVerdict(ran=False)
    scores = [difflib.SequenceMatcher(None, current_diff, prev).ratio() for prev in scored]
    avg = sum(scores) / len(scores)
    return ThrashVerdict(ran=True, thrashing=avg >= threshold, similarity=avg, window=len(scores))
