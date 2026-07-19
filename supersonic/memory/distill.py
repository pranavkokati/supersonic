"""Distillation — compact old, low-relevance ledger entries into lesson nodes.

Keeps the ledger bounded without ever silently discarding information:
invariants and unresolved failures are never touched by distillation
(compressing away a constraint is exactly the failure mode this system
exists to prevent). Only aged decisions/facts below the recency window get
folded into a single summarizing lesson entry via one LLM call — or, if no
provider is available, a deterministic truncated concatenation so the system
degrades gracefully rather than failing.
"""

from __future__ import annotations

import logging
from typing import Optional

from supersonic.memory.ledger import ContinuityLedger
from supersonic.memory.schema import LedgerEntry, new_entry
from supersonic.providers.base import LLMProvider, Message

logger = logging.getLogger(__name__)

DISTILL_THRESHOLD = 40   # distill once undistilled decision/fact entries exceed this count
KEEP_RECENT_TURNS = 5    # never distill entries from the last N turns


def _stale_candidates(ledger: ContinuityLedger, current_turn: int):
    return [
        e
        for e in ledger.by_kind("decision") + ledger.by_kind("fact")
        if current_turn - e.turn > KEEP_RECENT_TURNS
    ]


def should_distill(ledger: ContinuityLedger, current_turn: int) -> bool:
    return len(_stale_candidates(ledger, current_turn)) >= DISTILL_THRESHOLD


def distill(
    ledger: ContinuityLedger, provider: Optional[LLMProvider], current_turn: int
) -> Optional[LedgerEntry]:
    """Fold aged decisions/facts into one lesson entry. Returns the new lesson, or None."""
    candidates = _stale_candidates(ledger, current_turn)
    if len(candidates) < DISTILL_THRESHOLD:
        return None

    bullet_list = "\n".join(f"- (turn {e.turn}) {e.title}: {e.body}" for e in candidates)
    body = bullet_list[:1500]

    if provider is not None:
        try:
            result = provider.complete(
                [
                    Message(
                        role="system",
                        content=(
                            "Compress the following project decisions into one dense paragraph a "
                            "future build turn can use as working context. Keep concrete facts "
                            "(names, file paths, chosen approaches). Drop anything redundant. "
                            "Plain text, no markdown, under 200 words."
                        ),
                    ),
                    Message(role="user", content=bullet_list),
                ],
                max_tokens=400,
                temperature=0.2,
            )
            if result.text.strip():
                body = result.text.strip()
        except Exception:
            logger.exception("distillation LLM call failed, falling back to truncated concatenation")

    lesson = new_entry(
        "lesson",
        current_turn,
        title=f"Distilled summary of {len(candidates)} entries (through turn {candidates[-1].turn})",
        body=body,
        tags=["distilled"],
        importance=0.6,
        source="loop",
    )

    candidate_ids = {e.id for e in candidates}
    remaining = [e for e in ledger.all() if e.id not in candidate_ids]
    remaining.append(lesson)
    ledger.replace_all(remaining)
    ledger.render_brain()
    return lesson
