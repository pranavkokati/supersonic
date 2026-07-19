"""FastAPI local server — landing, dashboard, live SSE, and the Continuity Graph / checkpoint APIs."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from supersonic.agents.runner import available_agents
from supersonic.config import UserSecrets, ensure_dirs, get_settings, load_secrets, save_secrets
from supersonic.events import subscribe
from supersonic.loop.bandit import AgentBandit
from supersonic.loop.checkpoint import CheckpointManager, run_git
from supersonic.memory import ContinuityLedger
from supersonic.providers import available_providers
from supersonic.store import (
    create_project,
    create_run,
    delete_project,
    enqueue_project,
    get_project,
    get_run,
    init_db,
    list_projects,
    list_runs,
    portfolio_summary,
    update_project,
)
from supersonic.validate import RunValidationError, validate_live_run

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent / "app"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Supersonic", version="1.0.0")


class SecretsUpdate(BaseModel):
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    ollama_base_url: Optional[str] = None
    preferred_provider: Optional[str] = None
    default_agent: Optional[str] = None
    race_enabled: Optional[bool] = None
    race_agents: Optional[List[str]] = None
    max_race_turns: Optional[int] = None
    race_challenger_turn_cap: Optional[int] = None
    ship_mode: Optional[str] = None
    github_owner: Optional[str] = None
    github_repo: Optional[str] = None
    linear_api_key: Optional[str] = None
    linear_team_id: Optional[str] = None
    notify_webhook_url: Optional[str] = None
    notify_email_to: Optional[str] = None
    tavily_api_key: Optional[str] = None
    webhook_url: Optional[str] = None
    webhook_secret: Optional[str] = None
    schedule_enabled: Optional[bool] = None
    schedule_interval_hours: Optional[int] = None
    ledger_context_budget: Optional[int] = None
    max_turn_budget: Optional[int] = None
    verify_min_signals_pass: Optional[int] = None


class ProjectCreate(BaseModel):
    name: str = "Build"
    idea: str = ""
    agent: str = "claude"
    template_id: str = "greenfield"
    workdir: str = ""


class QueueCreate(BaseModel):
    seed: str = ""


class RunCreate(BaseModel):
    seed: str = ""


class SuggestRequest(BaseModel):
    seed: str = ""
    count: int = 3


def _run_dict(r) -> Dict[str, Any]:
    return asdict(r)


def _agent_ready(sec: UserSecrets) -> bool:
    agents = {a["id"]: a for a in available_agents()}
    info = agents.get(sec.default_agent)
    return bool(info and info.get("available"))


@app.on_event("startup")
def startup() -> None:
    ensure_dirs()
    init_db()
    from supersonic.schedule import start_scheduler

    start_scheduler()


@app.get("/api/health")
def health() -> Dict[str, Any]:
    s = get_settings()
    sec = load_secrets()
    return {
        "ok": True,
        "demo": s.sonic_demo,
        "agents": available_agents(),
        "providers": available_providers(sec),
        "api_version": 1,
        "orchestration": "checkpoint_verify_rollback",
        "features": [
            "continuity_graph",
            "checkpoint_verify_rollback",
            "bandit_agent_racing",
            "provider_agnostic",
            "native_git_shipping",
            "goal_critic",
            "thrash_detector",
            "portfolio",
            "schedule",
        ],
        "pillars": {
            "provider": bool(available_providers(sec)),
            "agent": _agent_ready(sec),
        },
        "keys_configured": {
            "anthropic": bool(sec.anthropic_api_key),
            "openai": bool(sec.openai_api_key),
            "tavily": bool(sec.tavily_api_key),
            "linear": bool(sec.linear_api_key),
        },
    }


@app.get("/api/secrets")
def get_secrets_masked() -> Dict[str, Any]:
    sec = load_secrets()
    d = sec.model_dump()
    for k in d:
        if k.endswith("_api_key") and d[k]:
            d[k] = d[k][:4] + "••••" + d[k][-4:] if len(d[k]) > 8 else "••••"
    return d


@app.put("/api/secrets")
def put_secrets(body: SecretsUpdate) -> Dict[str, str]:
    sec = load_secrets()
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(sec, k, v)
    save_secrets(sec)
    return {"status": "saved"}


@app.get("/api/projects")
def api_list_projects() -> List[Dict[str, Any]]:
    return [p.__dict__ for p in list_projects()]


@app.post("/api/projects")
def api_create_project(body: ProjectCreate) -> Dict[str, Any]:
    sec = load_secrets()
    agent = body.agent or sec.default_agent
    wd = body.workdir.strip() or None
    p = create_project(body.name, body.idea, agent, template_id=body.template_id or "greenfield", workdir=wd)
    return p.__dict__


@app.get("/api/projects/{pid}")
def api_get_project(pid: str) -> Dict[str, Any]:
    p = get_project(pid)
    if not p:
        raise HTTPException(404, "project not found")
    return {**p.__dict__, "runs": [_run_dict(r) for r in list_runs(pid)]}


@app.patch("/api/projects/{pid}")
def api_patch_project(pid: str, body: ProjectCreate) -> Dict[str, Any]:
    wd = body.workdir.strip() or None
    p = update_project(pid, name=body.name, idea=body.idea, agent=body.agent, template_id=body.template_id or "greenfield", workdir=wd)
    if not p:
        raise HTTPException(404, "project not found")
    return p.__dict__


def _delete_project_or_409(pid: str) -> Dict[str, str]:
    try:
        if not delete_project(pid):
            raise HTTPException(404, "project not found")
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return {"status": "deleted", "id": pid}


@app.delete("/api/projects/{pid}")
def api_delete_project(pid: str) -> Dict[str, str]:
    return _delete_project_or_409(pid)


@app.post("/api/projects/{pid}/delete")
def api_delete_project_post(pid: str) -> Dict[str, str]:
    return _delete_project_or_409(pid)


@app.post("/api/suggest")
def api_suggest(body: SuggestRequest) -> Dict[str, Any]:
    from supersonic.suggest import suggest_products

    secrets = load_secrets()
    ideas, bundle = suggest_products(secrets, body.seed, count=min(max(body.count, 1), 5))
    return {"seed": body.seed, "query": bundle.query, "synthesis": bundle.answer, "suggestions": [s.to_dict() for s in ideas]}


@app.get("/api/templates")
def api_templates() -> List[Dict[str, Any]]:
    from supersonic.templates import list_templates

    return list_templates()


@app.get("/api/portfolio")
def api_portfolio() -> List[Dict[str, Any]]:
    return portfolio_summary()


@app.get("/api/queue")
def api_queue() -> Dict[str, Any]:
    from supersonic.schedule import queue_status

    return queue_status()


@app.post("/api/projects/{pid}/queue")
def api_enqueue(pid: str, body: QueueCreate) -> Dict[str, Any]:
    p = get_project(pid)
    if not p:
        raise HTTPException(404, "project not found")
    qid = enqueue_project(pid, body.seed)
    return {"queue_id": qid, "project_id": pid, "status": "queued"}


@app.get("/api/runs/{rid}/diff")
def api_run_diff(rid: str) -> Dict[str, Any]:
    r = get_run(rid)
    if not r:
        raise HTTPException(404, "run not found")
    p = get_project(r.project_id)
    if not p or not p.workdir or not Path(p.workdir, ".git").exists():
        return {"diff": ""}
    workdir = Path(p.workdir)
    checkpoints = CheckpointManager(workdir).list()
    if len(checkpoints) < 2:
        return {"diff": ""}
    diff = run_git(["diff", checkpoints[-2].commit, checkpoints[-1].commit], workdir, check=False).stdout
    return {"diff": diff[:20000]}


@app.get("/api/projects/{pid}/ledger")
def api_project_ledger(pid: str) -> Dict[str, Any]:
    p = get_project(pid)
    if not p:
        raise HTTPException(404, "project not found")
    wd = Path(p.workdir) if p.workdir else None
    if not wd or not wd.exists():
        return {"project_id": pid, "ready": False, "stats": {}, "entries": []}
    ledger = ContinuityLedger(wd)
    entries = ledger.all(include_superseded=False)
    return {
        "project_id": pid,
        "ready": True,
        "stats": ledger.stats(),
        "entries": [e.to_dict() for e in entries[-200:]],
    }


@app.get("/api/projects/{pid}/checkpoints")
def api_project_checkpoints(pid: str) -> Dict[str, Any]:
    p = get_project(pid)
    if not p:
        raise HTTPException(404, "project not found")
    wd = Path(p.workdir) if p.workdir else None
    if not wd or not (wd / ".git").exists():
        return {"project_id": pid, "checkpoints": []}
    checkpoints = CheckpointManager(wd).list()
    return {"project_id": pid, "checkpoints": [c.to_dict() for c in checkpoints]}


@app.get("/api/projects/{pid}/bandit")
def api_project_bandit(pid: str) -> Dict[str, Any]:
    p = get_project(pid)
    if not p:
        raise HTTPException(404, "project not found")
    sec = load_secrets()
    wd = Path(p.workdir) if p.workdir else None
    agents = list(dict.fromkeys([sec.default_agent, *sec.race_agents]))
    if not wd or not wd.exists() or len(agents) < 2:
        return {"project_id": pid, "enabled": sec.race_enabled, "win_rates": {}}
    bandit = AgentBandit(wd, agents)
    return {"project_id": pid, "enabled": sec.race_enabled, "win_rates": bandit.win_rates()}


@app.get("/api/validate")
def api_validate(agent: Optional[str] = None) -> Dict[str, Any]:
    sec = load_secrets()
    kind = agent or sec.default_agent
    try:
        validate_live_run(sec, kind)  # type: ignore[arg-type]
        return {"ok": True, "agent": kind}
    except RunValidationError as e:
        return {"ok": False, "agent": kind, "error": str(e)}


@app.post("/api/projects/{pid}/run")
def api_run_factory(pid: str, body: RunCreate, bg: BackgroundTasks) -> Dict[str, Any]:
    p = get_project(pid)
    if not p:
        raise HTTPException(404, "project not found")
    secrets = load_secrets()
    settings = get_settings()
    if not settings.sonic_demo:
        try:
            validate_live_run(secrets, p.agent)  # type: ignore[arg-type]
        except RunValidationError as e:
            raise HTTPException(400, str(e)) from e

    run = create_run(pid)

    def _job() -> None:
        from supersonic.events import publish
        from supersonic.loop.orchestrator import run_factory
        from supersonic.store import update_run as ur

        try:
            run_factory(run, secrets, body.seed)
        except Exception as e:
            logger.exception("supersonic run failed")
            ur(run.id, status="failed", error=str(e), finished_at=datetime.now(timezone.utc).isoformat())
            publish(run.id, {"type": "error", "message": str(e)})

    bg.add_task(_job)
    return {"run_id": run.id, "status": "started"}


@app.get("/api/runs/{rid}")
def api_get_run(rid: str) -> Dict[str, Any]:
    r = get_run(rid)
    if not r:
        raise HTTPException(404, "run not found")
    return _run_dict(r)


@app.get("/api/runs/{rid}/stream")
async def api_stream_run(rid: str) -> StreamingResponse:
    r = get_run(rid)
    if not r:
        raise HTTPException(404, "run not found")

    async def gen():
        snap = json.dumps({"type": "snapshot", "run": _run_dict(r)})
        yield f"data: {snap}\n\n"
        async for msg in subscribe(rid):
            yield f"data: {msg}\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/api/projects/{pid}/artifact")
def api_project_artifact(pid: str, path: str) -> FileResponse:
    p = get_project(pid)
    if not p or not p.workdir:
        raise HTTPException(404)
    root = Path(p.workdir).resolve()
    target = (root / path).resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        raise HTTPException(404, "artifact not found")
    return FileResponse(target)


@app.get("/api/projects/{pid}/files")
def api_list_files(pid: str) -> List[str]:
    p = get_project(pid)
    if not p:
        raise HTTPException(404)
    root = Path(p.workdir)
    if not root.exists():
        return []
    return [str(f.relative_to(root)) for f in root.rglob("*") if f.is_file()][:200]


if APP_DIR.exists():
    app.mount("/assets", StaticFiles(directory=APP_DIR / "assets"), name="assets")


@app.get("/")
def landing() -> FileResponse:
    return FileResponse(APP_DIR / "landing.html")


@app.get("/dashboard")
def dashboard() -> FileResponse:
    return FileResponse(APP_DIR / "dashboard.html")


@app.get("/onboarding")
def onboarding() -> FileResponse:
    return FileResponse(APP_DIR / "onboarding.html")


@app.get("/site")
def marketing_redirect() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
