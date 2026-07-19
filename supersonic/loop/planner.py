"""Turn planning — provider-agnostic. One structured call decides what happens next.

Every call here degrades gracefully: a malformed or failed provider response
falls back to a deterministic default rather than crashing the loop. The
planner is a router, not a single point of failure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List

from supersonic.providers.base import LLMProvider, Message, ProviderError

logger = logging.getLogger(__name__)


@dataclass
class ProductBrand:
    product_name: str
    tagline: str
    repo_slug: str

    @classmethod
    def from_idea(cls, idea: str) -> "ProductBrand":
        slug = re.sub(r"[^a-z0-9]+", "-", idea.lower()).strip("-")[:40] or "sonic-build"
        name = (idea.split(".")[0][:40].strip()) or "Untitled Build"
        return cls(product_name=name, tagline="Built with Supersonic", repo_slug=slug)

    def to_context_block(self) -> str:
        if not self.product_name:
            return ""
        return f"## Brand\n- Product: {self.product_name}\n- Tagline: {self.tagline}\n- Repo: {self.repo_slug}"


@dataclass
class TurnPlan:
    done: bool = False
    run_agent: bool = True
    run_qa: bool = True
    follow_up: str = ""
    reason: str = ""

    @classmethod
    def default_continue(cls, turn: int) -> "TurnPlan":
        return cls(
            done=False,
            run_agent=True,
            run_qa=True,
            follow_up="Continue building toward the plan; address any open verification findings first.",
            reason=f"fallback heuristic routing at turn {turn} (provider unavailable or returned unusable output)",
        )


def generate_plan(provider: LLMProvider, idea: str, context_blocks: List[str]) -> str:
    context = "\n\n---\n\n".join(b for b in context_blocks if b.strip())[:8000]
    try:
        result = provider.complete(
            [
                Message(
                    role="system",
                    content="You are a pragmatic tech lead. Produce a tight, numbered MVP build plan: "
                    "4-8 concrete steps, no fluff, no hedging.",
                ),
                Message(role="user", content=f"Idea: {idea}\n\nContext:\n{context}"),
            ],
            max_tokens=800,
            temperature=0.4,
        )
        return result.text.strip() or _fallback_plan(idea)
    except ProviderError:
        logger.exception("plan generation failed, using fallback plan")
        return _fallback_plan(idea)


def _fallback_plan(idea: str) -> str:
    return f"1. Scaffold MVP for: {idea}\n2. README + tests\n3. Core feature\n4. Polish"


def generate_brand(provider: LLMProvider, idea: str, plan: str) -> ProductBrand:
    try:
        data = provider.complete_json(
            [
                Message(
                    role="system",
                    content='Name this product. Return JSON only: '
                    '{"product_name": str, "tagline": str, "repo_slug": str-kebab-case-under-40-chars}.',
                ),
                Message(role="user", content=f"Idea: {idea}\n\nPlan:\n{plan}"),
            ],
            max_tokens=200,
        )
        return ProductBrand(
            product_name=str(data.get("product_name", "")).strip() or idea[:40],
            tagline=str(data.get("tagline", "")).strip(),
            repo_slug=re.sub(r"[^a-z0-9-]", "", str(data.get("repo_slug", "")).lower())[:40] or "sonic-build",
        )
    except ProviderError:
        logger.exception("brand generation failed, using fallback brand")
        return ProductBrand.from_idea(idea)


def generate_turn_plan(
    provider: LLMProvider,
    *,
    idea: str,
    plan: str,
    turn: int,
    continuity_context: str,
    workdir_summary: str,
    verify_context: str,
    last_follow_up: str,
) -> TurnPlan:
    prompt = f"""Idea: {idea}

Build plan:
{plan}

Turn: {turn}
Last follow-up: {last_follow_up}

Continuity Graph (retrieved — invariants and known failures are always included):
{continuity_context[:4000]}

Workdir summary:
{workdir_summary[:2000]}

Latest verification result:
{verify_context[:2000]}

Decide the next turn. Return JSON only:
{{"done": bool, "run_agent": bool, "run_qa": bool, "follow_up": str, "reason": str}}

`done=true` only when the plan's MVP is genuinely complete AND the latest verification passed.
If verification failed, `follow_up` must directly address the failure reason, not just continue the plan blindly.
"""
    try:
        data = provider.complete_json(
            [
                Message(role="system", content="You are the build loop's router. Be decisive. Return valid JSON only."),
                Message(role="user", content=prompt),
            ],
            max_tokens=400,
            temperature=0.3,
        )
        return TurnPlan(
            done=bool(data.get("done", False)),
            run_agent=bool(data.get("run_agent", True)),
            run_qa=bool(data.get("run_qa", True)),
            follow_up=str(data.get("follow_up", "")).strip(),
            reason=str(data.get("reason", "")).strip() or "routed",
        )
    except ProviderError:
        logger.exception("turn planning failed, using fallback continue-plan")
        return TurnPlan.default_continue(turn)
