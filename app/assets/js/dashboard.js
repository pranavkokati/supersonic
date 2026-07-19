/** Supersonic dashboard — project list, composer, live SSE run view, settings. */

(() => {
  const state = { projectId: null, runId: null, stream: null, phaseNodes: {} };

  async function request(path, options = {}) {
    const res = await fetch(`/api${path}`, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const text = await res.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (_) { /* non-JSON */ }
    if (!res.ok) throw new Error(data.detail || data.error || text || `Request failed (${res.status})`);
    return data;
  }

  // ---------- health / pillar dots ----------
  async function loadHealth() {
    const badge = document.querySelector("#health-badge");
    const dots = document.querySelector("#pillar-dots");
    try {
      const health = await request("/health");
      const pillars = health.pillars || {};
      dots.innerHTML = Object.entries(pillars).map(([k, ok]) => `<span class="${ok ? "on" : ""}" title="${k}"></span>`).join("");
      const ready = Object.values(pillars).every(Boolean);
      badge.textContent = health.demo ? "demo" : ready ? "ready" : "setup needed";
      badge.className = `sn-badge ${health.demo ? "" : ready ? "ok" : "err"}`;
    } catch (e) {
      badge.textContent = "offline";
      badge.className = "sn-badge err";
    }
  }

  // ---------- project sidebar ----------
  async function loadProjects() {
    const list = document.querySelector("#project-list");
    try {
      const projects = await request("/projects");
      list.innerHTML = projects.map((p) => `
        <li data-id="${p.id}">
          <div class="name">${escapeHtml(p.name || p.idea || "Untitled")}</div>
          <div class="meta">${p.status} · ${p.agent}</div>
        </li>`).join("") || `<li class="sn-dim">No projects yet.</li>`;
    } catch (_) { /* ignore */ }
  }

  window.__sonicSelectProject = async (id) => {
    state.projectId = id;
    document.querySelector("#run-workspace")?.classList.remove("hidden");
    try {
      const project = await request(`/projects/${id}`);
      const latest = (project.runs || [])[0];
      if (latest) openStream(latest.id);
      refreshSidePanels();
    } catch (_) { /* ignore */ }
  };

  // ---------- composer ----------
  document.querySelector("#loop-btn")?.addEventListener("click", async () => {
    const btn = document.querySelector("#loop-btn");
    const idea = document.querySelector("#loop-prompt")?.value.trim() || "";
    const agent = document.querySelector("#project-agent")?.value || "claude";
    const workdir = document.querySelector("#loop-folder")?.value.trim() || "";
    btn.disabled = true;
    try {
      const project = await request("/projects", {
        method: "POST",
        body: JSON.stringify({ name: idea.slice(0, 80) || "Build", idea, agent, workdir }),
      });
      state.projectId = project.id;
      document.querySelector("#run-workspace")?.classList.remove("hidden");
      resetRunView();
      const run = await request(`/projects/${project.id}/run`, { method: "POST", body: JSON.stringify({ seed: idea }) });
      openStream(run.run_id);
      loadProjects();
    } catch (e) {
      alert(e.message);
    } finally {
      btn.disabled = false;
    }
  });

  document.querySelector("#loop-folder-clear")?.addEventListener("click", () => {
    document.querySelector("#loop-folder").value = "";
  });

  // ---------- live run view ----------
  function resetRunView() {
    document.querySelector("#agent-log").textContent = "";
    document.querySelector("#verify-log").textContent = "Verify gate results (tests, lint, goal critic, thrash detector) appear here each turn.";
    document.querySelector("#diff-log").textContent = "Diff since the last checkpoint streams here.";
    document.querySelector("#race-log").innerHTML = `<p class="sn-dim">No races run yet — enable Agent Racing in Settings.</p>`;
    document.querySelector("#setup-timeline").innerHTML = "";
    document.querySelector("#loop-timeline").innerHTML = "";
    document.querySelector("#checkpoint-track").innerHTML = "";
    document.querySelector("#ledger-list").innerHTML = "";
    document.querySelector("#play-by-play").innerHTML = "";
    document.querySelector("#follow-up-list").innerHTML = "";
    document.querySelector("#run-progress")?.classList.remove("hidden");
    document.querySelector("#run-progress-fill").style.width = "4%";
    setStatus("running");
  }

  function setStatus(status) {
    const el = document.querySelector("#live-status");
    el.textContent = status;
    el.className = `sn-live-status ${status}`;
  }

  function openStream(runId) {
    state.runId = runId;
    state.stream?.close();
    document.querySelector("#run-workspace")?.classList.remove("hidden");
    document.querySelector("#mission-strip")?.classList.remove("hidden");
    document.querySelector("#mission-panes")?.classList.remove("hidden");
    document.querySelector("#tracking-panel")?.classList.remove("hidden");
    document.querySelector("#checkpoint-panel")?.classList.remove("hidden");
    document.querySelector("#ledger-panel")?.classList.remove("hidden");
    document.querySelector("#playbook-panel")?.classList.remove("hidden");
    document.querySelector("#planner-panel")?.classList.remove("hidden");

    const es = new EventSource(`/api/runs/${runId}/stream`);
    state.stream = es;
    let turnsSeen = 0;
    es.onmessage = (msg) => {
      let evt;
      try { evt = JSON.parse(msg.data); } catch (_) { return; }
      handleEvent(evt, () => turnsSeen++);
    };
    es.onerror = () => { /* browser auto-retries; server closes the stream on completion */ };
  }

  function handleEvent(evt, bumpTurn) {
    switch (evt.type) {
      case "snapshot":
        setStatus(evt.run?.status || "running");
        break;
      case "phase": {
        const track = evt.stage === "setup" ? "#setup-timeline" : "#loop-timeline";
        document.querySelector(track === "#loop-timeline" ? "#loop-section" : "#setup-section")?.classList.remove("hidden");
        upsertPhaseNode(track, evt);
        break;
      }
      case "agent_line":
        appendLog("#agent-log", evt.line);
        break;
      case "checkpoint":
        appendCheckpoint(evt);
        break;
      case "ledger_entry":
        appendLedgerEntryStub(evt);
        break;
      case "verify_result":
        appendLog("#verify-log", formatVerify(evt), true);
        break;
      case "race_result":
        renderRace(evt);
        break;
      case "turn_started":
        bumpTurn();
        document.querySelector("#loop-turn-label").textContent = `turn ${evt.turn}`;
        document.querySelector("#run-progress-fill").style.width = `${Math.min(92, 10 + evt.turn * 6)}%`;
        playByPlay(`Turn ${evt.turn}: ${evt.goal || ""}`.slice(0, 140));
        break;
      case "turn_plan":
        document.querySelector("#mission-route").textContent = evt.done ? "Wrapping up…" : "Routing…";
        renderFollowUp(evt);
        break;
      case "setup_complete":
        renderTracking(evt);
        break;
      case "distilled":
        playByPlay(`Continuity Graph distilled at turn ${evt.turn}`);
        break;
      case "complete":
        setStatus(evt.status === "completed" ? "done" : "failed");
        document.querySelector("#run-progress-fill").style.width = "100%";
        playByPlay(`Build ${evt.status}.`);
        refreshSidePanels();
        state.stream?.close();
        break;
      case "error":
        setStatus("failed");
        playByPlay(`Error: ${evt.message}`);
        state.stream?.close();
        break;
    }
  }

  function upsertPhaseNode(selector, evt) {
    const container = document.querySelector(selector);
    let node = container.querySelector(`[data-phase="${evt.phase}"]`);
    if (!node) {
      node = document.createElement("div");
      node.dataset.phase = evt.phase;
      node.className = "sn-timeline-item";
      node.innerHTML = `<span class="sn-timeline-dot"></span><span class="sn-timeline-tool"></span><span class="sn-timeline-detail"></span>`;
      container.appendChild(node);
    }
    node.classList.toggle("running", evt.status === "running");
    node.classList.toggle("done", evt.status === "done");
    node.querySelector(".sn-timeline-tool").textContent = evt.tool || "";
    node.querySelector(".sn-timeline-detail").textContent = evt.detail || "";
  }

  function appendLog(selector, text, replace = false) {
    const el = document.querySelector(selector);
    if (!el) return;
    el.textContent = replace ? text : `${el.textContent}${el.textContent ? "\n" : ""}${text}`;
    el.scrollTop = el.scrollHeight;
  }

  function formatVerify(evt) {
    const bits = [`Turn ${evt.turn} — ${evt.passed ? "PASS" : "FAIL"} (${evt.signals_passed}/${evt.signals_ran})`, evt.summary];
    if (evt.tests_passed !== null && evt.tests_passed !== undefined) bits.push(`tests: ${evt.tests_passed ? "pass" : "fail"}`);
    if (evt.lint_passed !== null && evt.lint_passed !== undefined) bits.push(`lint: ${evt.lint_passed ? "pass" : "fail"}`);
    if (evt.critic_satisfied !== null && evt.critic_satisfied !== undefined) bits.push(`critic: ${evt.critic_satisfied ? "satisfied" : "not satisfied"}`);
    if (evt.thrashing !== null && evt.thrashing !== undefined) bits.push(`thrash: ${evt.thrashing ? "detected" : "clear"}`);
    return bits.join("\n");
  }

  function appendCheckpoint(evt) {
    const track = document.querySelector("#checkpoint-track");
    const node = document.createElement("span");
    node.className = `sn-checkpoint-node ${evt.verified ? "" : "rolled-back"}`;
    node.textContent = evt.turn;
    node.title = evt.verified ? `Turn ${evt.turn} — verified checkpoint` : `Turn ${evt.turn} — rolled back`;
    track.appendChild(node);
  }

  function appendLedgerEntryStub(evt) {
    const list = document.querySelector("#ledger-list");
    const div = document.createElement("div");
    div.className = `sn-ledger-entry ${evt.kind}`;
    div.innerHTML = `<div class="sn-ledger-kind">${evt.kind} · turn ${evt.turn}</div><div class="sn-ledger-title">${escapeHtml(evt.title)}</div>`;
    list.prepend(div);
    document.querySelector("#ledger-panel")?.classList.remove("hidden");
  }

  function renderRace(evt) {
    document.querySelector("#mission-race")?.classList.remove("hidden");
    const log = document.querySelector("#race-log");
    if (log.querySelector(".sn-dim")) log.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.innerHTML = `<div class="sn-panel-title" style="margin-bottom:6px">Turn ${evt.turn} · ${evt.task_type}</div>` +
      (evt.entrants || []).map((e) => `
        <div class="sn-race-entrant ${e.agent === evt.winner ? "winner" : ""}">
          <div class="sn-race-entrant-head"><span>${e.agent}${e.agent === evt.winner ? " 🏆" : ""}</span><span class="sn-race-entrant-score">score ${e.score}</span></div>
        </div>`).join("");
    log.appendChild(wrap);
    document.querySelector("#race-panel")?.classList.remove("hidden");
    refreshBandit();
  }

  function renderTracking(evt) {
    const gh = document.querySelector("#link-github");
    const lin = document.querySelector("#link-linear");
    if (evt.github_url) { gh.href = evt.github_url; gh.classList.remove("hidden"); }
    if (evt.linear_url) { lin.href = evt.linear_url; lin.classList.remove("hidden"); }
    document.querySelector("#tracking-detail").textContent = "Setup complete — entering build loop.";
  }

  function renderFollowUp(evt) {
    const list = document.querySelector("#follow-up-list");
    const li = document.createElement("li");
    li.textContent = evt.done ? `Done — ${evt.reason}` : `${evt.follow_up} (${evt.reason})`;
    list.prepend(li);
    while (list.children.length > 5) list.removeChild(list.lastChild);
  }

  function playByPlay(message) {
    const ul = document.querySelector("#play-by-play");
    const li = document.createElement("li");
    li.textContent = message;
    ul.prepend(li);
    while (ul.children.length > 20) ul.removeChild(ul.lastChild);
  }

  async function refreshSidePanels() {
    if (!state.projectId) return;
    try {
      const files = await request(`/projects/${state.projectId}/files`);
      const list = document.querySelector("#file-list");
      if (list) {
        list.innerHTML = files.map((f) => `<li>${escapeHtml(f)}</li>`).join("");
        document.querySelector("#files-section")?.classList.toggle("hidden", files.length === 0);
      }
    } catch (_) { /* ignore */ }
    refreshBandit();
  }

  async function refreshBandit() {
    if (!state.projectId) return;
    try {
      const data = await request(`/projects/${state.projectId}/bandit`);
      const el = document.querySelector("#race-leaderboard");
      const rates = data.win_rates || {};
      if (!Object.keys(rates).length) { el.innerHTML = `<p class="sn-dim">No race data yet.</p>`; return; }
      el.innerHTML = Object.entries(rates).map(([taskType, agents]) => `
        <div class="sn-race-task-group">
          <div class="sn-race-task-label">${taskType}</div>
          ${Object.entries(agents).map(([agent, rate]) => `
            <div class="sn-race-bar-row">
              <span class="sn-race-agent-name">${agent}</span>
              <span class="sn-race-bar-track"><span class="sn-race-bar-fill" style="width:${Math.round(rate * 100)}%"></span></span>
              <span class="sn-race-pct">${Math.round(rate * 100)}%</span>
            </div>`).join("")}
        </div>`).join("");
      document.querySelector("#race-panel")?.classList.toggle("hidden", !data.enabled && !Object.keys(rates).length);
    } catch (_) { /* ignore */ }
  }

  // ---------- settings ----------
  const ARRAY_FIELDS = new Set(["race_agents"]);
  const BOOL_FIELDS = new Set(["race_enabled", "schedule_enabled"]);

  async function loadSettings() {
    try {
      const secrets = await request("/secrets");
      const form = document.querySelector("#secrets-form");
      for (const [key, value] of Object.entries(secrets)) {
        const field = form.elements.namedItem(key);
        if (!field) continue;
        if (field.type === "checkbox") field.checked = Boolean(value);
        else if (ARRAY_FIELDS.has(key)) field.value = Array.isArray(value) ? value.join(",") : (value || "");
        else if (!String(value || "").includes("••••")) field.value = value ?? "";
      }
    } catch (_) { /* ignore */ }
  }

  document.querySelector("#secrets-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const body = {};
    for (const [key, value] of new FormData(form).entries()) {
      if (String(value).includes("••••")) continue;
      if (ARRAY_FIELDS.has(key)) { body[key] = String(value).split(",").map((s) => s.trim()).filter(Boolean); continue; }
      if (value === "") continue;
      body[key] = value;
    }
    for (const key of BOOL_FIELDS) {
      const field = form.elements.namedItem(key);
      if (field) body[key] = field.checked; // only send bools this form actually has a control for
    }
    ["max_race_turns", "max_turn_budget", "ledger_context_budget", "verify_min_signals_pass"].forEach((k) => {
      if (body[k] !== undefined) body[k] = Number(body[k]);
    });
    const status = document.querySelector("#secrets-status");
    try {
      await request("/secrets", { method: "PUT", body: JSON.stringify(body) });
      status.textContent = "Saved.";
      loadHealth();
    } catch (err) {
      status.textContent = err.message;
    }
  });

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  document.querySelectorAll(".sn-mtab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".sn-mtab").forEach((t) => t.classList.toggle("active", t === tab));
      document.querySelectorAll(".sn-mpane").forEach((p) => p.classList.toggle("hidden", p.id !== `pane-${tab.dataset.pane}`));
    });
  });

  loadHealth();
  loadProjects();
  loadSettings();
  setInterval(loadHealth, 30000);
})();
