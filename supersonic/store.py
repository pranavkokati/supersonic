"""SQLite store for projects and factory runs."""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from supersonic.config import CONFIG_DIR, ensure_dirs

DB_PATH = CONFIG_DIR / "sonic.db"


def _conn() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                idea TEXT,
                status TEXT DEFAULT 'draft',
                workdir TEXT,
                agent TEXT DEFAULT 'codex',
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                project_id TEXT,
                status TEXT DEFAULT 'pending',
                phases TEXT DEFAULT '[]',
                result TEXT,
                error TEXT,
                current_phase TEXT DEFAULT '',
                agent_log TEXT DEFAULT '',
                created_at TEXT,
                finished_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );
            """
        )
        for col, typ in [("current_phase", "TEXT DEFAULT ''"), ("agent_log", "TEXT DEFAULT ''")]:
            try:
                c.execute(f"ALTER TABLE runs ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        for col, typ in [("template_id", "TEXT DEFAULT 'greenfield'")]:
            try:
                c.execute(f"ALTER TABLE projects ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS run_queue (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                seed TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );
            """
        )


@dataclass
class Project:
    id: str
    name: str
    idea: str = ""
    status: str = "draft"
    workdir: str = ""
    agent: str = "claude"
    template_id: str = "greenfield"
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Run:
    id: str
    project_id: str
    status: str = "pending"
    phases: List[Dict[str, Any]] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    error: str = ""
    current_phase: str = ""
    agent_log: str = ""
    created_at: str = ""
    finished_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_workdir(path: str) -> str:
    wd = Path(path.strip()).expanduser().resolve()
    wd.mkdir(parents=True, exist_ok=True)
    return str(wd)


def _is_managed_workdir(workdir: str) -> bool:
    try:
        root = Path(workdir).resolve()
        managed = (CONFIG_DIR / "projects").resolve()
        return str(root).startswith(str(managed))
    except OSError:
        return False


def create_project(
    name: str,
    idea: str = "",
    agent: str = "claude",
    template_id: str = "greenfield",
    workdir: str | None = None,
) -> Project:
    init_db()
    pid = uuid.uuid4().hex[:12]
    if workdir and workdir.strip():
        workdir = _resolve_workdir(workdir)
    else:
        workdir = str(CONFIG_DIR / "projects" / pid)
        Path(workdir).mkdir(parents=True, exist_ok=True)
    ts = _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO projects (id,name,idea,status,workdir,agent,template_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, name, idea, "draft", workdir, agent, template_id, ts, ts),
        )
    return Project(
        id=pid,
        name=name,
        idea=idea,
        workdir=workdir,
        agent=agent,
        template_id=template_id,
        created_at=ts,
        updated_at=ts,
    )


def list_projects() -> List[Project]:
    init_db()
    with _conn() as c:
        rows = c.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
    return [_row_project(r) for r in rows]


def get_project(pid: str) -> Optional[Project]:
    init_db()
    with _conn() as c:
        r = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    return _row_project(r) if r else None


def delete_project(pid: str, *, remove_files: bool = True) -> bool:
    init_db()
    p = get_project(pid)
    if not p:
        return False
    with _conn() as c:
        running = c.execute(
            "SELECT 1 FROM runs WHERE project_id=? AND status='running' LIMIT 1",
            (pid,),
        ).fetchone()
        if running:
            raise ValueError("Cannot delete project while a run is in progress")
        c.execute("DELETE FROM runs WHERE project_id=?", (pid,))
        c.execute("DELETE FROM projects WHERE id=?", (pid,))
    if remove_files and p.workdir and _is_managed_workdir(p.workdir):
        root = Path(p.workdir)
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
    return True


def update_project(pid: str, **kwargs: Any) -> Optional[Project]:
    init_db()
    p = get_project(pid)
    if not p:
        return None
    for k, v in kwargs.items():
        if k == "workdir" and isinstance(v, str) and v.strip():
            v = _resolve_workdir(v)
        if hasattr(p, k) and v is not None:
            setattr(p, k, v)
    p.updated_at = _now()
    with _conn() as c:
        c.execute(
            "UPDATE projects SET name=?, idea=?, status=?, agent=?, template_id=?, workdir=?, updated_at=? WHERE id=?",
            (p.name, p.idea, p.status, p.agent, p.template_id, p.workdir, p.updated_at, pid),
        )
    return p


def enqueue_project(project_id: str, seed: str = "") -> str:
    init_db()
    qid = uuid.uuid4().hex[:12]
    ts = _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO run_queue (id, project_id, seed, status, created_at) VALUES (?,?,?,?,?)",
            (qid, project_id, seed, "pending", ts),
        )
    return qid


def list_queue() -> List[Dict[str, Any]]:
    init_db()
    with _conn() as c:
        rows = c.execute(
            "SELECT q.*, p.name as project_name FROM run_queue q LEFT JOIN projects p ON p.id=q.project_id "
            "WHERE q.status='pending' ORDER BY q.created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def peek_queue() -> Optional[Dict[str, Any]]:
    q = list_queue()
    return q[0] if q else None


def dequeue_next_queued() -> Optional[Dict[str, Any]]:
    init_db()
    item = peek_queue()
    if not item:
        return None
    with _conn() as c:
        c.execute("UPDATE run_queue SET status='running' WHERE id=?", (item["id"],))
    return item


def complete_queue_item(qid: str, *, status: str = "done") -> None:
    init_db()
    with _conn() as c:
        c.execute("UPDATE run_queue SET status=? WHERE id=?", (status, qid))


def get_running_project_id() -> Optional[str]:
    init_db()
    with _conn() as c:
        r = c.execute(
            "SELECT project_id FROM runs WHERE status='running' LIMIT 1"
        ).fetchone()
    return str(r["project_id"]) if r else None


def portfolio_summary() -> List[Dict[str, Any]]:
    """Aggregate health for portfolio dashboard."""
    init_db()
    projects = list_projects()
    out: List[Dict[str, Any]] = []
    for p in projects:
        runs = list_runs(p.id)
        latest = runs[0] if runs else None
        tracking = (latest.result or {}).get("tracking", {}) if latest and latest.result else {}
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "status": p.status,
                "agent": p.agent,
                "template_id": getattr(p, "template_id", "greenfield"),
                "idea": p.idea,
                "runs": len(runs),
                "last_run_status": latest.status if latest else None,
                "turns": (latest.result or {}).get("turns") if latest and latest.result else None,
                "build_complete": (latest.result or {}).get("build_complete") if latest and latest.result else False,
                "linear_url": tracking.get("linear_project_url", ""),
                "github_url": tracking.get("github_url", ""),
                "updated_at": p.updated_at,
            }
        )
    return out


def create_run(project_id: str) -> Run:
    init_db()
    rid = uuid.uuid4().hex[:12]
    ts = _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO runs (id,project_id,status,phases,created_at) VALUES (?,?,?,?,?)",
            (rid, project_id, "pending", "[]", ts),
        )
    return Run(id=rid, project_id=project_id, created_at=ts)


def update_run(rid: str, **kwargs: Any) -> None:
    init_db()
    with _conn() as c:
        if "phases" in kwargs:
            kwargs["phases"] = json.dumps(kwargs["phases"])
        if "result" in kwargs and kwargs["result"] is not None:
            kwargs["result"] = json.dumps(kwargs["result"])
        cols = ", ".join(f"{k}=?" for k in kwargs)
        c.execute(f"UPDATE runs SET {cols} WHERE id=?", (*kwargs.values(), rid))


def get_run(rid: str) -> Optional[Run]:
    init_db()
    with _conn() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    return _row_run(r) if r else None


def list_runs(project_id: str) -> List[Run]:
    init_db()
    with _conn() as c:
        rows = c.execute("SELECT * FROM runs WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
    return [_row_run(r) for r in rows]


def _row_project(r: sqlite3.Row) -> Project:
    d = dict(r)
    d.setdefault("template_id", "greenfield")
    return Project(**{k: v for k, v in d.items() if k in Project.__dataclass_fields__})


def append_agent_log(rid: str, line: str) -> None:
    init_db()
    with _conn() as c:
        c.execute("UPDATE runs SET agent_log = COALESCE(agent_log, '') || ? || char(10) WHERE id=?", (line, rid))


def _row_run(r: sqlite3.Row) -> Run:
    d = dict(r)
    d["phases"] = json.loads(d.get("phases") or "[]")
    d["result"] = json.loads(d["result"]) if d.get("result") else None
    d.setdefault("current_phase", "")
    d.setdefault("agent_log", "")
    return Run(**{k: v for k, v in d.items() if k in Run.__dataclass_fields__})
