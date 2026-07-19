#!/usr/bin/env python3
"""Supersonic CLI — `sonic`."""

from __future__ import annotations

import os

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from supersonic.agents.runner import available_agents
from supersonic.config import ensure_dirs, load_secrets, save_secrets
from supersonic.providers import available_providers
from supersonic.store import create_project, create_run, init_db, list_projects

app = typer.Typer(name="sonic", help="Supersonic — Checkpoint/Verify/Rollback build loop with a structured Continuity Graph memory")
console = Console()


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    """Start the local dashboard."""
    ensure_dirs()
    init_db()
    uvicorn.run("supersonic.server:app", host=host, port=port, reload=False)


@app.command()
def run(
    idea: str = typer.Option("", help="Seed idea (left blank, the loop grounds one from research)"),
    agent: str = typer.Option("claude", help="claude | codex | opencode | cursor | aider"),
    demo: bool = typer.Option(False, "--demo", help="Run without live provider/agent calls"),
) -> None:
    """Run one full build loop from the CLI."""
    if demo:
        os.environ["SONIC_DEMO"] = "1"
    ensure_dirs()
    init_db()
    secrets = load_secrets()

    if not demo and not available_providers(secrets):
        raise typer.BadParameter(
            "No LLM provider configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY, or run a local `ollama serve`."
        )

    from supersonic.loop.orchestrator import run_factory

    p = create_project(name=idea[:80] or "CLI run", idea=idea, agent=agent)
    r = create_run(p.id)
    console.print(f"[bold]Supersonic run[/] project={p.id} run={r.id}")
    result = run_factory(r, secrets, idea)
    console.print_json(data=result)


@app.command()
def doctor() -> None:
    """Check LLM providers, coding-agent CLIs, and git/gh availability."""
    import shutil

    ensure_dirs()
    sec = load_secrets()
    table = Table(title="Supersonic doctor")
    table.add_column("Check")
    table.add_column("Status")

    providers = available_providers(sec)
    for name in ("anthropic", "openai", "ollama"):
        table.add_row(f"Provider: {name}", "✓ configured" if name in providers else "—")

    table.add_row("git", "✓ on PATH" if shutil.which("git") else "missing (required)")
    table.add_row("gh (GitHub shipping)", "✓ on PATH" if shutil.which("gh") else "— optional, shipping to GitHub disabled")

    for agent_info in available_agents():
        table.add_row(f"Agent: {agent_info['id']}", "✓ on PATH" if agent_info["available"] else "not found")

    console.print(table)


@app.command()
def projects() -> None:
    """List local projects."""
    init_db()
    for project in list_projects():
        console.print(f"{project.id}  [{project.status}]  {project.name}  agent={project.agent}")


@app.command("queue-add")
def queue_add(project_id: str = typer.Argument(...), seed: str = typer.Option("")) -> None:
    """Add a project to the overnight portfolio queue."""
    from supersonic.store import enqueue_project

    init_db()
    qid = enqueue_project(project_id, seed)
    console.print(f"[green]Queued[/] {qid} for project {project_id}")


@app.command("queue-run")
def queue_run() -> None:
    """Run the next queued project, if nothing is currently running."""
    from supersonic.schedule import run_next_queued

    init_db()
    rid = run_next_queued()
    console.print(f"[green]Started[/] run {rid}" if rid else "[yellow]Nothing to run[/] (queue empty or busy)")


@app.command()
def schedule(
    enable: bool = typer.Option(True, "--enable/--disable"),
    hours: int = typer.Option(24, help="Interval between scheduled runs"),
) -> None:
    """Enable or disable the overnight portfolio scheduler."""
    sec = load_secrets()
    sec.schedule_enabled = enable
    sec.schedule_interval_hours = max(hours, 1)
    save_secrets(sec)
    console.print(f"Scheduler {'enabled' if enable else 'disabled'} · every {sec.schedule_interval_hours}h")


@app.command()
def portfolio() -> None:
    """Portfolio health summary across all local projects."""
    from supersonic.store import portfolio_summary

    init_db()
    table = Table(title="Supersonic portfolio")
    table.add_column("Project")
    table.add_column("Status")
    table.add_column("Turns")
    table.add_column("GitHub")
    for row in portfolio_summary():
        table.add_row(row["name"][:40], row["status"], str(row.get("turns") or "—"), "✓" if row.get("github_url") else "—")
    console.print(table)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
