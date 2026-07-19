"""Common shape for optional research providers (currently just Tavily)."""

from __future__ import annotations

from typing import Protocol

from supersonic.research.tavily import TavilyBundle


class ResearchProvider(Protocol):
    def search_ideas(self, seed: str = "") -> TavilyBundle: ...
