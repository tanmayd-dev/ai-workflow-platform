/* ============================================================
   Atlas Ops console — vanilla JS, no frameworks, no build step.

   State: JWT in localStorage, per-section caches, two drawers
   (workflow detail, run detail). The run drawer polls its run
   every 3s while the run is RUNNING.
   ============================================================ */
"use strict";

const API = "/api/v1";
const TOKEN_KEY = "atlas_token";
const STEP_TYPES = ["AI_ANALYSIS", "DOMAIN_RULE", "HUMAN_APPROVAL", "CONDITIONAL"];

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/* ---------------- state ---------------- */
let token = localStorage.getItem(TOKEN_KEY) || "";
const workflowCache = new Map();   // id -> workflow (with steps)
let currentRunId = null;           // run shown in the drawer
let runPollTimer = null;
let runsFilter = "";               // "" = ALL
let selectedRunRow = null;
let wfForSubmit = [];              // APPROVED workflows for the submit form

/* ---------------- tiny helpers ---------------- */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  const sameDay = new Date().toDateString() === d.toDateString();
  return sameDay
    ? d.toLocaleTimeString(undefined, { hour12: false })
    : d.toISOString().replace("T", " ").slice(0, 19);
}

function shortId(id) { return id ? String(id).slice(0, 8) : "—"; }

function pretty(v) {
  if (v === null || v === undefined) return "—";
  try { return typeof v === "string" ? v : JSON.stringify(v, null, 2); }
  catch { return String(v); }
}

function detailText(res, data) {
  const d = data && data.detail;
  if (d === undefined || d === null) return `HTTP ${res.status}`;
  if (typeof d === "string") return d;
  // FastAPI 422 validation payloads: array of {loc, msg} — or a wrapped object
  if (Array.isArray(d)) {
    return d.map((e) => `${(e.loc || []).join(".")}: ${e.msg}`).join("\n");
  }
  if (d.message) {
    const errs = Array.isArray(d.errors)
      ? "\n" + d.errors.map((e) => `${(e.loc || []).join(".")}: ${e.msg}`).join("\n")
      : "";
    return `${d.message}${errs}`;
  }
  return JSON.stringify(d, null, 2);
}

function toast(msg, kind = "info") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 260); }, 4200);
}

/* ---------------- API client ---------------- */
async function api(path, { method = "GET", body } = {}) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(API + path, {
      method, headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      redirect: "follow",
    });
  } catch (e) {
    throw new Error("Network error — is the API reachable?");
  }
  if (res.status === 401 && path !== "/auth/login") { onUnauthorized(); throw new Error("Session expired — sign in again"); }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const err = new Error(detailText(res, data));
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ---------------- auth ---------------- */
function showGate() {
  $("#gate").hidden = false;
  $("#shell").hidden = true;
  $("#login-email").focus();
}

function onUnauthorized() {
  token = "";
  localStorage.removeItem(TOKEN_KEY);
  stopRunPoll();
  showGate();
}

async function doLogin(evt) {
  evt.preventDefault();
  const email = $("#login-email").value.trim();
  const pass = $("#login-password").value;
  const errBox = $("#login-error");
  errBox.hidden = true;
  if (!email || !pass) { errBox.textContent = "Enter your work email and password."; errBox.hidden = false; return; }
  const btn = $("#login-submit");
  btn.disabled = true;
  try {
    // OAuth2-compatible password form (the API expects form-encoded here)
    let res = await fetch(`${API}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ username: email, password: pass }),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error(detailText(res, data));
    token = data.access_token;
    localStorage.setItem(TOKEN_KEY, token);
    await boot();
  } catch (e) {
    errBox.textContent = e.message;
    errBox.hidden = false;
  } finally {
    btn.disabled = false;
  }
}

$("#signout").addEventListener("click", onUnauthorized);
/* The login form must preventDefault (native submit would just reload /ui) */
$("#login-form").addEventListener("submit", doLogin);

/* ---------------- boot / shell ---------------- */
async function boot() {
  try {
    const me = await api("/auth/me");
    $("#gate").hidden = true;
    $("#shell").hidden = false;
    $("#user-email").textContent = me.email;
    switchSection("sec-intents");
    // Preload what several sections share; failures stay non-fatal per section
    refreshWorkflows(true);
  } catch {
    /* onUnauthorized already handled inside api() */
  }
}

function switchSection(id) {
  $$(".rail-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.sec === id));
  $$(".panel").forEach((p) => { p.hidden = p.id !== id; });
  if (id === "sec-intents") $("#intent-text").focus();
  if (id === "sec-workflows") renderWorkflows();
  if (id === "sec-runs") refreshRuns();
  if (id === "sec-audit") refreshAudit();
}
$$(".rail-btn").forEach((b) => b.addEventListener("click", () => switchSection(b.dataset.sec)));

/* ============================================================
   01 · Intent → Draft
   ============================================================ */
let intentDraft = null;

$("#intent-draft-btn").addEventListener("click", async () => {
  const box = $("#intent-error");
  box.hidden = true;
  const intent = $("#intent-text").value.trim();
  const domain = $("#intent-domain").value.trim();
  if (!intent) { box.textContent = "Write the intent first — what should happen, and to what?"; box.hidden = false; return; }
  if (!domain) { box.textContent = "Pick or type a domain (e.g. fraud, kyc)."; box.hidden = false; return; }
  const btn = $("#intent-draft-btn");
  btn.disabled = true; btn.textContent = "Drafting…";
  try {
    const res = await api("/intents/draft", { method: "POST", body: { intent, domain } });
    intentDraft = res.workflow_draft;
    $("#intent-result").hidden = false;
    $("#intent-name").value = intentDraft.name || "";
    $("#intent-draft-json").textContent = pretty(intentDraft);
    $("#intent-context").textContent = res.retrieved_context || "";
    toast("Draft generated — review it before filing.", "ok");
  } catch (e) {
    box.textContent = e.message;
    box.hidden = false;
    toast(e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "Draft workflow";
  }
});

$("#intent-create-btn").addEventListener("click", async () => {
  if (!intentDraft) return;
  const box = $("#intent-error");
  box.hidden = true;
  const name = $("#intent-name").value.trim();
  if (!name) { box.textContent = "Give the workflow a name before filing."; box.hidden = false; return; }
  const btn = $("#intent-create-btn");
  btn.disabled = true;
  try {
    const wf = await api("/workflows/draft", {
      method: "POST",
      body: {
        name,
        description: intentDraft.description ?? null,
        domain: $("#intent-domain").value.trim(),
        steps: (intentDraft.steps || []).map((s, i) => ({
          order_index: s.order_index ?? i,
          step_type: s.step_type,
          name: s.name ?? `Step ${i + 1}`,
          description: s.description ?? null,
          config_json: s.config_json ?? {},
        })),
      },
    });
    toast(`Draft filed: ${wf.name} (${shortId(wf.id)})`, "ok");
    intentDraft = null;
    $("#intent-result").hidden = true;
    await refreshWorkflows(true);
    switchSection("sec-workflows");
    openWorkflow(wf.id);
  } catch (e) {
    box.textContent = e.message;
    box.hidden = false;
    toast(e.message, "err");
  } finally {
    btn.disabled = false;
  }
});

/* ============================================================
   02 · Workflows
   ============================================================ */
async function refreshWorkflows(silent = false) {
  try {
    const all = await api("/workflows/");
    all.forEach((w) => workflowCache.set(w.id, w));
    wfForSubmit = all.filter((w) => w.status === "APPROVED");
    fillRunWorkflowSelect();
    renderWorkflows();
    renderSubmitOptions();
  } catch (e) {
    if (!silent) { toast(e.message, "err"); }
  }
}

$("#wf-refresh").addEventListener("click", () => refreshWorkflows());

function renderWorkflows() {
  const rows = Array.from(workflowCache.values())
    .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const tbody = $("#wf-list");
  tbody.innerHTML = rows.map((w) => `
    <tr data-id="${esc(w.id)}" class="${currentWfId === w.id ? "is-open" : ""}">
      <td>${esc(w.name)}</td>
      <td><span class="badge">${esc(w.domain)}</span></td>
      <td><span class="pill" data-status="${esc(w.status)}">${esc(w.status)}</span></td>
      <td class="cell-time">${esc(fmtTime(w.created_at))}</td>
      <td class="cell-id">v${esc(w.version)}</td>
      <td class="cell-id">${esc(shortId(w.id))}</td>
    </tr>`).join("");
  $("#wf-empty").hidden = rows.length > 0;
  $$("#wf-list tr").forEach((tr) =>
    tr.addEventListener("click", () => openWorkflow(tr.dataset.id)));
}

/* ---- workflow detail drawer ---- */
let currentWfId = null;

function fetchWorkflow(id) {
  if (workflowCache.has(id)) return Promise.resolve(workflowCache.get(id));
  return api(`/workflows/${id}`);
}

async function openWorkflow(id) {
  const drawer = $("#drawer-wf");
  let wf;
  try { wf = await fetchWorkflow(id); }
  catch (e) { toast(e.message, "err"); return; }
  workflowCache.set(id, wf);
  if (currentRunId) closeDrawer("drawer-run", { keepScrim: true });
  currentWfId = id;
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  $("#scrim").hidden = false;
  renderWorkflows();

  $("#wf-d-title").textContent = `${wf.name}`;
  $("#wf-d-error").hidden = true;
  $("#wf-d-meta").innerHTML = `
    <dt>status</dt><dd><span class="pill" data-status="${esc(wf.status)}">${esc(wf.status)}</span></dd>
    <dt>domain</dt><dd>${esc(wf.domain)}</dd>
    <dt>version</dt><dd>v${esc(wf.version)}</dd>
    <dt>id</dt><dd>${esc(wf.id)}</dd>
    <dt>created</dt><dd>${esc(fmtTime(wf.created_at))}</dd>
    <dt>approved</dt><dd>${esc(fmtTime(wf.approved_at))}</dd>`;

  const isDraft = wf.status === "DRAFT";
  $("#wf-d-actions").hidden = !isDraft;
  $("#wf-d-edit-wrap").hidden = !isDraft;
  $("#wf-d-edit-wrap").open = false;

  const steps = [...(wf.steps || [])].sort((a, b) => a.order_index - b.order_index);
  $("#wf-d-steps").innerHTML = steps.map((s) => `
    <li>
      <div class="step-line">
        <span class="step-name">${esc(s.name)}</span>
        <span class="badge" data-type="${esc(s.step_type)}">${esc(s.step_type)}</span>
        <span class="cell-id mono">#${esc(s.order_index)}</span>
      </div>
      ${s.description ? `<div class="step-desc">${esc(s.description)}</div>` : ""}
      ${s.step_type === "CONDITIONAL"
        ? `<details class="config"><summary>CONFIG</summary><pre class="mono">${esc(pretty(s.config_json))}</pre></details>
           <details class="raw"><summary>BRANCHES</summary>${branchBadges(s.config_json)}</details>`
        : `<details class="config"><summary>CONFIG</summary><pre class="mono">${esc(pretty(s.config_json))}</pre></details>`}
    </li>`).join("");

  // Prefill the edit form with the current draft
  $("#wf-edit-name").value = wf.name || "";
  $("#wf-edit-desc").value = wf.description || "";
  $("#wf-edit-steps").value = pretty(steps.map((s) => ({
    order_index: s.order_index,
    step_type: s.step_type,
    name: s.name,
    description: s.description ?? null,
    config_json: s.config_json ?? {},
  })));
  $("#wf-edit-validate").hidden = true;
}

function branchBadges(cfg) {
  // Conditional bodies are surfaced as list badges: {true: ..., false: ...}
  let bodies = {};
  const inner = cfg && cfg.config_json !== undefined && cfg.config_json !== null && typeof cfg.config_json === "object"
    ? { ...cfg.config_json, ...cfg } : { ...(cfg || {}) };
  for (const [k, v] of Object.entries(inner)) {
    if (k === "true_body" || k === "false_body" || k === "true" || k === "false" || k === "body") {
      if (v && typeof v === "object") bodies[k] = v;
    } else if (v && typeof v === "object" && (v.condition !== undefined || v.true_body || v.false_body)) {
      bodies[k] = v;
    }
  }
  const keys = Object.keys(bodies);
  if (!keys.length) return '<span class="muted mono-inline">(no branch bodies declared)</span>';
  return `<div class="badge-list">${keys.map((k) => `<span class="badge">${esc(k)}: ${esc(shortId(bodies[k].step_type ?? "steps"))}</span>`).join(" ")}</div>`;
}

$("#wf-d-approve-btn").addEventListener("click", async () => {
  if (!currentWfId) return;
  const err = $("#wf-d-error");
  err.hidden = true;
  const btn = $("#wf-d-approve-btn");
  btn.disabled = true;
  try {
    const wf = await api(`/workflows/${currentWfId}/approve`, { method: "POST" });
    workflowCache.set(currentWfId, wf);
    openWorkflow(currentWfId);
    refreshWorkflows(true);
    toast(`Approved: ${wf.name}`, "ok");
  } catch (e) {
    err.textContent = e.message;   // 403 (role) / 422 (validation) shown inline
    err.hidden = false;
  } finally { btn.disabled = false; }
});

/* ---- client-side validation of edited steps (readable errors) ---- */
function validateStepsSteps(steps) {
  const errs = [];
  if (!Array.isArray(steps) || steps.length === 0) {
    return ["Steps must be a non-empty JSON array."];
  }
  const seenOrder = new Set(), seenNames = new Set();
  steps.forEach((s, i) => {
    const at = `step ${i + 1}`;
    if (typeof s !== "object" || s === null || Array.isArray(s)) { errs.push(`${at}: must be a JSON object.`); return; }
    if (!s.name || typeof s.name !== "string") errs.push(`${at}: "name" is required (non-empty string).`);
    else if (seenNames.has(s.name)) errs.push(`${at}: duplicate name "${s.name}".`);
    else seenNames.add(s.name);
    if (!Number.isInteger(s.order_index) || s.order_index < 0) errs.push(`${at}: "order_index" must be a non-negative integer.`);
    else if (seenOrder.has(s.order_index)) errs.push(`${at}: duplicate order_index ${s.order_index}.`);
    else seenOrder.add(s.order_index);
    if (!STEP_TYPES.includes(s.step_type)) errs.push(`${at}: "step_type" must be one of ${STEP_TYPES.join(", ")} (got "${s.step_type}").`);
    if (s.description !== undefined && s.description !== null && typeof s.description !== "string") errs.push(`${at}: "description" must be a string or omitted.`);
    if (s.config_json !== undefined && s.config_json !== null && (typeof s.config_json !== "object" || Array.isArray(s.config_json))) {
      errs.push(`${at}: "config_json" must be a JSON object.`);
    }
  });
  return errs;
}

$("#wf-edit-save").addEventListener("click", async () => {
  if (!currentWfId) return;
  const box = $("#wf-edit-validate");
  box.hidden = true;
  const stepsText = $("#wf-edit-steps").value;
  let steps;
  try {
    steps = JSON.parse(stepsText);
  } catch (e) {
    box.textContent = `Steps JSON does not parse: ${e.message}`;
    box.hidden = false;
    return;
  }
  const clientErrors = validateStepsSteps(steps);
  if (clientErrors.length) {
    box.textContent = "Fix these before saving:\n" + clientErrors.join("\n");
    box.hidden = false;
    return;
  }
  const wf = workflowCache.get(currentWfId);
  const nameChanged = $("#wf-edit-name").value.trim() !== (wf?.name ?? "");
  const descChanged = $("#wf-edit-desc").value !== (wf?.description ?? "");
  const payload = {
    ...(nameChanged ? { name: $("#wf-edit-name").value.trim() } : {}),
    ...(descChanged ? { description: $("#wf-edit-desc").value || null } : {}),
    steps: steps.map((s) => ({
      order_index: s.order_index,
      step_type: s.step_type,
      name: s.name,
      description: s.description ?? null,
      config_json: s.config_json ?? {},
    })),
  };
  if (payload.steps === undefined && !nameChanged && !descChanged) {
    box.textContent = "Nothing to change — edit the name, description, or steps."; box.hidden = false;
    return;
  }
  const btn = $("#wf-edit-save");
  btn.disabled = true;
  try {
    const updated = await api(`/workflows/${currentWfId}`, { method: "PATCH", body: payload });
    workflowCache.set(currentWfId, updated);
    openWorkflow(currentWfId);   // re-render drawer with server round-trip steps
    refreshWorkflows(true);
    toast("Draft saved.", "ok");
  } catch (e) {
    // 422 (approval-validator gate), 409 (not DRAFT), 403 (role), 400 (workflow missing? no — 404)
    box.textContent = e.message;
    box.hidden = false;
  } finally { btn.disabled = false; }
});

/* ============================================================
   03 · Runs
   ============================================================ */
$("#runs-refresh").addEventListener("click", () => refreshRuns());

const chipBox = $(".chips");
chipBox.addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  $$(".chip").forEach((c) => c.classList.toggle("is-active", c === chip));
  runsFilter = chip.dataset.status;
  refreshRuns();
});

async function refreshRuns() {
  const q = runsFilter ? `?status=${encodeURIComponent(runsFilter)}` : "";
  let runs;
  try {
    runs = await api(`/runs${q}`);
  } catch (e) {
    // tolerate a 400 from an unknown status token (e.g. a future CANCELLED)
    toast(e.message, "err");
    return;
  }
  const rows = runs;
  const tbody = $("#runs-list");
  tbody.innerHTML = rows.map((r) => {
    const wf = workflowCache.get(r.workflow_id);
    return `
    <tr data-id="${esc(r.id)}" class="${selectedRunRow === r.id ? "is-open" : ""}">
      <td><span class="pill" data-status="${esc(r.status)}">${esc(r.status)}</span></td>
      <td>${outcomeCell(r)}</td>
      <td class="cell-id">${esc(r.account_id)}</td>
      <td>${esc(wf ? wf.name : shortId(r.workflow_id))}</td>
      <td class="cell-time">${esc(fmtTime(r.started_at))}</td>
      <td class="cell-id">${esc(shortId(r.id))}</td>
    </tr>`;
  }).join("");
  $("#runs-empty").hidden = rows.length > 0;
  $$("#runs-list tr").forEach((tr) =>
    tr.addEventListener("click", () => openRun(tr.dataset.id)));
}

function fillRunWorkflowSelect() {
  const sel = $("#run-workflow");
  const current = sel.value;
  sel.innerHTML = wfForSubmit.length
    ? wfForSubmit.map((w) => `<option value="${esc(w.id)}">${esc(w.name)} · ${esc(w.domain)}</option>`).join("")
    : '<option value="">— no APPROVED workflows yet —</option>';
  if (wfForSubmit.some((w) => w.id === current)) sel.value = current;
}

function renderSubmitOptions() {
  const sel = $("#run-workflow");
  const btn = $("#run-submit-btn");
  const ok = wfForSubmit.length > 0;
  btn.disabled = !ok;
  sel.disabled = !ok;
  if (!ok) {
    // Keep a hint visible for the first-time user
    sel.innerHTML = '<option value="">Approve a workflow first (02 Workflows)</option>';
  }
}

/* account id keeps its "ACC-" prefix as you type */
$("#run-account").addEventListener("input", (e) => {
  let v = e.target.value;
  if (!v.startsWith("ACC-")) {
    v = "ACC-" + v.replace(/^-+|^ACC-+/g, "");
    e.target.value = v;
  }
});

$("#run-submit-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const box = $("#run-submit-error");
  box.hidden = true;
  const workflowId = $("#run-workflow").value;
  const accountId = $("#run-account").value.trim();
  let input = {};
  try { input = JSON.parse($("#run-input").value || "{}"); }
  catch (err) {
    box.textContent = `Input data is not valid JSON: ${err.message}`;
    box.hidden = false; return;
  }
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    box.textContent = "Input data must be a JSON object (e.g. {\"amount\": 9000}).";
    box.hidden = false; return;
  }
  if (!workflowId) { box.textContent = "Pick a workflow (or approve one in 02 Workflows)."; box.hidden = false; return; }
  if (!accountId) { box.textContent = "Give the run an account id (ACC-…)."; box.hidden = false; return; }
  const trigger = $("#run-trigger").value.trim();
  const btn = $("#run-submit-btn");
  btn.disabled = true;
  try {
    const run = await api("/runs/", {
      method: "POST",
      body: { workflow_id: workflowId, account_id: accountId, input_data: input, trigger_reason: trigger || null },
    });
    toast(`Run accepted (202) — ${shortId(run.id)} on ${run.account_id}`, "ok");
    selectedRunRow = run.id;
    await refreshRuns();
    await openRun(run.id);           // drawer attaches + starts polling
  } catch (err) {
    // 409 means the account already has an active run — show it clearly
    box.textContent = err.status === 409 ? `Blocked: ${err.message}` : err.message;
    box.hidden = false;
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
    renderSubmitOptions();
  }
});

/* ---- run detail drawer ---- */
function stopRunPoll() {
  if (runPollTimer) { clearInterval(runPollTimer); runPollTimer = null; }
}

async function openRun(id) {
  stopRunPoll();
  currentRunId = id;
  selectedRunRow = id;
  const drawer = $("#drawer-run");
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  $("#scrim").hidden = false;
  renderWorkflows();
  await refreshRunPanel(true);
  startRunPoll();
}

function startRunPoll() {
  stopRunPoll();
  runPollTimer = setInterval(async () => {
    if (!currentRunId) return stopRunPoll();
    // Refresh the list quietly + the open run's panel every 3s while running
    refreshRuns();
    try {
      const run = await api(`/runs/${currentRunId}`);
      if (run.status !== "RUNNING") {
        toast(`Run ${shortId(run.id)} → ${run.status}`, run.status === "FAILED" ? "err" : "ok");
        stopRunPoll();
      }
      await refreshRunPanel();
    } catch { /* transient network hiccup: next tick retries */ }
  }, 3000);
}

/* One-line human-readable summary for an execution row (details stay under
   Snapshots/JSON). Structure-tolerant: works with dict or scalar outputs. */
function execHint(x) {
  const o = x.output_snapshot;
  if (o === null || o === undefined) return "";
  const out = (typeof o === "object") ? o : { value: o };
  if (out.approved !== undefined) {
    const verb = out.approved ? "Approved" : "Rejected";
    const by = out.approver ? ` by ${out.approver}` : "";
    const note = (out.notes || out.reason) ? ` — ${out.notes || out.reason}` : "";
    return `${verb}${by}${note}`;
  }
  if (out.condition_met !== undefined) {
    return out.next ? `Branch → ${out.next}` : `Condition ${out.condition_met ? "met" : "not met"}`;
  }
  if (out.rule !== undefined && Object.keys(out).length === 1) return "Rule referenced, not evaluated";
  if (out.content !== undefined) return String(out.content).slice(0, 140);
  if (x.rule_evaluation_result !== null && x.rule_evaluation_result !== undefined) {
    return `Rule result: ${JSON.stringify(x.rule_evaluation_result)}`;
  }
  const text = typeof o === "string" ? o : JSON.stringify(o);
  return text.length > 140 ? `${text.slice(0, 140)}…` : text;
}

/* ---- machine-readable decision extraction (runs table + drawer banner) ---- */
/* Mirrors WorkflowExecutor._decision_verdict: read the compact JSON contract
   the AI/action steps emit. Falls back to undefined when the payload carries
   no contract (then the UI shows the raw payload only). */
function runDecision(run) {
  const out = run.output_data;
  if (out && typeof out === "object" && !Array.isArray(out) && out.verdict) return out.verdict;
  return null;
}

function verdictTone(verdict, run) {
  if (!verdict || !verdict.action) return (run && run.status === "PAUSED") ? "amber" : "neutral";
  const action = String(verdict.action).toUpperCase();
  if (/SUSPEND|BLOCK|REJECT|CANCEL|DENY|FREEZE|TERMINAT/.test(action)) return "red";
  if (/ALLOW|APPROVE|CLEAR|NO_ACTION|PROCEED|PASS|RELEASE/.test(action)) return "green";
  return "amber"; /* REVIEW / unknown → attention */
}

function verdictChips(verdict, run) {
  const chips = [];
  if (verdict.risk_tier) {
    chips.push(`<span class="vchip" data-tone="${esc(verdictTone(verdict, run))}">risk ${esc(String(verdict.risk_tier).toUpperCase())}${verdict.risk_score !== undefined ? ` · ${esc(String(verdict.risk_score))}` : ""}</span>`);
  }
  if (verdict.account) chips.push(`<span class="vchip">account ${esc(verdict.account)}</span>`);
  if (verdict.transaction_id) chips.push(`<span class="vchip mono">tx ${esc(verdict.transaction_id)}</span>`);
  return chips.join("");
}

/* Runs-table outcome cell: big colored action badge, or fallback pill when no
   decision contract exists yet (mock/legacy runs). */
function outcomeCell(run) {
  const verdict = runDecision(run);
  if (verdict && verdict.action) {
    const tone = verdictTone(verdict, run);
    return `<span class="verdict-badge" data-tone="${esc(tone)}">${esc(String(verdict.action).toUpperCase())}</span>`;
  }
  return '<span class="muted">—</span>';
}

/* Drawer banner: half-second glanceability for the decision + its context. */
function renderVerdictBanner(run) {
  const box = $("#run-d-verdict");
  const verdict = runDecision(run);
  const reviews = (run.output_data && run.output_data.human_reviews) || {};
  const lastReviewKey = Object.keys(reviews).pop();
  const review = lastReviewKey ? reviews[lastReviewKey] : null;
  const tone = verdictTone(verdict, run);
  const icon = tone === "red" ? "🔴" : tone === "green" ? "🟢" : tone === "amber" ? "🟡" : "⚪";

  let action;
  if (verdict && verdict.action) action = String(verdict.action).toUpperCase();
  else if (run.status === "PAUSED") action = "AWAITING SUPERVISOR REVIEW";
  else {
    box.hidden = true;
    return;
  }

  const chips = verdict ? verdictChips(verdict, run) : "";
  const reviewer = review ? (review.approver ? ` by ${review.approver}` : "") : "";
  const note = review && review.notes && review.notes !== "Approved by human reviewer"
    ? `<div class="v-note">“${esc(review.notes)}”<span class="v-note-by"> — supervisor note${esc(reviewer)}</span></div>`
    : (review && review.approver ? `<div class="v-note-by">reviewed by ${esc(review.approver)}</div>` : "");
  const summary = verdict && verdict.summary ? `<div class="v-summary">${esc(verdict.summary)}</div>` : "";
  const account = run.account_id ? `<span class="vchip">account ${esc(run.account_id)}</span>` : "";

  box.hidden = false;
  box.innerHTML = `
    <div class="verdict-banner" data-tone="${esc(tone)}">
      <div class="verdict-action">${icon} ${esc(action)}</div>
      ${summary}
      <div class="v-chips">${account}${chips}</div>
      ${note}
    </div>`;
}

async function refreshRunPanel(keepScroll = false) {
  const id = currentRunId;
  if (!id) return;
  const scroll = $("#drawer-run .drawer-body");
  const st = keepScroll ? 0 : scroll.scrollTop;
  const sbox = $("#run-d-error");
  let run;
  try { run = await api(`/runs/${id}`); }
  catch (e) {
    sbox.textContent = e.message; sbox.hidden = false;
    return;
  }
  let execs = [];
  try { execs = await api(`/runs/${id}/executions`); }
  catch (e) { /* keep the run even if executions are unavailable */ }

  $("#run-d-title").textContent = `RUN ${shortId(run.id)}`;
  sbox.hidden = true;
  $("#run-d-meta").innerHTML = `
    <dt>status</dt><dd><span class="pill" data-status="${esc(run.status)}">${esc(run.status)}</span></dd>
    <dt>account</dt><dd>${esc(run.account_id)}</dd>
    <dt>workflow</dt><dd>${esc(workflowCache.get(run.workflow_id)?.name ?? shortId(run.workflow_id))}</dd>
    <dt>run id</dt><dd>${esc(run.id)}</dd>
    <dt>step</dt><dd>${esc(run.current_step_index)}</dd>
    <dt>started</dt><dd>${esc(fmtTime(run.started_at))}</dd>
    <dt>ended</dt><dd>${esc(fmtTime(run.completed_at))}</dd>`;

  const errEl = $("#run-d-error-message");
  errEl.hidden = !run.error_message;
  errEl.textContent = run.error_message || "";

  $("#run-d-input").textContent =
    pretty((run.output_data && typeof run.output_data === "object" && !Array.isArray(run.output_data))
      ? run.output_data.input : undefined);

  renderVerdictBanner(run);
  $("#run-d-output").textContent = pretty(run.output_data);

  $("#run-d-actions").hidden = run.status !== "PAUSED";
  $("#run-d-note").value = "";

  $("#run-d-executions").innerHTML = execs.map((x) => `
    <li data-status="${esc(x.status)}">
      <div class="tl-line">
        <span class="badge" data-type="${esc(x.node_type)}">${esc(x.node_type)}</span>
        <span class="mono-inline">#${esc(shortId(x.id))}</span>
        <span class="pill" data-status="${esc(x.status)}">${esc(x.status)}</span>
      </div>
      <div class="tl-time">${esc(fmtTime(x.started_at))} → ${esc(fmtTime(x.completed_at))}</div>
      ${execHint(x) ? `<div class="exec-hint">${esc(execHint(x))}</div>` : ""}
      ${x.error_message ? `<div class="tl-error">✗ ${esc(x.error_message)}</div>` : ""}
      <details class="snap"><summary>Snapshots/JSON</summary>
        ${x.prompt_snapshot ? `<div class="mono-inline">prompt</div><pre>${esc(x.prompt_snapshot)}</pre>` : ""}
        <div class="mono-inline">input</div><pre>${esc(pretty(x.input_snapshot))}</pre>
        <div class="mono-inline">output</div><pre>${esc(pretty(x.output_snapshot))}</pre>
        ${x.rule_evaluation_result !== null && x.rule_evaluation_result !== undefined
          ? `<div class="mono-inline">rule result</div><pre>${esc(pretty(x.rule_evaluation_result))}</pre>` : ""}
        ${x.retrieved_context ? `<div class="mono-inline">retrieved context</div><pre>${esc(pretty(x.retrieved_context))}</pre>` : ""}
      </details>
    </li>`).join("");
  if (!execs.length) {
    $("#run-d-executions").innerHTML =
      '<li><span class="muted">No node executions recorded yet.</span></li>';
  }
  if (!keepScroll) scroll.scrollTop = st;
}

/* run approve / reject (approver role; 409 maker–checker denials surface here) */
$("#run-d-approve").addEventListener("click", () => runVerdict("approve"));
$("#run-d-reject").addEventListener("click", () => runVerdict("reject"));

async function runVerdict(kind) {
  const id = currentRunId;
  if (!id) return;
  const note = $("#run-d-note").value.trim();
  const sbox = $("#run-d-error");
  sbox.hidden = true;
  const box = kind === "approve" ? $("#run-d-approve") : $("#run-d-reject");
  box.disabled = true;
  try {
    const body = kind === "approve" ? { notes: note || null } : { reason: note || null };
    const run = await api(`/runs/${id}/${kind}`, { method: "POST", body });
    toast(`${kind === "approve" ? "Approved" : "Rejected"}: ${shortId(run.id)} → ${run.status}`, "ok");
    refreshRuns();
    await refreshRunPanel();
  } catch (e) {
    sbox.textContent = e.message;   // 403 role gate, 409 maker-checker race, 400 not PAUSED
    sbox.hidden = false;
  } finally {
    box.disabled = false;
  }
}

/* ============================================================
   04 · Audit
   ============================================================ */
$("#audit-filter-form").addEventListener("submit", (e) => { e.preventDefault(); refreshAudit(); });
$("#audit-apply").addEventListener("click", (e) => { e.preventDefault(); refreshAudit(); });

async function refreshAudit() {
  const params = new URLSearchParams();
  const t = $("#audit-type").value.trim();
  const eid = $("#audit-entity-id").value.trim();
  if (t) params.set("entity_type", t);
  if (eid) params.set("entity_id", eid);
  params.set("limit", "100");
  const qs = params.toString() ? `?${params}` : "?limit=100";
  const tbody = $("#audit-list");
  let rows;
  try {
    rows = await api(`/audit-logs${qs}`);
  } catch (e) {
    // 403 (needs auditor/admin) is the common case here — explain it
    tbody.innerHTML = "";
    $("#audit-empty").hidden = false;
    $("#audit-empty").innerHTML = `<div class="form-error">${esc(e.message)}</div>
      <p>The audit trail is visible to <strong>auditor</strong> and <strong>admin</strong> roles only.
      Sign in with such an account to observe approvals, edits, and run verdicts.</p>`;
    return;
  }
  tbody.innerHTML = rows.map((r) => `
    <tr data-details="${esc(pretty(r))}">
      <td class="cell-time">${esc(fmtTime(r.timestamp))}</td>
      <td><span class="badge">${esc(r.action)}</span></td>
      <td>
        ${r.actor_role ? `<span class="badge">${esc(r.actor_role)}</span>` : ""}
        <span class="cell-id">${esc(shortId(r.actor_id))}</span>
      </td>
      <td>
        <span class="badge">${esc(r.entity_type)}</span>
        <span class="cell-id">${esc(shortId(r.entity_id))}</span>
      </td>
    </tr>`).join("");
  $("#audit-empty").hidden = rows.length > 0;
  $("#audit-empty").innerHTML = `
    No audit rows${t || eid ? " for this filter" : ""} yet. Every approval, draft edit, and run verdict
    writes a row here — try <strong>01 Intent → Draft</strong> or approve something to generate events.`;
}

/* expandable details under every audit row */
$("#audit-list").addEventListener("click", (e) => {
  const tr = e.target.closest("tr");
  if (!tr) return;
  if (e.target.closest(".details-row")) return;
  let next = tr.nextElementSibling;
  if (next && next.classList.contains("details-row")) { next.remove(); tr.classList.remove("is-open"); return; }
  const details = document.createElement("tr");
  details.className = "details-row";
  details.innerHTML = `<td colspan="4"><div class="audit-details"><pre>${esc(tr.dataset.details)}</pre></div></td>`;
  tr.classList.add("is-open");
  tr.after(details);
});

/* ---------------- drawers: close + scrim ---------------- */
function closeDrawer(id, { keepScrim = false } = {}) {
  const d = document.getElementById(id);
  d.classList.remove("open");
  d.setAttribute("aria-hidden", "true");
  if (id === "drawer-run") { currentRunId = null; stopRunPoll(); }
  if (id === "drawer-wf") currentWfId = null;
  if (!keepScrim && !$("#drawer-run").classList.contains("open") && !$("#drawer-wf").classList.contains("open")) {
    $("#scrim").hidden = true;
  }
  renderWorkflows();
}

$(".scrim").addEventListener("click", () => {
  if ($("#drawer-run").classList.contains("open")) closeDrawer("drawer-run");
  else if ($("#drawer-wf").classList.contains("open")) closeDrawer("drawer-wf");
});
$$(".drawer-close").forEach((b) =>
  b.addEventListener("click", () => closeDrawer(b.dataset.drawer)));

/* Escape closes drawers too (keyboard parity) */
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if ($("#drawer-run").classList.contains("open")) closeDrawer("drawer-run");
    else if ($("#drawer-wf").classList.contains("open")) closeDrawer("drawer-wf");
  }
});

/* ---------------- start ---------------- */
if (token) boot(); else showGate();
