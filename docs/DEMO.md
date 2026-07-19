# Supersonic — demo walkthrough

A guided first run. Total time: **3–5 minutes**.

---

## Before you start

```bash
git clone https://github.com/your-org/supersonic.git
cd supersonic
./bootstrap.sh
source .venv/bin/activate
sonic serve
```

Open **http://127.0.0.1:8787**

**Option A — Live run:** add one LLM key (Anthropic or OpenAI) in onboarding. Claude Code / Codex / etc.
authenticate via their own local CLI login — nothing extra needed there.

**Option B — No keys:** `export SONIC_DEMO=1` before `sonic serve`, or run `sonic run --demo` from the CLI.
Demo mode runs the full Checkpoint/Verify/Rollback loop with synthetic turns so you can see the mechanics
without touching a live provider or coding-agent CLI.

---

## Act 1 — Positioning (30 sec)

> "Supersonic checkpoints before every turn, builds, and only keeps the change if it clears a four-signal
> Verify gate — tests, lint, a goal-satisfaction critic, and a thrash detector. Fail any of those and it's
> rolled back automatically, not left to compound. Memory isn't a truncated transcript, it's a structured
> Continuity Graph of decisions, invariants, and failures."

Show the landing page → **Open Supersonic**.

---

## Act 2 — Onboarding (30 sec)

Walk through the steps:

1. **Welcome** — what the loop does
2. **Provider** — paste one API key (or skip for demo mode)
3. **How it works** — prompt → folder → build
4. **Open dashboard**

> "The key is stored in ~/.supersonic/config.json — never uploaded anywhere."

---

## Act 3 — Run a build (2 min)

On the composer:

1. **Prompt (optional):**
   `A CLI that syncs issues to local markdown files`

2. **Folder:** leave default, or set e.g. `~/Projects/issue-sync-demo`

3. Click **Send**

Narrate the **Setup** timeline as it runs — research grounding, plan, brand, checkpoint init, optional GitHub
repo creation.

Then the **build loop**, turn by turn:

- **Agent tab** — live CLI output from the coding agent
- **Verify tab** — the four-signal gate result for the turn that just ran
- **Diff tab** — the change since the last checkpoint
- **Checkpoint timeline** — green nodes for verified turns, red for rollbacks
- **Continuity Graph panel** — decisions/invariants/failures accumulating live
- **Ship targets** — GitHub link appears once a repo is created

---

## Act 4 — Close (30 sec)

> "One optional prompt, one button, and the loop only keeps what it can prove. Swap coding agents any time,
> and the whole thing runs on your machine with one API key."

---

## Fallbacks

| Issue | Fix |
|-------|-----|
| Agent not found | `sonic doctor` — install the Claude Code, Codex, or another supported CLI |
| No provider configured | Use `SONIC_DEMO=1`, or add an Anthropic/OpenAI key, or run a local `ollama serve` |
| Run stuck | Check the Agent tab for CLI errors; restart `sonic serve` |
| Want CLI only | `sonic run --idea "..." --agent claude` |

---

## Suggested prompts (reliable demos)

- `CLI that syncs GitHub issues to markdown`
- `Python API that wraps a public weather API`
- `Todo app with SQLite and a REST API`
