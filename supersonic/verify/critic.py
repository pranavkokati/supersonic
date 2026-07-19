"""Goal-satisfaction critic — does this turn's diff actually do what it was asked?

A test suite can pass while completely missing the point of the turn. This
signal exists specifically to catch that: an LLM call comparing the diff
against the stated goal and the ledger's recorded invariants.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from supersonic.providers.base import LLMProvider, Message, ProviderError, parse_json_loose

logger = logging.getLogger(__name__)


@dataclass
class CriticVerdict:
    ran: bool = False
    satisfied: bool = True  # fail-open: no critic available should never itself fail a turn
    confidence: float = 0.0
    reasoning: str = ""
    violated_invariants: List[str] = field(default_factory=list)

    def to_context_block(self) -> str:
        if not self.ran:
            return "## Goal critic\nNot run (no provider available)."
        verdict = "SATISFIED" if self.satisfied else "NOT SATISFIED"
        lines = [f"## Goal critic — {verdict} (confidence {self.confidence:.2f})", self.reasoning]
        if self.violated_invariants:
            lines.append("Violated invariants: " + ", ".join(self.violated_invariants))
        return "\n".join(lines)


def judge(provider: Optional[LLMProvider], *, goal: str, diff: str, invariants: List[str]) -> CriticVerdict:
    if provider is None:
        return CriticVerdict(ran=False)
    if not diff.strip():
        return CriticVerdict(ran=True, satisfied=False, confidence=1.0, reasoning="No changes were made this turn.")

    invariant_text = "\n".join(f"- {i}" for i in invariants) or "(none recorded yet)"
    prompt = f"""Turn goal:
{goal}

Invariants that must not be violated:
{invariant_text}

Diff (truncated):
{diff[:6000]}

Judge this diff against the goal above. Return JSON only:
{{"satisfied": bool, "confidence": number 0-1, "reasoning": string under 80 words, "violated_invariants": [string]}}
"""
    try:
        result = provider.complete(
            [
                Message(
                    role="system",
                    content="You are a strict code reviewer judging intent-match, not style. Return valid JSON only.",
                ),
                Message(role="user", content=prompt),
            ],
            model=provider.fast_model or provider.default_model,
            max_tokens=350,
            temperature=0.0,
            json_mode=True,
        )
        parsed = parse_json_loose(result.text)
        return CriticVerdict(
            ran=True,
            satisfied=bool(parsed.get("satisfied", True)),
            confidence=float(parsed.get("confidence", 0.5) or 0.5),
            reasoning=str(parsed.get("reasoning", "")).strip(),
            violated_invariants=[str(x) for x in (parsed.get("violated_invariants") or [])],
        )
    except (ProviderError, ValueError, TypeError):
        logger.exception("critic judgement failed, failing open")
        return CriticVerdict(ran=False, satisfied=True, reasoning="Critic unavailable this turn.")
