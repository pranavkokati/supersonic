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

    # Coding agent backend.
    default_agent: AgentKind = "claude"

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
    # Of the (up to 4, or 5 with the DLE telemetry gate enabled) verification
    # signals that actually ran, how many must pass. Floored at 1 — a value
    # of 0 would make every turn pass unconditionally, silently defeating the
    # entire Verify gate.
    verify_min_signals_pass: int = Field(default=3, ge=1, le=5)

    # --- Deterministic Loop Engine (DLE) toggles ---------------------------
    # Static import-graph scoping hint injected into each turn's prompt.
    # Cheap (regex/ripgrep over the tree, cached); on by default.
    dle_dependency_mapper: bool = True
    # Ask the agent for a unified diff instead of direct file edits, and
    # apply it with `git apply`. Off by default — not every coding-agent CLI
    # backend reliably emits diff-only output; turn it on once you've
    # confirmed your configured agent behaves well in this mode. Falls back
    # to the normal full-file-rewrite path automatically on any failure.
    dle_patch_diff_mode: bool = False
    # Fast ast.parse/bracket-balance check before the four-signal gate.
    # Cheap; on by default.
    dle_syntax_shield: bool = True
    # OPTIONAL fifth Verify signal (browser-based runtime check via
    # Playwright). Auto-detected per project (needs a package.json dev/start
    # script binding a port) and auto-skipped when Playwright/Chromium isn't
    # available — this flag just allows disabling the *attempt* outright.
    dle_telemetry_gate: bool = True
    # Review Risk: after a turn ships, rank its changed files by blast-radius/
    # sensitive-path/test-coverage heuristics so a human knows what to actually
    # read closely. Cheap (static text analysis, no LLM call); on by default.
    dle_review_risk: bool = True
    # Dependency Trust Gate: before the expensive four-signal gate, check any
    # newly-added package name (manifest entries or install-command lines) in
    # this turn's diff against the real PyPI/npm registry. A nonexistent
    # package fails the turn outright (the "slopsquatting" hallucinated-
    # dependency attack pattern); a suspiciously new-but-real one is surfaced
    # as a warning instead. Needs network access to the public registries;
    # degrades to "not run" rather than blocking a turn if unreachable. On by
    # default.
    dle_dependency_trust: bool = True
    # Secret Leak Gate: before the expensive gate, scan this turn's added diff
    # lines for the structural shape of a real credential (AWS key, PEM
    # private key, GitHub/Slack/Stripe/Anthropic/OpenAI/Google token, a new
    # .env file). A high-confidence match fails the turn outright — the same
    # severity as a syntax error. AI-assisted commits leak a real secret at
    # roughly double the rate of human-only commits (GitGuardian, 2026); this
    # is the mitigation for that specific, measured risk. On by default.
    dle_secret_leak: bool = True
    # Test Quality Gate: after a turn's real tests pass, run a small, bounded
    # set of AST-level mutations (comparison/boolean/constant flips) scoped
    # only to the functions this turn touched, and re-run the suite against
    # each one. A mutant the suite doesn't catch is a test that passes but
    # doesn't actually verify the behavior it claims to — a failure mode
    # "tests: PASS" cannot see by definition. Soft signal (participates in
    # the normal N-of-M vote, does not hard-fail a turn on its own, since a
    # surviving mutant is evidence of a weak test, not proof of a bug). On
    # by default.
    dle_test_quality: bool = True
    # Fraction of generated mutants a touched function's tests must kill for
    # Test Quality to count as passed for that turn. Floored/ceilinged to a
    # valid probability.
    test_quality_min_kill_rate: float = Field(default=0.7, ge=0.0, le=1.0)
    # Signed Turn Receipts: for every turn that ships, write an Ed25519-signed
    # JSON attestation (prompt hash, diff hash, full gate verdict, provider/
    # model, coding agent) to .supersonic/receipts/turn-<n>.json in the SAME
    # commit as the checkpoint it describes. Not a Verify signal — like
    # Review Risk, it never blocks a turn, it's a reproducibility record for
    # turns that already shipped. Verify any receipt with `sonic verify-
    # receipts <path>`; the public key travels inside the receipt itself, so
    # no access to this machine's private key is needed to check one. On by
    # default.
    dle_signed_receipts: bool = True

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
