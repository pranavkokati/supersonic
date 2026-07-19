"""Tavily — optional web research enrichment.

Entirely optional: if no key is configured, TavilyResearch simply isn't
constructed and the loop skips research enrichment rather than failing.
This is unlike the prior generation of this tool, where Tavily was one of
four mandatory sponsor-stack keys required before the loop could run at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from supersonic.config import UserSecrets


@dataclass
class SearchHit:
    title: str
    url: str
    content: str
    score: float = 0.0


@dataclass
class TavilyBundle:
    query: str
    hits: List[SearchHit] = field(default_factory=list)
    answer: Optional[str] = None

    def to_context_block(self) -> str:
        if not self.hits and not self.answer:
            return ""
        lines = [f"## Research: {self.query}"]
        if self.answer:
            lines.append(f"**Synthesis:** {self.answer}")
        for i, h in enumerate(self.hits[:8], 1):
            lines.append(f"\n### [{i}] {h.title}\n{h.url}\n{h.content[:1200]}")
        return "\n".join(lines)


def is_configured(secrets: UserSecrets) -> bool:
    return bool(secrets.tavily_api_key.strip())


class TavilyResearch:
    def __init__(self, secrets: UserSecrets):
        if not is_configured(secrets):
            raise ValueError("Tavily API key not configured — call is_configured() first")
        self.secrets = secrets
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from tavily import TavilyClient

            self._client = TavilyClient(api_key=self.secrets.tavily_api_key)
        return self._client

    def search_ideas(self, seed: str = "") -> TavilyBundle:
        query = seed.strip() or "best developer tool ideas to build in 2026"
        resp = self.client.search(query, search_depth="advanced", max_results=8, include_answer=True)
        hits = [
            SearchHit(title=h.get("title", ""), url=h.get("url", ""), content=h.get("content", ""), score=h.get("score", 0))
            for h in resp.get("results", [])
        ]
        return TavilyBundle(query=query, hits=hits, answer=resp.get("answer"))
