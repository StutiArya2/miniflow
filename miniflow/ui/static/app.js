/* MiniFlow dashboard: vanilla JS + SVG, no dependencies.
 *
 * Data flow: GET /api/runs (every 3 s) for the runs table; GET /api/runs/<id>
 * for one run; then an EventSource on /api/runs/<id>/events pushes task,
 * attempt and run changes until the run ends.
 */
"use strict";

const STATES = ["PENDING", "READY", "RUNNING", "RETRYING", "SUCCESS", "FAILED", "SKIPPED"];
const TERMINAL = new Set(["SUCCESS", "FAILED", "SKIPPED"]);
const STATE_WORDS = {
  PENDING: "waiting", READY: "ready", RUNNING: "running", RETRYING: "retrying",
  SUCCESS: "done", FAILED: "failed", SKIPPED: "skipped",
};
const SVG_NS = "http://www.w3.org/2000/svg";

const S = {
  runs: [],
  runId: null,
  run: null,          // run metadata
  graph: null,        // {tasks: {name: {kind, after}}}
  layout: null,       // {name: {col, row}}
  tasks: new Map(),   // name -> task row
  attempts: [],
  selected: null,
  follow: true,
  skew: 0,            // server clock - browser clock, seconds
  es: null,
  pollTimer: null,
  nodes: new Map(),   // name -> <g>
  edges: [],          // [{from, to, el}]
  control: null,      // GET /api/control, or null when the dashboard is read-only
  controlBusy: false, // a control request is in flight
  controlError: null, // why the last control request was refused
};

// ------------------------------------------------------------------ helpers
const $ = (id) => document.getElementById(id);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const nowServer = () => Date.now() / 1000 + S.skew;

function fmtDur(sec) {
  if (sec === null || sec === undefined || !isFinite(sec)) return "–";
  if (sec < 1) return `${Math.round(sec * 1000)} ms`;
  if (sec < 60) return `${sec.toFixed(sec < 10 ? 2 : 1)} s`;
  const m = Math.floor(sec / 60), r = Math.round(sec % 60);
  return `${m}m ${String(r).padStart(2, "0")}s`;
}

function fmtClock(ts) {
  if (!ts) return "–";
  const d = new Date(ts * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString() : d.toLocaleString();
}

// "find_alerts" -> "Find alerts": step names read as words outside the graph.
function humanize(name) {
  const words = String(name).replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function fmtAgo(ts) {
  if (!ts) return "–";
  const sec = Math.max(0, nowServer() - ts);
  if (sec < 45) return "just now";
  if (sec < 3600) return `${Math.round(sec / 60)} min ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function pill(state, text) {
  return h("span", { class: `pill st-${state}` }, text || state.toLowerCase());
}

function firstLine(text) {
  if (!text) return "";
  const lines = text.trim().split("\n");
  return lines[lines.length - 1];
}

async function getJSON(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  return res.json();
}

// ------------------------------------------------------------------- theme
const THEMES = ["auto", "light", "dark", "brutalist", "glass", "terminal"];
const THEME_NAMES = {
  auto: ["Auto", "Light or dark, following your system"],
  light: ["Light", "Neutral greys"],
  dark: ["Dark", "Neutral greys, dark"],
  brutalist: ["Brutalist", "Black rules, hard shadows, loud colour"],
  glass: ["Glass", "Frosted panels over a colour field"],
  terminal: ["Terminal", "Green phosphor on black"],
};
const THEME_ICONS = {
  auto: '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 2.5a5.5 5.5 0 0 1 0 11z" fill="currentColor"/></svg>',
  light: '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="3" fill="currentColor"/><path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  dark: '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M13.5 9.6A5.5 5.5 0 0 1 6.4 2.5a5.5 5.5 0 1 0 7.1 7.1z" fill="currentColor"/></svg>',
  brutalist: '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="2" width="9" height="9" fill="currentColor"/><rect x="5" y="5" width="9" height="9" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>',
  glass: '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><circle cx="6" cy="6" r="4" fill="currentColor" opacity=".45"/><rect x="5" y="5" width="9" height="9" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>',
  terminal: '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4.5l3.5 3.5L3 11.5M8.5 11.5H13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};
const PLAY_ICON = '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 3.2v9.6a.5.5 0 0 0 .76.43l7.7-4.8a.5.5 0 0 0 0-.86l-7.7-4.8a.5.5 0 0 0-.76.43z" fill="currentColor"/></svg>';
function applyTheme(t) {
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  $("theme").title = `Theme: ${THEME_NAMES[t][0]}`;
  $("theme").innerHTML = THEME_ICONS[t];
  for (const item of $("theme-menu").children) item.setAttribute("aria-checked", String(item.dataset.value === t));
}
function initTheme() {
  let t = "auto";
  try { t = localStorage.getItem("miniflow-theme") || "auto"; } catch { /* storage blocked */ }
  if (!THEMES.includes(t)) t = "auto";
  const menu = $("theme-menu"), button = $("theme");
  const setOpen = (open) => { menu.hidden = !open; button.setAttribute("aria-expanded", String(open)); };
  menu.replaceChildren(...THEMES.map((name) => {
    const item = h("button", {
      type: "button", role: "menuitemradio", class: "theme-item", "data-value": name,
      onclick: () => {
        t = name;
        try { localStorage.setItem("miniflow-theme", t); } catch { /* storage blocked */ }
        applyTheme(t);
        setOpen(false);
        button.focus();
      },
    }, h("span", { class: "theme-ico" }), h("span", { class: "theme-text" },
      h("strong", {}, THEME_NAMES[name][0]), h("small", {}, THEME_NAMES[name][1])));
    item.querySelector(".theme-ico").innerHTML = THEME_ICONS[name];
    return item;
  }));
  applyTheme(t);
  button.addEventListener("click", () => setOpen(menu.hidden));
  document.addEventListener("pointerdown", (e) => { if (!menu.hidden && !e.target.closest(".theme-picker")) setOpen(false); });
  menu.addEventListener("keydown", (e) => {
    const items = [...menu.children], i = items.indexOf(document.activeElement);
    if (e.key === "Escape") { setOpen(false); button.focus(); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus();
    }
  });
}

// -------------------------------------------------------------------- runs
// Responses can arrive out of order; only the newest request's answer is used.
let runsRequest = 0, controlRequest = 0;

async function loadRuns() {
  const n = ++runsRequest;
  let runs;
  try {
    runs = await getJSON("/api/runs");
  } catch (e) {
    console.warn(e);
    return;
  }
  if (n !== runsRequest) return;
  S.runs = runs;
  renderRuns();
  const latest = S.runs[0];
  renderEmpty();
  if (!latest) return;
  if (!S.runId || (S.follow && latest.run_id !== S.runId)) {
    await selectRun(latest.run_id);
  } else if (S.run && !S.run.finished_at && !S.es && !S.pollTimer) {
    openStream(S.runId, sinceOf());
  }
}

function renderRuns() {
  const list = $("runs");
  list.replaceChildren(...S.runs.slice(0, 30).map((r) => {
    const dur = r.finished_at ? r.finished_at - r.started_at : nowServer() - r.started_at;
    const state = r.finished_at ? r.status : "RUNNING";
    return h("li", {},
      h("button", {
        type: "button",
        class: `run-item${r.run_id === S.runId ? " current" : ""}`,
        "aria-current": r.run_id === S.runId ? "true" : null,
        title: `${r.flow} · run ${r.run_id} · ${r.counts.SUCCESS} of ${r.total} steps done`,
        onclick: () => { setFollow(false); selectRun(r.run_id); if (typeof studioGo === "function") studioGo("runs"); },
      },
        h("span", { class: `dot d-${state}` }),
        h("span", { class: "run-name" }, humanize(r.flow)),
        h("span", { class: "run-when" }, fmtAgo(r.started_at)),
        h("span", { class: "run-line2" },
          h("code", {}, r.run_id.slice(0, 7)),
          r.finished_at ? ` · ${fmtDur(dur)}` : ` · running ${fmtDur(dur)}`,
          r.counts.FAILED ? h("span", { class: "run-bad" }, ` · ${r.counts.FAILED} failed`) : null)));
  }));
}

function renderEmpty() {
  $("empty").hidden = S.runs.length > 0;
  $("dashboard").hidden = S.runs.length === 0;
  $("empty-cli").hidden = !!S.control;
  $("empty-control").hidden = !S.control;
}

function setFollow(on) {
  S.follow = on;
  $("follow").checked = on;
}

// ----------------------------------------------------------------- control
// Only when the dashboard was started with --flows; otherwise /api/control is a 404.
async function loadControl() {
  const n = ++controlRequest;
  let res, data = null;
  try {
    res = await fetch("/api/control", { cache: "no-store" });
    if (res.ok) data = await res.json();
  } catch (e) {
    console.warn(e);
    return;
  }
  if (n !== controlRequest || !(res.ok || res.status === 404)) return;
  S.control = data;
  renderControl();
  const active = S.control && S.control.active;
  if (active && active.run_id && S.follow && active.run_id !== S.runId) loadRuns();
}

const failRate = () => Number($("ctl-rate").value) / 100;

async function control(action, body) {
  S.controlBusy = true;
  S.controlError = null;
  renderControl();
  let started = false;
  try {
    const res = await fetch(`/api/control/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.ok) {
      const result = await res.json();
      started = action !== "cancel" && !result.queued;
      if (started) { setFollow(true); S.newRunOpen = false; }
    } else {
      const data = await res.json().catch(() => ({}));
      S.controlError = data.error || `${action} failed (${res.status})`;
    }
  } catch (e) {
    S.controlError = String(e);
  } finally {
    S.controlBusy = false;
  }
  await loadControl();
  // Switch to the new run as soon as its process has created it, not at the next poll.
  for (let i = 0; started && i < 25 && S.control?.active && !S.control.active.run_id; i++) {
    await new Promise((resolve) => setTimeout(resolve, 200));
    await loadControl();
  }
  loadRuns();
}

function renderControl() {
  const c = S.control;
  $("new-run").hidden = !c;
  if (S.controlError) S.newRunOpen = true;  // keep the reason on screen
  $("control").hidden = !c || !S.newRunOpen;
  $("new-run").setAttribute("aria-expanded", String(!!(c && S.newRunOpen)));
  renderEmpty();
  renderResume();
  if (!c) return;
  const select = $("ctl-flow");
  if ([...select.options].map((o) => o.value).join() !== c.flows.join()) {
    const current = select.value;
    select.replaceChildren(...c.flows.map((f) => h("option", { value: f }, f)));
    if (c.flows.includes(current)) select.value = current;
  }
  const busy = S.controlBusy;
  select.disabled = busy;
  $("ctl-run").disabled = busy || c.flows.length === 0;
  $("ctl-active").hidden = !c.active && !c.queued?.length;
  if (c.active || c.queued?.length) {
    const running = c.running || (c.active ? [c.active] : []);
    $("ctl-active").replaceChildren(
      running.length ? `Running ${running.map((item) => item.flow).join(", ")}` : "",
      c.queued?.length ? ` · ${c.queued.length} waiting` : "");
  }
  $("ctl-cancel").hidden = !c.active;
  $("ctl-cancel").disabled = S.controlBusy;
  if (c.active) $("ctl-cancel").title = "Cancel the selected active run, or the oldest active run";
  const error = S.controlError || c.error;
  $("ctl-error").hidden = !error;
  $("ctl-error").textContent = error || "";
}

// Shown for the run on screen when it failed or was interrupted and its flow is in the folder.
function renderResume() {
  const c = S.control, r = S.run;
  const show = !!(c && r && !c.active && !S.controlBusy && r.finished_at
    && (r.status === "FAILED" || r.status === "INTERRUPTED") && c.flows.includes(r.flow));
  $("ctl-resume").hidden = !show;
  if (show) $("ctl-resume").title = "Run the steps that didn't finish again, keeping the results that did";
}

// ---------------------------------------------------------------- one run
function closeStream() {
  if (S.es) { S.es.close(); S.es = null; }
  if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
}

function sinceOf() {
  let m = 0;
  for (const t of S.tasks.values()) m = Math.max(m, t.updated_at);
  return m;
}

async function selectRun(runId) {
  closeStream();
  let d;
  try {
    d = await getJSON(`/api/runs/${encodeURIComponent(runId)}`);
  } catch (e) {
    console.warn(e);
    return;
  }
  const changedRun = runId !== S.runId;
  S.runId = runId;
  S.run = d.run;
  S.graph = d.graph;
  S.layout = d.layout;
  S.tasks = new Map(d.tasks.map((t) => [t.task, t]));
  S.attempts = d.attempts;
  S.skew = d.now - Date.now() / 1000;
  if (changedRun) S.selected = null;
  // While the editor is open the address stays #editor, so a reload comes back to it.
  try { if (!(typeof Studio === "object" && Studio.visible)) history.replaceState(null, "", `#run=${runId}`); } catch { /* sandboxed */ }
  buildGraph();
  renderAll();
  renderRuns();
  if (!d.run.finished_at) openStream(runId, d.since);
}

function openStream(runId, since) {
  closeStream();
  if (!("EventSource" in window)) return startPolling(runId);
  let failures = 0;
  const es = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events?since=${since}`);
  S.es = es;
  es.addEventListener("tasks", (e) => {
    const p = JSON.parse(e.data);
    for (const t of p.tasks) S.tasks.set(t.task, t);
    failures = 0;
    updateGraph();
    renderOutcome();
    renderProblems();
    renderSummary();
    renderDetails();
    renderTimeline();
  });
  es.addEventListener("attempts", (e) => {
    S.attempts = JSON.parse(e.data);
    renderTimeline();
    renderDetails();
    renderSummary();
  });
  es.addEventListener("run", (e) => {
    const r = JSON.parse(e.data);
    if (r) S.run = r;
    renderMeta();
    renderSummary();
  });
  es.addEventListener("done", () => {
    closeStream();
    renderAll();
    loadRuns();
  });
  es.onerror = () => {
    // EventSource reconnects by itself; fall back to polling if it keeps failing.
    if (++failures >= 3) startPolling(runId);
  };
}

function startPolling(runId) {
  closeStream();
  S.pollTimer = setInterval(async () => {
    if (S.runId !== runId) return closeStream();
    try {
      const d = await getJSON(`/api/runs/${encodeURIComponent(runId)}`);
      S.run = d.run;
      S.tasks = new Map(d.tasks.map((t) => [t.task, t]));
      S.attempts = d.attempts;
      updateGraph();
      renderAll();
      if (d.run.finished_at) { closeStream(); loadRuns(); }
    } catch (e) { console.warn(e); }
  }, 1000);
}

// ------------------------------------------------------------------ render
function renderAll() {
  renderStory();
  renderOutcome();
  renderProblems();
  renderMeta();
  renderSummary();
  updateGraph();
  renderTimeline();
  renderDetails();
}

function runState() {
  if (!S.run) return "PENDING";
  if (!S.run.finished_at) return "RUNNING";
  return S.run.status;
}

function renderMeta() {
  const r = S.run;
  if (!r) { $("run-meta").replaceChildren(); return; }
  const dur = (r.finished_at || nowServer()) - r.started_at;
  const { title, summary } = flowTitle(r.flow);
  $("crumb-run").textContent = `${title} · ${r.run_id.slice(0, 7)}`;
  $("run-meta").replaceChildren(
    h("div", { class: "run-title" },
      h("h1", {}, title),
      h("span", { class: `status-badge b-${runState()}` }, h("span", { class: `dot d-${runState()}` }),
        r.finished_at ? STATUS_WORDS[r.status] || r.status.toLowerCase() : "Running")),
    ...(summary ? [h("p", { class: "run-summary" }, summary)] : []),
  );
  renderResume();
}

const STATUS_WORDS = { SUCCESS: "Succeeded", FAILED: "Failed", INTERRUPTED: "Interrupted", RUNNING: "Running" };

// Examples carry a short title ("Live earthquake monitor"); other flows only a
// docstring sentence, which reads better as the summary under the flow's name.
function flowTitle(flow) {
  const about = (S.graph && S.graph.about) || {};
  const short = about.title && about.title.length <= 48 && !/[.:]$/.test(about.title);
  return { title: short ? about.title : humanize(flow), summary: short ? null : about.title || null };
}

function counts() {
  const c = Object.fromEntries(STATES.map((st) => [st, 0]));
  for (const t of S.tasks.values()) c[t.state] += 1;
  return c;
}

function prop(label, ...value) {
  return h("div", { class: "prop" }, h("dt", {}, label), h("dd", {}, ...value));
}

function renderSummary() {
  if (!S.run) return;
  const c = counts();
  const total = S.tasks.size;
  const done = c.SUCCESS + c.FAILED + c.SKIPPED;

  const progress = $("progress");
  progress.replaceChildren();
  for (const st of ["SUCCESS", "FAILED", "SKIPPED", "RETRYING", "RUNNING", "READY"]) {
    if (!c[st] || !total) continue;
    progress.append(h("span", { style: `width:${(100 * c[st]) / total}%;background:var(--${st.toLowerCase()}-line)`, title: `${c[st]} ${st.toLowerCase()}` }));
  }
  progress.setAttribute("aria-label", `${done} of ${total} tasks finished`);

  const wall = (S.run.finished_at || nowServer()) - S.run.started_at;
  const executed = new Set(S.attempts.map((a) => a.task)).size;
  const retries = S.attempts.length - executed;
  const reused = [...S.tasks.values()].filter((t) => t.from_cache).length;
  const extra = [c.FAILED && h("span", { class: "bad" }, `${c.FAILED} failed`), c.SKIPPED && `${c.SKIPPED} skipped`,
    c.RUNNING && `${c.RUNNING} running`, reused && `${reused} reused`].filter(Boolean);
  const started = new Date(S.run.started_at * 1000);
  $("stats").replaceChildren(
    prop("Started", h("span", { title: started.toLocaleString() }, started.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }))),
    prop("Duration", S.run.finished_at ? fmtDur(wall) : `${fmtDur(wall)} so far`),
    prop("Steps", `${c.SUCCESS} of ${total} done`, ...extra.flatMap((x) => [h("span", { class: "sep" }, " · "), x])),
    prop("Retries", String(retries)),
    prop("Run ID", h("code", { title: S.run.run_id }, S.run.run_id.slice(0, 12))),
  );
}

// The List view: every step in dependency order, one row each.
function renderStepList() {
  const body = $("step-rows");
  const names = Object.keys(S.graph.tasks).sort((a, b) =>
    (S.layout[a].col - S.layout[b].col) || (S.layout[a].row - S.layout[b].row));
  body.replaceChildren(...names.map((name) => {
    const t = S.tasks.get(name) || { state: "PENDING", attempts: 0 };
    const took = t.started_at && t.finished_at ? fmtDur(t.finished_at - t.started_at) : "";
    return h("tr", {
      class: `s-${t.state}${S.selected === name ? " selected" : ""}`, tabindex: "0",
      onclick: () => select(name),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(name); } },
    },
      h("td", { title: name }, h("span", { class: `dot d-${t.state}` }), h("span", { class: "step-name" }, humanize(name))),
      h("td", { class: "state" }, STATE_WORDS[t.state] || t.state.toLowerCase(), t.from_cache ? " · reused" : ""),
      h("td", { class: "num" }, t.attempts > 1 ? String(t.attempts) : t.attempts ? "1" : ""),
      h("td", { class: "num" }, took),
      h("td", { class: "track-col" }, stepTrack(t)));
  }));
}

// A tiny Gantt bar per row: where the step sat within the run.
function stepTrack(t) {
  const track = h("span", { class: "track" });
  if (!S.run || !t.started_at) return track;
  const t0 = S.run.started_at, t1 = Math.max(S.run.finished_at || nowServer(), t.finished_at || 0);
  const span = Math.max(t1 - t0, 0.001);
  const end = t.finished_at || (t.state === "RUNNING" ? nowServer() : t.started_at);
  const left = ((t.started_at - t0) / span) * 100, width = Math.max(((end - t.started_at) / span) * 100, 0.8);
  track.append(h("span", { class: `track-bar d-${t.state}`, style: `left:${left.toFixed(2)}%;width:${width.toFixed(2)}%` }));
  return track;
}

// List | Graph | Timeline: one pane at a time, remembered per browser.
function showPane(pane) {
  for (const name of ["list", "graph", "timeline"]) {
    $(`pane-${name}`).hidden = name !== pane;
    $(`pane-tab-${name}`).setAttribute("aria-selected", String(name === pane));
  }
  $("legend").hidden = pane !== "graph";
  $("timeline-hint").hidden = pane !== "timeline";
  try { localStorage.setItem("miniflow-pane", pane); } catch { /* storage blocked */ }
  if (pane === "timeline") renderTimeline();
  if (pane === "graph" && !G.fitted) fitGraph(true);
}

function renderLegend() {
  $("legend").replaceChildren(
    ...["RUNNING", "RETRYING", "SUCCESS", "FAILED", "SKIPPED"].map((st) =>
      h("span", {}, h("span", { class: "sw", style: `background:var(--${st.toLowerCase()}-line)` }), STATE_WORDS[st])));
}

// ----------------------------------------------------------------- details
function select(name) {
  S.selected = name;
  updateGraph();
  renderTimeline();
  renderDetails();
}

function renderDetails() {
  const body = $("details-body");
  const name = S.selected;
  const open = !!(name && S.graph && name in S.graph.tasks);
  document.querySelector(".workbench").classList.toggle("has-details", open);
  if (!open) {
    body.replaceChildren();
    return;
  }
  const info = S.graph.tasks[name];
  const t = S.tasks.get(name) || { state: "PENDING", attempts: 0 };
  const attempts = S.attempts.filter((a) => a.task === name).sort((a, b) => a.attempt - b.attempt);
  const downstream = Object.entries(S.graph.tasks).filter(([, i]) => i.after.includes(name)).map(([n]) => n);
  const took = t.started_at && t.finished_at ? t.finished_at - t.started_at : null;

  const link = (n) => h("button", { type: "button", class: "link", onclick: () => select(n) }, n);
  const facts = [
    took !== null && `took ${fmtDur(took)}`,
    t.attempts > 1 && `${t.attempts} tries`,
    t.item_count != null && `${t.item_count.toLocaleString()} items`,
    info.optional && "optional",
    t.from_cache && "reused from last run",
  ].filter(Boolean).join(" · ");
  const parts = [
    h("h3", {}, humanize(name)),
    h("div", { class: "detail-sub" }, pill(t.state), facts ? h("span", { class: "muted" }, facts) : null),
    info.description ? h("p", { class: "desc" }, info.description) : null,
    t.preview ? [h("div", { class: "section-title" }, t.from_cache ? "Result (reused from the previous run)" : "Result"), h("pre", { class: "preview box" }, t.preview)] : null,
    t.sample !== null && t.sample !== undefined && typeof t.sample === "object" ? [
      h("div", { class: "section-title" }, "Sample output"), renderSample(t.sample, t.item_count),
    ] : null,
    t.error && t.state !== "SUCCESS" ? [
      h("div", { class: "section-title" }, t.state === "SKIPPED" ? "Why it was skipped" : "What went wrong"),
      h("pre", { class: "error" }, firstLine(t.error)),
      t.error.includes("\n") ? h("details", { class: "tech" }, h("summary", {}, "Technical details (Python traceback)"), h("pre", { class: "error" }, t.error)) : null,
    ] : null,
    t.state === "FAILED" && info.optional ? h("p", { class: "desc" }, "This step is optional, so the job carried on without it.") : null,
    h("details", { class: "tech more" }, h("summary", {}, "More details"),
      h("dl", { class: "kv" },
        h("dt", {}, "Step id"), h("dd", {}, h("code", {}, name)),
        h("dt", {}, "Runs on"), h("dd", {}, `${info.kind} worker`),
        h("dt", {}, "Started"), h("dd", {}, fmtClock(t.started_at)),
        h("dt", {}, "Saved"), h("dd", {}, t.from_cache ? "reused from the previous run" : t.has_result ? "yes, kept for resume" : t.state === "SUCCESS" ? "no (reruns on resume)" : "–"),
        h("dt", {}, "Needs"), h("dd", {}, info.after.length ? h("div", { class: "links" }, info.after.map(link)) : "nothing (first step)"),
        h("dt", {}, "Feeds"), h("dd", {}, downstream.length ? h("div", { class: "links" }, downstream.map(link)) : "nothing (last step)"),
      ),
      attempts.map((a) => h("div", { class: "attempt" },
        h("div", { class: "attempt-head" },
          h("span", {}, `Try ${a.attempt} `, pill(a.state)),
          h("span", { class: "muted" }, `${a.worker || "timed out"} · ${fmtDur(a.finished_at - a.started_at)}`)),
        a.error ? h("pre", { class: "error" }, firstLine(a.error)) : null))),
  ];
  body.replaceChildren(...parts.flat(Infinity).filter(Boolean));
}

// Pretty JSON that keeps short arrays and objects on one line, so a list of
// pairs reads as ["Thu 01 Oct", 5] rather than four lines each.
function flatJSON(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(flatJSON).join(", ")}]`;
  return `{${Object.entries(value).map(([k, v]) => `${JSON.stringify(k)}: ${flatJSON(v)}`).join(", ")}}`;
}

function compactJSON(value, indent = "") {
  const flat = flatJSON(value);
  if (value === null || typeof value !== "object" || flat.length + indent.length <= 60) return flat;
  const inner = `${indent}  `;
  const items = Array.isArray(value)
    ? value.map((v) => inner + compactJSON(v, inner))
    : Object.entries(value).map(([k, v]) => `${inner}${JSON.stringify(k)}: ${compactJSON(v, inner)}`);
  const [open, close] = Array.isArray(value) ? ["[", "]"] : ["{", "}"];
  return `${open}\n${items.join(",\n")}\n${indent}${close}`;
}

function renderSample(sample, count) {
  if (Array.isArray(sample) && sample.length && sample[0] && typeof sample[0] === "object" && !Array.isArray(sample[0])) {
    const rows = sample.filter((row) => row && typeof row === "object" && !Array.isArray(row));
    const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))];
    const table = h("table", { class: "sample-table" },
      h("thead", {}, h("tr", {}, columns.map((key) => h("th", {}, key)))),
      h("tbody", {}, rows.map((row) => h("tr", {}, columns.map((key) =>
        h("td", {}, typeof row[key] === "string" ? row[key] : JSON.stringify(row[key] ?? null)))))));
    return h("div", { class: "sample-scroll" }, table,
      count != null ? h("p", { class: "hint" }, `Showing ${rows.length} of ${count} items`) : null);
  }
  return h("pre", { class: "preview box json" }, compactJSON(sample));
}

function renderProblems() {
  const box = $("run-problems");
  const failed = [...S.tasks.values()].filter((task) => task.state === "FAILED");
  const skipped = [...S.tasks.values()].filter((task) => task.state === "SKIPPED").length;
  box.hidden = failed.length === 0;
  if (box.hidden) return;
  $("run-problems-title").textContent = failed.length === 1
    ? `${humanize(failed[0].task)} failed`
    : `${failed.length} steps failed`;
  $("run-problems-body").replaceChildren(
    ...failed.map((task) => h("p", { class: "alert-line" },
      failed.length > 1 ? h("strong", {}, `${humanize(task.task)}: `) : null,
      h("code", {}, firstLine(task.error) || "This step failed."),
      h("button", { type: "button", class: "alert-link", onclick: () => select(task.task) }, "View step"))),
    skipped ? h("p", { class: "alert-note" },
      `${skipped} later step${skipped === 1 ? " was" : "s were"} skipped. Fix the cause and press Resume to finish the run without redoing the steps that succeeded.`) : null);
}

async function loadSchedules() {
  try {
    const response = await fetch("/api/schedules", { cache: "no-store" });
    if (!response.ok) return;
    const data = await response.json();
    const panel = $("schedules-panel");
    panel.hidden = !data.schedules.length && !data.events.length && !data.queued?.length && !Object.keys(data.problems).length;
    if (panel.hidden) return;
    const scheduleLabel = (item) => item.cron ? `Cron ${item.cron}`
      : item.daily ? `Daily ${item.daily}`
      : item.interval_seconds != null ? `Every ${item.interval_seconds}s`
      : item.watch ? `Watch ${item.watch} · check every ${item.poll_seconds}s`
      : item.configured ? `Webhook ${item.url}` : `Webhook ${item.url} · set ${item.webhook}`;
    $("schedules-body").replaceChildren(...data.schedules.map((item) =>
      h("div", { class: "schedule-row" },
        h("strong", {}, `${item.flow} · ${item.name}`),
        h("span", {}, scheduleLabel(item)),
        item.next_run != null ? h("span", {}, `Next: ${new Date(item.next_run * 1000).toLocaleString()}`) : null)));
    const queued = (data.queued || []).map((item, index) =>
      h("div", { class: "schedule-row" }, pill("QUEUED"),
        h("span", {}, `${index + 1}. ${item.flow} · ${item.source}`)));
    const events = data.events.slice(0, 8).map((event) =>
      h("div", { class: "schedule-row" },
        pill(event.status),
        h("span", {}, `${event.flow} · ${event.schedule} · ${new Date(event.due_at * 1000).toLocaleString()}`),
        event.message ? h("span", { class: "muted" }, event.message) : null));
    const problems = Object.entries(data.problems).map(([flow, message]) =>
      h("p", { class: "error" }, `${flow}: ${message}`));
    $("schedule-events").replaceChildren(...queued, ...events, ...problems);
  } catch { /* dashboard is still usable if a schedule read fails */ }
}

// ---------------------------------------------------------------- examples
const EX = { list: [], allowRun: false, offline: false, running: null, pending: null };

async function loadExamples() {
  let d;
  try { d = await getJSON("/api/examples"); } catch (e) { console.warn(e); return; }
  Object.assign(EX, { list: d.examples, allowRun: d.allowRun, offline: d.offline, running: d.running });
  if (!d.running) EX.pending = null;
  renderGallery();
}

function renderGallery() {
  const busy = EX.running || EX.pending;
  // Rebuild the cards only when something changed: replacing a button under the
  // pointer would swallow the click.
  const signature = JSON.stringify([EX.list.map((e) => e.name), EX.allowRun, EX.offline, busy]);
  if (signature === EX.signature) return;
  EX.signature = signature;
  $("gallery-hint").textContent = EX.allowRun
    ? `Real jobs on ${EX.offline ? "saved real-world data" : "live public data"}. Pick one and watch it run.`
    : "Run one from a terminal with the command shown on each row.";
  $("example-cards").replaceChildren(...EX.list.map((ex) => {
    const k = ex.kinds;
    const isRunning = busy === ex.name;
    const foot = EX.allowRun
      ? h("button", {
          type: "button", class: "run-btn", disabled: busy ? true : null,
          title: busy && !isRunning ? `Wait for ${busy} to finish` : `Start the ${ex.title.toLowerCase()} now`,
          onclick: () => runExample(ex.name),
        }, "Running…")
      : h("code", {}, `miniflow example ${ex.name} --ui`);
    if (EX.allowRun && !isRunning) foot.innerHTML = `${PLAY_ICON} Run`;
    return h("article", { class: "example" },
      h("div", { class: "example-text" },
        h("h3", {}, ex.title),
        h("p", { title: ex.description }, ex.description)),
      h("span", { class: "example-steps", title: `${k.io} io · ${k.cpu} cpu · ${k.async} async` }, `${ex.tasks} steps`),
      foot);
  }));
}

async function runExample(name) {
  EX.pending = name;
  renderGallery();
  try {
    const res = await fetch(`/api/examples/${encodeURIComponent(name)}/run`, {
      method: "POST", headers: { "X-MiniFlow-Action": "run" },
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.error || `Could not start ${name} (${res.status})`);
      EX.pending = null;
      renderGallery();
      return;
    }
  } catch (e) {
    EX.pending = null;
    renderGallery();
    return;
  }
  setFollow(true);
  if (typeof studioNav === "function") studioNav("runs");
  setTimeout(loadRuns, 400);
  setTimeout(loadExamples, 400);
}

// ------------------------------------------------------- story & outcome
function renderStory() {
  const about = (S.graph && S.graph.about) || {};
  const box = $("story");
  const usage = /(^|\n)\s*(miniflow |uv run |python |\$ )|\s#\s/.test(about.description || "");
  box.hidden = !about.description || usage;
  if (box.hidden) return;
  $("story-text").textContent = about.description;
  box.title = box.classList.contains("open") ? "" : "Click to read more";
  $("story-watch").hidden = !about.watch;
  $("story-watch").querySelector("span").textContent = about.watch || "";
}

function renderOutcome() {
  const body = $("outcome-body");
  if (!S.graph) { body.replaceChildren(); return; }
  const names = Object.keys(S.graph.tasks);
  const used = new Set(names.flatMap((n) => S.graph.tasks[n].after));
  // The flow's chosen highlight steps, or else its final steps.
  const chosen = ((S.graph.about || {}).highlights || []).filter((n) => n in S.graph.tasks);
  const shown = (chosen.length ? chosen : names.filter((n) => !used.has(n)))
    .filter((n) => !["FAILED", "SKIPPED"].includes((S.tasks.get(n) || {}).state));
  $("outcome").hidden = shown.length === 0;
  const items = shown.map((n) => {
    const t = S.tasks.get(n) || { state: "PENDING" };
    const info = S.graph.tasks[n];
    let text;
    if (t.state === "SUCCESS") text = t.preview || "Done.";
    else text = "Not finished yet: the result appears here when this step is done.";
    return h("div", { class: "outcome-item" },
      h("div", { class: "head" },
        h("button", { type: "button", class: "outcome-name", title: info.description || null, onclick: () => select(n) }, humanize(n)),
        t.state === "SUCCESS" ? null : pill(t.state)),
      h("pre", { class: "preview" }, text));
  });
  body.replaceChildren(h("div", { class: "outcome-items" }, items));
}
// -------------------------------------------------------------------- init
function init() {
  initTheme();
  renderLegend();
  $("follow").addEventListener("change", (e) => {
    S.follow = e.target.checked;
    if (S.follow) loadRuns();
  });
  $("ctl-rate").addEventListener("input", () => {
    $("ctl-rate-value").textContent = `${$("ctl-rate").value}%`;
    renderResume();  // its tooltip names the rate
  });
  $("ctl-run").addEventListener("click", () => control("run", { flow: $("ctl-flow").value, fail_rate: failRate() }));
  $("ctl-resume").addEventListener("click", () => control("resume", { run_id: S.runId, fail_rate: failRate() }));
  $("ctl-cancel").addEventListener("click", () => {
    const selected = (S.control?.running || []).find((item) => item.run_id === S.runId);
    control("cancel", selected ? { run_id: selected.run_id } : {});
  });
  // Check the controls every second while a run is active, otherwise every 3 s.
  let ticks = 0;
  loadControl().then(() => {
    if (!S.control) return;  // read-only dashboard
    setInterval(() => {
      ticks += 1;
      if (S.control && (S.control.active || ticks % 3 === 0)) loadControl();
    }, 1000);
  });
  loadExamples();
  setInterval(loadExamples, 3000);
  loadSchedules();
  setInterval(loadSchedules, 3000);
  const m = /run=([\w-]+)/.exec(location.hash);
  if (m) {
    setFollow(false);
    selectRun(m[1]).then(loadRuns);
  } else {
    loadRuns();
  }
  setInterval(loadRuns, 3000);
  // Keep clocks and running bars moving while a run is live.
  setInterval(() => {
    if (S.run && !S.run.finished_at) { renderMeta(); renderSummary(); renderTimeline(); }
  }, 500);
  $("story").addEventListener("click", () => { $("story").classList.toggle("open"); renderStory(); });
  $("new-run").addEventListener("click", () => { S.newRunOpen = !S.newRunOpen; S.controlError = null; renderControl(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && S.newRunOpen) { S.newRunOpen = false; renderControl(); } });
  document.addEventListener("pointerdown", (e) => {
    if (S.newRunOpen && !e.target.closest(".newrun")) { S.newRunOpen = false; renderControl(); }
  });
  $("details-close").addEventListener("click", () => select(null));
  initGraphView();
  initGantt();
  for (const tab of document.querySelectorAll(".seg [data-pane]")) {
    tab.addEventListener("click", () => showPane(tab.dataset.pane));
  }
  let pane = "list";
  try { pane = localStorage.getItem("miniflow-pane") || "list"; } catch { /* storage blocked */ }
  showPane(["list", "graph", "timeline"].includes(pane) ? pane : "list");
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderTimeline, 150);
  });
}

init();
