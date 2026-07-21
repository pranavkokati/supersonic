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
    # Risk-Aware Model Escalation: if the turn that just shipped had at least
    # one HIGH-risk file in its Review Risk brief, the loop escalates to a
    # stronger model for the turn immediately following it — both the coding
    # agent's own CLI invocation (via its --model flag) and Supersonic's own
    # critic call — on the theory that a turn continuing work in an area
    # already flagged as auth/payment/migration-adjacent, high-blast-radius,
    # or undertested deserves stronger judgment on the very next pass, not
    # just a heuristic warning read after the fact. It can never affect the
    # turn that triggered it (Review Risk only runs on a turn that already
    # passed the gate and shipped), and it decays after exactly one escalated
    # turn — re-evaluated fresh from whatever the newest shipped turn found.
    dle_risk_escalation: bool = True
    # Model passed via --model to each coding-agent CLI when escalating.
    # Empty string = "no escalation target configured for this agent" — the
    # feature safely no-ops for that agent and the turn runs at its normal
    # model. Claude Code CLI accepts the "opus" alias directly (confirmed
    # current as of its July 2026 docs), so it ships a sensible default;
    # Codex/OpenCode/Cursor/Aider are left blank deliberately — their model
    # catalogs move fast enough that a hardcoded "strongest available model"
    # string would go stale and risk silently failing a real user's build.
    # Set your own in Settings once you know which model you want.
    escalation_model_claude: str = "opus"
    escalation_model_codex: str = ""
    escalation_model_opencode: str = ""
    escalation_model_cursor: str = ""
    escalation_model_aider: str = ""

    # --- Reliability Mesh -----------------------------------------------
    # PTY-native execution: run the coding-agent CLI inside a real
    # pseudo-terminal (stdlib `pty`, POSIX only) instead of a plain
    # subprocess pipe, so it sees a genuine TTY (isatty() true) instead of a
    # pipe some CLIs treat as "non-interactive" and silently degrade
    # behavior for. This is NOT filesystem-level interception — a PTY only
    # ever governs a process's stdin/stdout, never its file writes (see
    # agents/pty_runner.py's module docstring for the full explanation).
    # Off by default: it's the newer, POSIX-only path, and falls back to
    # the plain-subprocess path automatically wherever it can't run.
    dle_pty_supervision: bool = False
    # Live Syntax Watch: a concurrent filesystem watcher (stdlib `ast` +
    # mtime polling, no extra dependency) that re-parses each touched
    # Python file within a fraction of a second of it being saved, mid-turn
    # — instant visibility into a broken file instead of waiting for the
    # end-of-turn, diff-based Syntax Shield check. Observability only in
    # this version: it surfaces findings immediately, it doesn't (yet)
    # interrupt the running agent process. Cheap; on by default.
    dle_live_syntax_watch: bool = True
    # Self-Evolving Rules Engine: when the SAME Verify failure category
    # repeats across turns at least `rules_evolution_min_repeats` times, a
    # supervisor-critic LLM call synthesizes one durable rule from the
    # specific failure trace and appends it (never rewrites/mutates an
    # existing one) to this project's own `.supersonic/rules.md`, which then
    # gets folded into every subsequent turn's prompt — and, best-effort,
    # mirrored into `.cursorrules`/`CLAUDE.md` if the project already has
    # one of those real convention files (never created from scratch). On
    # by default; a no-op until a failure category actually repeats.
    dle_rules_evolution: bool = True
    rules_evolution_min_repeats: int = Field(default=2, ge=2, le=10)
    # Docker Sandbox: runs the coding-agent CLI inside a throwaway Docker
    # container (only this project's workdir bind-mounted in, capabilities
    # dropped, memory/CPU/pids capped, --rm) instead of directly on the
    # host, so checkpoint/rollback's git-scoped protection is backed by a
    # real filesystem boundary for anything outside the workdir. Does NOT
    # sandbox network egress — the agent still needs outbound access to
    # its own LLM provider's API (see agents/sandbox_runner.py's module
    # docstring for the full honest scope). Off by default: it requires a
    # real Docker install AND a pre-built/pulled image (docker_sandbox_image
    # below) — not zero-config the way dle_pty_supervision is — and falls
    # back cleanly to PTY/plain-subprocess execution if Docker isn't
    # reachable or no image is configured.
    dle_docker_sandbox: bool = False
    docker_sandbox_image: str = ""  # e.g. "supersonic-sandbox:latest" — see docker/sandbox.Dockerfile
    docker_memory_limit: str = "2g"
    docker_cpu_limit: str = "2"
    docker_pids_limit: int = Field(default=256, ge=16, le=4096)

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
