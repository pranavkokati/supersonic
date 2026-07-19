# Supersonic roadmap

Local, open-source build loop that only moves forward on evidence.

**Docs:** [README](README.md) · [Architecture](docs/ARCHITECTURE.md) · [Demo walkthrough](docs/DEMO.md) · [FAQ](docs/FAQ.md)

---

## Shipped

- Checkpoint → Verify → Rollback core loop: git-native checkpointing, a four-signal verify gate (tests,
  lint/typecheck, goal-satisfaction critic, thrash detector), automatic rollback on failure
- Continuity Graph: structured decision/invariant/failure/lesson ledger with relevance-ranked retrieval and
  periodic distillation — replaces flat-transcript context compression entirely
- Bandit-Gated Agent Racing: Thompson-sampling agent selection across Claude Code, Codex, OpenCode, Cursor
  Agent, and Aider, with a hard per-run cost ceiling
- Provider-agnostic LLM layer: Anthropic, OpenAI, or local Ollama, auto-detected — one key to run
- Native git + `gh` CLI shipping — no sponsor-locked integration middleman
- Optional plugins (Linear, Tavily research, completion webhook), all off by default
- Local dashboard with a live checkpoint timeline, Continuity Graph explorer, and race leaderboard

## Next

- Multi-run tabs and a replay scrubber over checkpoint history
- Visual regression + accessibility QA as additional verify-gate signals
- Desktop shell (Tauri) with menu-bar status
- Plugin SDK for custom verify signals and custom bandit task-type classifiers
- Cross-project Continuity Graph search (learn invariants across your whole portfolio, not just one project)
