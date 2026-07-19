"""Generic fallback research — a single cheap LLM call when no search API key is configured.

Not a web search. It asks the configured LLM provider for its own best
understanding of the space, clearly labeled as such, so the planner still
gets *something* to ground the initial plan on even with zero research keys.
"""

from __future__ import annotations

import logging
from typing import Optional

from supersonic.providers.base import LLMProvider, Message, ProviderError
from supersonic.research.tavily import TavilyBundle

logger = logging.getLogger(__name__)


def model_knowledge_bundle(provider: Optional[LLMProvider], seed: str) -> TavilyBundle:
    if provider is None or not seed.strip():
        return TavilyBundle(query=seed, hits=[], answer=None)
    try:
        result = provider.complete(
            [
                Message(
                    role="system",
                    content="In 3-4 sentences, summarize the competitive/product landscape for this idea "
                    "from what you already know. Be concrete. Note this is not live search.",
                ),
                Message(role="user", content=seed),
            ],
            max_tokens=300,
            temperature=0.3,
        )
        return TavilyBundle(query=seed, hits=[], answer=result.text.strip())
    except ProviderError:
        logger.exception("model-knowledge fallback research failed")
        return TavilyBundle(query=seed, hits=[], answer=None)
