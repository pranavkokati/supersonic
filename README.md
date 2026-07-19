# Supersonic

**The build loop that only moves forward on evidence.**

[![License: MIT](https://img.shields.io/badge/License-MIT-pink.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)

Supersonic is a local, open-source autonomous build loop. It plans, checkpoints, builds, verifies, and ships —
and unlike a single-call router that blindly decides what happens next, it can only keep a turn's changes once
they clear an independent four-signal Verify gate. A failed turn is rolled back to the last proven-good state,
not left to compound into a worse one.

## What makes this different

Most agentic build loops share the same weak points: they truncate context instead of remembering it
structurally, they trust one LLM call to decide whether a turn succeeded, and they lock you into a specific
stack of paid integrations before you can try them at all. Supersonic is built around three specific answers
to those problems:

- **Checkpoint → Verify → Rollback.** Every turn is git-checkpointed before it runs. Verification checks up to
  four independent signals — tests, lint/typecheck, an LLM goal-satisfaction critic, and a diff-similarity
  thrash detector — and a turn is only kept if enough of the signals that actually ran come back positive.
  A failed turn is hard-reset to the last verified checkpoint, and the failure reason is written to permanent
  memory so the next attempt doesn't repeat it.

- **Continuity Graph, not a truncated transcript.** Instead of compressing a flat context blob down to fit a
  token budget, Supersonic keeps an append-only, git-committed ledger of structured facts — decisions,
  invariants, failures, and distilled lessons. Each turn retrieves only what's relevant to its goal, with
  invariants and open failures always included regardless of relevance score, because silently dropping a
  constraint is far more expensive than a few extra tokens of context.

- **Bandit-Gated Agent Racing.** Optionally run two coding-agent CLIs against each other in isolated git
  worktrees. A Thompson-sampling bandit decides *when* it's actually worth racing — early on, or whenever the
  two agents' performance for a given task type is still uncertain — and stops racing once it has learned
  which agent wins at that kind of work. Racing frequency decays over a run instead of doubling cost forever,
  and a hard per-run ceiling backstops the worst case. Off by default.

Everything else — GitHub shipping, Linear, research enrichment, completion notifications — is optional and
off unless you configure it. The only hard requirement to run Supersonic is **one LLM API key**: Anthropic,
OpenAI, or a local Ollama server.

## Quickstart

```bash
git clone https://github.com/your-org/supersonic.git
cd supersonic
./bootstrap.sh
source .venv/bin/activate
sonic serve
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787) and complete onboarding — it asks for one API key.

## Run

```bash
sonic serve                                                     # local dashboard
sonic run --idea "Build a focused developer tool" --agent claude
sonic run --agent claude --race --race-with codex               # bandit-gated racing
sonic run --demo                                                 # no live provider/agent calls
```

## Verify setup

```bash
sonic doctor
```

Checks which LLM provider is configured, which coding-agent CLIs are on `PATH`, and whether `git`/`gh` are
available for shipping.

## Pipeline

| Stage | What happens |
|---|---|
| Plan | One provider-agnostic call grounds the idea and writes a build plan + product name |
| Checkpoint | Git-native commit + tag of the current verified state |
| Build | Claude Code, Codex, OpenCode, Cursor Agent, or Aider — single agent, or bandit-gated racing between two |
| Verify | Tests + lint/typecheck + goal-satisfaction critic + thrash detector, combined into one pass/fail gate |
| Rollback or ship | Pass → new checkpoint, pushed to GitHub. Fail → hard reset, failure logged to the Continuity Graph |

The loop stops when the planner marks the build genuinely complete *and* the latest verification passed. The
configured `max_turn_budget` remains a hard ceiling regardless.

## Project layout

```text
supersonic/          Python package
  providers/          LLM provider abstraction (Anthropic, OpenAI, Ollama) — auto-detected
  memory/             Continuity Graph — ledger, retrieval, distillation
  loop/               Checkpoint / Rollback / Planner / Bandit / Race / Orchestrator
  verify/             Tests, lint, goal critic, thrash detector, combined gate
  agents/             Coding-agent CLI runner + git-worktree isolation for racing
  integrations/       Native git + gh CLI shipping, optional Linear, optional webhook notify
  research/           Optional Tavily enrichment — never required
app/                  Local onboarding + dashboard (FastAPI + vanilla JS, SSE live view)
web/                  Marketing site
tests/                Pytest suite
docs/                 Architecture, demo walkthrough, FAQ
```

- [Architecture](docs/ARCHITECTURE.md)
- [Demo walkthrough](docs/DEMO.md)
- [FAQ](docs/FAQ.md)
- [Roadmap](ROADMAP.md)

## Development

```bash
source .venv/bin/activate
python -m pytest tests/ -q
```

## License

MIT — see [LICENSE](LICENSE).
