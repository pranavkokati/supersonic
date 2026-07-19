const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

let step = 0;
const total = 4;

function renderSteps() {
  const el = $("#ob-steps");
  if (!el) return;
  el.innerHTML = Array.from({ length: total }, (_, i) => {
    const cls = i === step ? "active" : i < step ? "done" : "";
    return `<span class="${cls}" aria-hidden="true"></span>`;
  }).join("");
}

function showStep(n) {
  step = Math.max(0, Math.min(total - 1, n));
  $$(".ob-panel").forEach((panel) => panel.classList.toggle("hidden", Number(panel.dataset.step) !== step));
  renderSteps();
}

async function api(path, opts = {}) {
  const response = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json", ...opts.headers },
    ...opts,
  });
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (_) { /* non-JSON */ }
  if (!response.ok) throw new Error(data.detail || text || `Request failed (${response.status})`);
  return data;
}

function formBody(form) {
  const body = {};
  for (const [key, value] of new FormData(form).entries()) {
    if (value !== "" && !String(value).includes("••••")) body[key] = String(value).trim();
  }
  return body;
}

$$(".ob-next").forEach((btn) => btn.addEventListener("click", () => showStep(step + 1)));
$$(".ob-back").forEach((btn) => btn.addEventListener("click", () => showStep(step - 1)));

$("#ob-keys-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const status = $("#ob-keys-status");
  const body = formBody(form);
  if (!body.anthropic_api_key && !body.openai_api_key && !body.ollama_base_url) {
    status.textContent = "Add at least one provider — Anthropic, OpenAI, or a local Ollama URL.";
    return;
  }
  try {
    await api("/secrets", { method: "PUT", body: JSON.stringify(body) });
    const health = await api("/health");
    const provider = health.providers?.[0];
    $("#ob-ready-lead").textContent = provider
      ? `Connected via ${provider}. You're ready to build.`
      : "Saved — add a provider key any time from Settings.";
    showStep(step + 1);
  } catch (err) {
    status.textContent = err.message;
  }
});

renderSteps();
