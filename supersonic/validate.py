"""Validate readiness before a live run — provider-agnostic, one working LLM connection required."""

from __future__ import annotations

from supersonic.agents.runner import available_agents
from supersonic.config import AgentKind, UserSecrets
from supersonic.providers import available_providers


class RunValidationError(Exception):
    pass


def validate_live_run(secrets: UserSecrets, agent: AgentKind) -> None:
    problems: list[str] = []

    if not available_providers(secrets):
        problems.append(
            "No LLM provider reachable: set ANTHROPIC_API_KEY or OPENAI_API_KEY, or run a local `ollama serve`"
        )

    agents = {a["id"]: a for a in available_agents()}
    info = agents.get(agent)
    if info and not info.get("available"):
        problems.append(f"'{agent}' coding-agent CLI not found on PATH")

    if problems:
        raise RunValidationError("Not ready to run: " + "; ".join(problems))
