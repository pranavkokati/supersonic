"""Local configuration for Supersonic.

Design goal: work with a single API key. No forced multi-vendor sponsor
stack, no mandatory issue tracker, no mandatory notification channel.
Everything beyond one LLM provider key is optional and additive.
"""

from __future__ import annotations

import json
import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path.home() / ".supersonic"
CONFIG_FILE = CONFIG_DIR / "config.json"
PROJECTS_DIR = CONFIG_DIR / "projects"

AgentKind = Literal["claude", "codex", "cursor", "opencode", "aider"]
ProviderKind = Literal["anthropic", "openai", "ollama"]


class UserSecrets(BaseModel):
    """Everything Supersonic needs. Only one LLM key is required to run."""

    # LLM providers — auto-detected in this priority order unless `preferred_provider` is set.
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    preferred_provider: str = ""  # "" = auto-detect

    # Coding agent backend(s). `race_agents` (>=2) enables Bandit-Gated Agent Racing.
    default_agent: AgentKind = "claude"
    race_agents: list[str] = Field(default_factory=list)
    race_enabled: bool = False
    max_race_turns: int = 5
    race_challenger_turn_cap: int = 6  # tool-call budget cap for the losing/challenger worktree

    # Shipping — native git + gh CLI, no middleman.
    ship_mode: Literal["push", "pr"] = "pr"
    github_owner: str = ""
    github_repo: str = ""

    # Optional plugins — off unless a key/flag is present.
    linear_api_key: str = ""
    linear_team_id: str = ""
    notify_webhook_url: str = ""
    notify_email_to: str = ""
    tavily_api_key: str = ""  # optional research enrichment only

    webhook_url: str = ""
    webhook_secret: str = ""

    # Scheduler / portfolio queue
    schedule_enabled: bool = False
    schedule_interval_hours: int = 24

    # Loop tuning
    ledger_context_budget: int = 6000  # max tokens of retrieved Continuity Graph context per turn
    max_turn_budget: int = 60
    # Of the (up to 4) verification signals that actually ran, how many must pass.
    # Floored at 1 — a value of 0 would make every turn pass unconditionally,
    # silently defeating the entire Verify gate.
    verify_min_signals_pass: int = Field(default=3, ge=1, le=4)

    model_config = ConfigDict(extra="ignore")

    def schedule_state_file(self) -> Path:
        return CONFIG_DIR / "schedule_state.json"

    def configured_providers(self) -> list[str]:
        names = []
        if self.anthropic_api_key.strip():
            names.append("anthropic")
        if self.openai_api_key.strip():
            names.append("openai")
        names.append("ollama")  # always attempted last; free if a local server is running
        return names


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    sonic_host: str = "127.0.0.1"
    sonic_port: int = 8787
    sonic_demo: bool = Field(default=False, validation_alias="SONIC_DEMO")


def _migrate_legacy_data() -> None:
    if CONFIG_DIR.exists():
        return
    for legacy_name in (".loopy", ".software-factory"):
        legacy = Path.home() / legacy_name
        if legacy.exists():
            try:
                shutil.copytree(legacy, CONFIG_DIR)
            except Exception:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            return


def load_secrets() -> UserSecrets:
    ensure_dirs()
    if CONFIG_FILE.exists():
        data = json.loads(CONFIG_FILE.read_text())
        return UserSecrets.model_validate(data)
    return UserSecrets()


def save_secrets(secrets: UserSecrets) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(secrets.model_dump_json(indent=2))


def ensure_dirs() -> None:
    _migrate_legacy_data()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if os.environ.get("SONIC_DEMO") == "1":
        return s.model_copy(update={"sonic_demo": True})
    return s
