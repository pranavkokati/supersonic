"""Autosuggest — ranked product ideas before starting a build.

Optional UX nicety, not on the critical path: works with or without Tavily
configured, and routes its one LLM call through the provider abstraction
instead of a hardcoded vendor.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Tuple

from supersonic.config import UserSecrets
from supersonic.providers import get_provider
from supersonic.providers.base import Message, ProviderError
from supersonic.research.tavily import TavilyBundle, TavilyResearch, is_configured
from supersonic.research.web import model_knowledge_bundle

logger = logging.getLogger(__name__)

SUGGEST_SYSTEM = """You propose shippable software products for an autonomous build loop.
Given the research context, output JSON only — an array of exactly 3 ideas:
[
  {
    "title": "short product name",
    "idea": "one-sentence build scope",
    "pitch": "2 sentences: why now + who it's for",
    "score": 0.0-1.0,
    "signals": ["signal 1", "signal 2"]
  }
]

Prefer CLI tools, dev tools, and micro-SaaS a solo builder can ship in under 30 turns.
Score higher when the research cites clear demand and a feasible scope."""


@dataclass
class IdeaSuggestion:
    title: str
    idea: str
    pitch: str = ""
    score: float = 0.5
    signals: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "idea": self.idea,
            "pitch": self.pitch,
            "score": round(self.score, 2),
            "signals": self.signals,
        }


def _parse_suggestions(raw: str) -> List[IdeaSuggestion]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[[\s\S]*\]", text)
        if not m:
            return []
        data = json.loads(m.group())
    if not isinstance(data, list):
        return []
    out: List[IdeaSuggestion] = []
    for item in data[:5]:
        if not isinstance(item, dict):
            continue
        out.append(
            IdeaSuggestion(
                title=str(item.get("title") or "Untitled")[:80],
                idea=str(item.get("idea") or "")[:500],
                pitch=str(item.get("pitch") or "")[:600],
                score=min(1.0, max(0.0, float(item.get("score") or 0.5))),
                signals=[str(s)[:120] for s in (item.get("signals") or [])[:4] if s],
            )
        )
    return out


def _fallback_suggestions(seed: str) -> List[IdeaSuggestion]:
    base = seed.strip() or "developer automation"
    return [
        IdeaSuggestion(
            title="ShipCLI",
            idea=f"A CLI that automates repetitive workflows around {base}",
            pitch="Developers waste hours on glue scripts. A focused CLI ships fast with clear GitHub distribution.",
            score=0.8,
            signals=["Dev tooling demand", "CLI distribution"],
        ),
        IdeaSuggestion(
            title="LocalSync",
            idea=f"Offline-first sync layer for teams working on {base}",
            pitch="Remote teams need local-first tools that sync when online. Buildable end-to-end in one run.",
            score=0.72,
            signals=["Local-first trend", "Low infra cost"],
        ),
        IdeaSuggestion(
            title="AuditKit",
            idea=f"Automated audit reports and dashboards for {base} metrics",
            pitch="Compliance and observability budgets are growing; a read-only audit CLI fits a short build loop.",
            score=0.65,
            signals=["Observability spend", "Report automation"],
        ),
    ]


def suggest_products(secrets: UserSecrets, seed: str = "", *, count: int = 3) -> Tuple[List[IdeaSuggestion], TavilyBundle]:
    if is_configured(secrets):
        bundle = TavilyResearch(secrets).search_ideas(seed)
    else:
        try:
            provider = get_provider(secrets)
        except ProviderError:
            provider = None
        bundle = model_knowledge_bundle(provider, seed)

    try:
        provider = get_provider(secrets)
        data = provider.complete_json(
            [
                Message(role="system", content=SUGGEST_SYSTEM),
                Message(
                    role="user",
                    content=f"Seed topic: {seed or '(open — pick from research)'}\n\nResearch:\n{bundle.to_context_block()[:8000]}",
                ),
            ],
            max_tokens=900,
        )
        ideas = _parse_suggestions(json.dumps(data)) if isinstance(data, list) else []
        if not ideas and isinstance(data, dict) and "ideas" in data:
            ideas = _parse_suggestions(json.dumps(data["ideas"]))
    except (ProviderError, Exception):
        logger.exception("suggest_products failed, using fallback suggestions")
        ideas = []

    if not ideas:
        ideas = _fallback_suggestions(seed)
    return ideas[:count], bundle
