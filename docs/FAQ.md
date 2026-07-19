# Supersonic — FAQ

## What is this, in one paragraph?

A local, open-source autonomous build loop. It plans a build, checkpoints the project with git, runs a coding
agent, verifies the result against four independent signals, and either keeps the change as a new checkpoint
or rolls it back — writing the reason to a structured memory (the Continuity Graph) either way.

## Do I need four different vendor accounts to try it?

No. **One LLM API key** — Anthropic, OpenAI, or a local Ollama server — is the only hard requirement. GitHub
shipping needs the `gh` CLI (free, one-time `gh auth login`). Linear, Tavily research, and completion webhooks
are optional plugins, off unless you configure them.

## Why does Verify check four separate things instead of just running tests?

Tests only tell you the code doesn't crash on the cases you already thought to write. A test suite can pass
while completely missing what the turn was actually supposed to do. The goal-satisfaction critic exists
specifically to catch that gap — it compares the diff against the turn's stated goal, not just against
assertions. Lint/typecheck catches a different failure class again, and the thrash detector catches something
none of the other three can see: the loop oscillating between two states instead of converging.

## What happens when a turn fails verification?

The workdir is hard-reset to the last checkpoint that *did* pass (`git reset --hard` plus a clean of untracked
files, with `.continuity/` preserved). The failure — what was attempted and why it didn't pass — is written to
the Continuity Graph as a `failure` entry, which is always included in every subsequent turn's retrieved
context regardless of relevance ranking. The next attempt starts already knowing what didn't work.

## What is the Continuity Graph, concretely?

An append-only JSONL file at `<workdir>/.continuity/ledger.jsonl`, committed to the project's own git history.
Each entry is one of `decision`, `invariant`, `failure`, `lesson`, or `fact`. Retrieval is a lightweight
TF-IDF-style scorer over the ledger's own text (no embeddings, no vector database, no extra dependency) —
invariants and open failures are always included; decisions and facts are ranked by relevance to the current
turn's goal. Old, aged-out decisions get periodically folded into a single distilled `lesson` entry via one
LLM call, so the ledger stays bounded without silently losing information.

## Does Supersonic race multiple coding agents against each other?

No, deliberately not. An earlier design ran two agent CLIs concurrently and used a Thompson-sampling bandit
to pick a winner. It roughly doubled LLM spend on every turn it fired for, in exchange for a marginal benefit
the Verify gate already delivers for free — bad output gets rejected regardless of which agent wrote it. It
was cut. One configured agent per project, no bandit, no doubled cost.

## Which coding agents are supported?

Claude Code, Codex, OpenCode, Cursor Agent, and Aider — any local CLI on `PATH`. `sonic doctor` reports which
ones it can find. Pick one as your project's default agent.

## Can I try it without any API keys at all?

Yes — `sonic run --demo` (or `SONIC_DEMO=1`) runs the full Checkpoint/Verify/Rollback loop with synthetic
turns, so you can see the mechanics (checkpointing, the verify gate, the routing loop) without a live
provider or coding-agent CLI.

## Where does my data go?

Nowhere but your machine. Config lives in `~/.supersonic/config.json`. Project workdirs are plain git repos —
`~/.supersonic/projects/<id>/` by default, or a folder you choose. Nothing is uploaded except the API calls
you've explicitly configured (your LLM provider, and GitHub/Linear/webhook only if you've set those up).

## Where do I go for more detail?

- [Architecture](ARCHITECTURE.md) — module-by-module breakdown and the run lifecycle
- [Demo walkthrough](DEMO.md) — a guided first run
- [Roadmap](../ROADMAP.md) — what's shipped and what's next
