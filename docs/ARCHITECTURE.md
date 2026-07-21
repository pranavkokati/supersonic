# Supersonic architecture

## Overview

Supersonic is a **local FastAPI app** with a static frontend. The Python `supersonic` package owns
orchestration, agent invocation, structured memory, verification, and shipping. State persists in SQLite
(`~/.supersonic/sonic.db`) plus per-project workdirs, each of which is itself a git repository — the
checkpoint/rollback mechanism *is* git, not a bespoke snapshot format.

```
┌─────────────────────────────────────────────────────────┐
│  Browser (localhost:8787)                                │
│  landing → onboarding → dashboard (SSE live view)        │
└──────────────────────────┬───────────────────────────────┘
                            │ REST + SSE
┌──────────────────────────▼───────────────────────────────┐
│  supersonic/server.py (FastAPI)                           │
│  projects · runs · secrets · health · ledger · checkpoints│
└──────────────────────────┬───────────────────────────────┘
                            │
┌──────────────────────────▼───────────────────────────────┐
│  supersonic/loop/orchestrator.py                          │
│  setup once → per turn: plan → checkpoint → build → verify│
└─┬──────────┬───────────┬────────────┬──────────┬─────────┘
  │          │           │            │          │
  ▼          ▼           ▼            ▼          ▼
providers/  memory/    agents/      verify/   integrations/
(LLM calls) (Continuity (runner)    (gate:    (git_ops,
             Graph)                 4 signals) github, linear)
```

---

## The loop, precisely

Every turn:

1. **Retrieve** — `ContinuityGraph.retrieve(goal, budget, turn)` pulls the highest-relevance decisions/lessons
   from the ledger, plus *all* invariants and open failures unconditionally.
2. **Checkpoint reference** — the loop already holds `last_good_checkpoint`, a git commit tagged by
   `CheckpointManager` the last time a turn passed verification.
3. **Build** — `CodingAgentRunner` runs the project's one configured agent against the current prompt.
4. **Verify** — `verify.gate.run_gate()` runs up to four independent signals (tests, lint/typecheck, an LLM
   goal-satisfaction critic, a diff-similarity thrash detector) and requires enough of the signals that
   actually ran to pass.
5. **Commit or revert** — pass: `ledger.record_decision()` + new checkpoint + optional `git push`. Fail:
   `ledger.record_failure()` + `rollback_to(last_good_checkpoint)` (hard git reset, `.continuity/` preserved).
6. **Route** — `loop/planner.generate_turn_plan()` decides the next goal and whether the build is done, given
   the retrieved Continuity Graph context and the verification result.

This is why the loop can't silently drift the way a single-call router can: nothing is kept unless it's
proven, and every failure becomes context the next attempt can't ignore.

There is deliberately no multi-agent racing here. An earlier design ran two coding-agent CLIs concurrently
and picked a winner via a Thompson-sampling bandit — it doubled LLM spend on every turn it fired for, in
exchange for a benefit the Verify gate already delivers for free (rejecting bad output regardless of which
agent produced it). It's gone; one agent per project, no bandit tuning.

---

## Core modules

| Module | Role |
|--------|------|
| `loop/orchestrator.py` | Main pipeline — setup once, then the per-turn Checkpoint/Verify/Rollback loop |
| `loop/checkpoint.py` | Git-native commit + tag of verified state |
| `loop/rollback.py` | Hard git reset to the last verified checkpoint |
| `loop/planner.py` | Provider-agnostic plan / brand / next-turn routing |
| `loop/multi_repo.py` | Multi-Repository State Anchoring — coordinates checkpoint/rollback across linked repo working directories |
| `loop/replay.py` | Black Box Replay — assembles ledger + checkpoints + receipts + rules into one self-contained HTML timeline |
| `memory/` | Continuity Graph — `schema.py` (entries), `ledger.py` (append-only store), `graph.py` (retrieval), `distill.py` (compaction), `rules_engine.py` (Self-Evolving Rules Engine) |
| `verify/` | `qa.py` (tests/lint), `critic.py` (goal satisfaction), `thrash.py` (oscillation detector), `live_syntax_watch.py` (concurrent mid-turn syntax watcher), `gate.py` (combined decision) |
| `providers/` | `anthropic_provider.py`, `openai_provider.py`, `ollama_provider.py`, auto-detected in `__init__.py` |
| `agents/runner.py` | Spawn Claude Code / Codex / OpenCode / Cursor / Aider CLIs |
| `agents/pty_runner.py` | Optional PTY-native execution (`pty.fork()`) — a real terminal, not filesystem-write interception |
| `agents/sandbox_runner.py` | Optional Docker-sandboxed execution — contains filesystem blast radius to a throwaway container + the mounted workdir; does not sandbox network egress |
| `integrations/git_ops.py`, `integrations/github.py` | Native git + `gh` CLI shipping — no middleman |
| `integrations/linear.py`, `integrations/notify.py` | Optional, off unless configured |
| `research/tavily.py` | Optional enrichment, never required |
| `store.py` | SQLite projects + runs |
| `server.py` | HTTP API + static app mount |
| `events.py` | SSE pub/sub for the live dashboard |

---

## Run lifecycle

1. **Project created** — workdir assigned (default `~/.supersonic/projects/<id>` or a user-chosen path)
2. **Run started** — `POST /api/projects/{id}/run`
3. **Setup phase** (once per run)
   - Idea grounding (optional Tavily research, or a fallback model-knowledge call)
   - Plan + brand (one provider call each)
   - Git repo init + initial checkpoint
   - Optional GitHub repo creation (`gh`), optional Linear issue
4. **Build loop** (until the planner says done *and* the latest verification passed, or `max_turn_budget` hits)
   - Retrieve Continuity Graph context for this turn's goal
   - Build (the project's configured agent)
   - Verify gate (up to 4 signals)
   - Checkpoint or rollback
   - Route next turn
5. **Complete** — final ship (PR if `ship_mode=pr`), optional completion webhook, run marked done

Events stream to the dashboard via `GET /api/runs/{id}/stream`. The Continuity Graph and checkpoint history
are also queryable directly: `GET /api/projects/{id}/ledger`, `GET /api/projects/{id}/checkpoints`.

---

## Configuration

| Path | Contents |
|------|----------|
| `~/.supersonic/config.json` | Provider keys, default agent, loop tuning |
| `~/.supersonic/sonic.db` | Projects and run history |
| `~/.supersonic/projects/<id>/` | Default project workdirs |
| `<workdir>/.continuity/ledger.jsonl` | The Continuity Graph — append-only, git-committed with the project |
| `<workdir>/.continuity/BRAIN.md` | Human/agent-readable snapshot of the ledger, regenerated each turn |
| `<workdir>/.supersonic/rules.json` + `rules.md` | Self-Evolving Rules Engine — one durable rule per repeated Verify failure category |
| `<workdir>/.supersonic/linked_repos.json` | Multi-Repository State Anchoring — the other repo paths registered alongside this project |
| `<custom>/` | User-chosen folder (never auto-deleted) |

---

## Frontend structure

| Path | Purpose |
|------|---------|
| `app/landing.html` | Entry — Open Supersonic |
| `app/onboarding.html` | First-run key setup (one provider key) + tutorial |
| `app/dashboard.html` | Composer + live run view (checkpoint timeline, Continuity Graph explorer) + settings |
| `web/index.html` | Public marketing site |

Shared design tokens: `style.css` + `sonic.css`.

---

## CLI

`sonic` entry point (`supersonic/cli.py`):

- `serve` — start local UI
- `run` — one-shot headless loop
- `doctor` — validate provider keys, agent CLIs, `git`/`gh` on PATH, and Docker Sandbox reachability
- `projects` / `portfolio` — list local builds
- `queue-add` / `queue-run` / `schedule` — overnight portfolio queue
- `verify-receipts` — cryptographically verify every Signed Turn Receipt in a project, offline
- `replay` — generate Black Box Replay, a self-contained HTML timeline of the whole build

---

## Testing

```bash
python -m pytest tests/ -q
```

Covers ledger read/write and retrieval, the verify gate's signal aggregation, checkpoint/rollback against a
real temp git repo, and provider auto-detection.
