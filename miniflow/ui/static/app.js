/* MiniFlow dashboard: vanilla JS + SVG, no dependencies.
 *
 * Data flow: GET /api/runs (every 3 s) for the runs table; GET /api/runs/<id>
 * for one run; then an EventSource on /api/runs/<id>/events pushes task,
 * attempt and run changes until the run ends.
 */
"use strict";

const STATES = ["PENDING", "READY", "RUNNING", "RETRYING", "SUCCESS", "FAILED", "SKIPPED"];
const TERMINAL = new Set(["SUCCESS", "FAILED", "SKIPPED"]);
const NODE_W = 172, NODE_H = 48, COL_GAP = 64, ROW_GAP = 20, PAD = 16;
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
const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  $("theme").title = `Theme: ${t}`;
  $("theme").textContent = t === "auto" ? "◐" : t === "light" ? "☀" : "☾";
}
function initTheme() {
  let t = "auto";
  try { t = localStorage.getItem("miniflow-theme") || "auto"; } catch { /* storage blocked */ }
  applyTheme(t);
  $("theme").addEventListener("click", () => {
    t = THEMES[(THEMES.indexOf(t) + 1) % THEMES.length];
    try { localStorage.setItem("miniflow-theme", t); } catch { /* storage blocked */ }
    applyTheme(t);
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

function stackedBar(counts, total, cls) {
  const bar = h("span", { class: cls });
  for (const st of ["SUCCESS", "FAILED", "SKIPPED", "RETRYING", "RUNNING", "READY"]) {
    const n = counts[st] || 0;
    if (!n || !total) continue;
    bar.append(h("span", { style: `width:${(100 * n) / total}%;background:var(--${st.toLowerCase()}-line)`, title: `${n} ${st.toLowerCase()}` }));
  }
  return bar;
}

function renderRuns() {
  const body = $("runs");
  body.replaceChildren(...S.runs.map((r) => {
    const dur = r.finished_at ? r.finished_at - r.started_at : nowServer() - r.started_at;
    const c = r.counts;
    const summary = [`${c.SUCCESS} ok`];
    if (c.FAILED) summary.push(`${c.FAILED} failed`);
    if (c.SKIPPED) summary.push(`${c.SKIPPED} skipped`);
    return h("tr", {
      class: r.run_id === S.runId ? "current" : null,
      tabindex: "0",
      onclick: () => { setFollow(false); selectRun(r.run_id); },
      onkeydown: (e) => { if (e.key === "Enter") { setFollow(false); selectRun(r.run_id); } },
    },
      h("td", { class: "id" }, r.run_id),
      h("td", {}, r.flow),
      h("td", {}, pill(r.status === "RUNNING" ? "RUNNING" : r.status, r.status.toLowerCase())),
      h("td", {}, fmtClock(r.started_at)),
      h("td", {}, r.finished_at ? fmtDur(dur) : `${fmtDur(dur)}…`),
      h("td", {}, stackedBar(c, r.total, "mini"), `${summary.join(" · ")} / ${r.total}`),
    );
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
      started = action !== "cancel";
      if (started) setFollow(true);
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
  $("control").hidden = !c;
  renderEmpty();
  renderResume();
  if (!c) return;
  const select = $("ctl-flow");
  if ([...select.options].map((o) => o.value).join() !== c.flows.join()) {
    const current = select.value;
    select.replaceChildren(...c.flows.map((f) => h("option", { value: f }, f)));
    if (c.flows.includes(current)) select.value = current;
  }
  const busy = S.controlBusy || !!c.active;
  select.disabled = busy;
  $("ctl-run").disabled = busy || c.flows.length === 0;
  $("ctl-active").hidden = !c.active;
  if (c.active) {
    $("ctl-active").replaceChildren(
      c.active.action === "resume" ? "Resuming " : "Running ", h("strong", {}, c.active.flow), "…");
  }
  $("ctl-cancel").hidden = !c.active;
  $("ctl-cancel").disabled = S.controlBusy;
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
  if (show) $("ctl-resume").title = `Rerun the unfinished tasks of ${r.run_id} with a ${$("ctl-rate").value}% fault rate`;
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
  $("run-meta").replaceChildren(
    h("span", {}, "run ", h("strong", {}, r.run_id)),
    h("span", {}, "flow ", h("strong", {}, r.flow)),
    pill(runState(), r.finished_at ? r.status.toLowerCase() : "running"),
    h("span", {}, `started ${fmtClock(r.started_at)}`),
    h("span", {}, r.finished_at ? `took ${fmtDur(dur)}` : `${fmtDur(dur)} so far`),
  );
  renderResume();
}

function counts() {
  const c = Object.fromEntries(STATES.map((st) => [st, 0]));
  for (const t of S.tasks.values()) c[t.state] += 1;
  return c;
}

function stat(label, value, sub, tip) {
  return h("div", { class: "stat", title: tip || null },
    h("div", { class: "label" }, label),
    h("div", { class: "value" }, value),
    sub ? h("div", { class: "sub" }, sub) : null);
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
  let busy = 0;
  for (const a of S.attempts) if (a.started_at && a.finished_at) busy += a.finished_at - a.started_at;
  const executed = new Set(S.attempts.map((a) => a.task)).size;
  const retries = S.attempts.length - executed;
  const reused = [...S.tasks.values()].filter((t) => t.from_cache).length;
  const kinds = { io: 0, cpu: 0, async: 0 };
  for (const info of Object.values(S.graph.tasks)) kinds[info.kind] = (kinds[info.kind] || 0) + 1;

  $("stats").replaceChildren(
    stat("Progress", `${done} / ${total}`, `${c.SUCCESS} succeeded · ${c.FAILED} failed · ${c.SKIPPED} skipped`),
    stat(S.run.finished_at ? "Duration" : "Elapsed", fmtDur(wall),
      S.run.finished_at ? S.run.status.toLowerCase()
        : [c.RUNNING && `${c.RUNNING} running`, c.RETRYING && `${c.RETRYING} waiting to retry`].filter(Boolean).join(" · ") || "starting…"),
    stat("Parallel speedup", wall > 0 && busy > 0 ? `×${(busy / wall).toFixed(2)}` : "–", `${fmtDur(busy)} of task time`,
      "Total time spent inside steps ÷ real time the run took. ×3 means the job did about three steps' worth of work at once. Below ×1 means the steps were so short that scheduling and starting workers took longer than the work itself, or the run spent most of its time waiting before retries."),
    stat("Retries", String(retries), reused ? `${reused} task${reused === 1 ? "" : "s"} reused` : `${S.attempts.length} attempts`),
    stat("Worker kinds", `${kinds.io} · ${kinds.cpu} · ${kinds.async}`, "io · cpu · async"),
  );
}

// ------------------------------------------------------------------- graph
function nodePos(name) {
  const p = S.layout[name];
  const rowsInCol = S._rowsPerCol[p.col];
  const offset = ((S._maxRows - rowsInCol) * (NODE_H + ROW_GAP)) / 2;
  return {
    x: PAD + p.col * (NODE_W + COL_GAP),
    y: PAD + offset + p.row * (NODE_H + ROW_GAP),
  };
}

function truncate(text, max) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function buildGraph() {
  const svg = $("graph");
  svg.replaceChildren();
  S.nodes.clear();
  S.edges = [];
  const names = Object.keys(S.graph.tasks);
  if (!names.length) return;

  const cols = Math.max(...names.map((n) => S.layout[n].col)) + 1;
  S._rowsPerCol = Array(cols).fill(0);
  for (const n of names) S._rowsPerCol[S.layout[n].col] += 1;
  S._maxRows = Math.max(...S._rowsPerCol);
  const width = PAD * 2 + cols * NODE_W + (cols - 1) * COL_GAP;
  const height = PAD * 2 + S._maxRows * NODE_H + (S._maxRows - 1) * ROW_GAP;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);

  svg.append(s("defs", {},
    s("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" },
      s("path", { d: "M0,0 L10,5 L0,10 z", class: "arrow" }))));

  const edgeLayer = s("g", {});
  const nodeLayer = s("g", {});
  svg.append(edgeLayer, nodeLayer);

  for (const [name, info] of Object.entries(S.graph.tasks)) {
    for (const up of info.after) {
      if (!(up in S.graph.tasks)) continue;
      const a = nodePos(up), b = nodePos(name);
      const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x - 2, y2 = b.y + NODE_H / 2;
      const mid = (x1 + x2) / 2;
      const el = s("path", { d: `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`, class: "edge", "marker-end": "url(#arrow)" });
      edgeLayer.append(el);
      S.edges.push({ from: up, to: name, el });
    }
  }

  for (const [name, info] of Object.entries(S.graph.tasks)) {
    const { x, y } = nodePos(name);
    const g = s("g", {
      class: "node", transform: `translate(${x},${y})`, tabindex: "0", role: "button",
      onclick: () => select(name),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(name); } },
    });
    if (info.kind === "cpu") {
      g.append(s("rect", { class: "back", x: 5, y: -5, width: NODE_W, height: NODE_H, rx: 3 }));
    }
    const rx = info.kind === "async" ? NODE_H / 2 : info.kind === "cpu" ? 3 : 8;
    g.append(
      s("rect", { class: "box", width: NODE_W, height: NODE_H, rx }),
      s("text", { class: "name", x: 14, y: 20 }, truncate(name, 20)),
      s("text", { class: "sub", x: 14, y: 37 }, ""),
      s("text", { class: "badge", x: NODE_W - 12, y: 20, "text-anchor": "end" }, ""),
      s("title", {}, info.description ? `${name}\n${info.description}` : name),
    );
    nodeLayer.append(g);
    S.nodes.set(name, g);
  }
  updateGraph();
}

function updateGraph() {
  if (!S.graph) return;
  for (const [name, g] of S.nodes) {
    const t = S.tasks.get(name);
    const info = S.graph.tasks[name];
    const state = t ? t.state : "PENDING";
    g.setAttribute("class", `node n-${state}${info.optional ? " optional" : ""}${S.selected === name ? " selected" : ""}`);
    let sub = `${info.kind} · ${state.toLowerCase()}`;
    if (t && t.attempts > 1) sub += ` · try ${t.attempts}`;
    g.querySelector(".sub").textContent = sub;
    g.querySelector(".badge").textContent = t && t.from_cache ? "↺ reused" : "";
    g.setAttribute("aria-label", `${name}, ${info.kind} task, ${state.toLowerCase()}`);
  }
  for (const e of S.edges) {
    const src = S.tasks.get(e.from), dst = S.tasks.get(e.to);
    let cls = "edge";
    if (src && src.state === "SUCCESS") cls += " done";
    if (dst && dst.state === "SKIPPED") cls += " skipped";
    e.el.setAttribute("class", cls);
  }
}

function renderLegend() {
  const legend = $("legend");
  legend.replaceChildren(
    ...["PENDING", "RUNNING", "RETRYING", "SUCCESS", "FAILED", "SKIPPED"].map((st) =>
      h("span", {}, h("span", { class: "sw", style: `background:var(--${st.toLowerCase()});border-color:var(--${st.toLowerCase()}-line)` }), st.toLowerCase())),
    h("span", { class: "shape" }, "▭ io"),
    h("span", { class: "shape" }, "⧉ cpu"),
    h("span", { class: "shape" }, "⬭ async"),
  );
}

// ---------------------------------------------------------------- timeline
function laneName(a) {
  return a.worker || "timed out";
}

function laneOrder(name) {
  if (name.startsWith("thread-")) return [0, name];
  if (name.startsWith("pid-")) return [1, name];
  if (name === "loop") return [2, name];
  if (name === "running") return [4, name];
  return [3, name];
}

function niceStep(span, target) {
  const raw = span / target;
  const pow = 10 ** Math.floor(Math.log10(raw));
  for (const m of [1, 2, 5, 10]) if (raw <= m * pow) return m * pow;
  return 10 * pow;
}

function renderTimeline() {
  const svg = $("timeline");
  svg.replaceChildren();
  if (!S.run) return;
  const bars = S.attempts
    .filter((a) => a.started_at && a.finished_at)
    .map((a) => ({ task: a.task, attempt: a.attempt, state: a.state, start: a.started_at, end: a.finished_at, lane: laneName(a), error: a.error }));
  const now = nowServer();
  if (!S.run.finished_at) {
    for (const t of S.tasks.values()) {
      if (t.state === "RUNNING") {
        bars.push({ task: t.task, attempt: t.attempts + 1, state: "RUNNING", start: t.updated_at, end: now, lane: "running" });
      }
    }
  }
  if (!bars.length) {
    svg.setAttribute("width", 0);
    svg.setAttribute("height", 0);
    const parent = svg.parentElement;
    parent.querySelector(".timeline-empty")?.remove();
    parent.append(h("div", { class: "timeline-empty" }, S.run.finished_at ? "No task was executed in this run (everything was reused)." : "Waiting for the first attempt to start…"));
    return;
  }
  svg.parentElement.querySelector(".timeline-empty")?.remove();

  const lanes = [...new Set(bars.map((b) => b.lane))].sort((a, b) => {
    const [ka, na] = laneOrder(a), [kb, nb] = laneOrder(b);
    return ka - kb || na.localeCompare(nb, undefined, { numeric: true });
  });
  const LABEL_W = 170, LANE_H = 30, BAR_H = 20, AXIS_H = 24;
  const t0 = S.run.started_at;
  const t1 = Math.max(S.run.finished_at || now, ...bars.map((b) => b.end));
  const span = Math.max(t1 - t0, 0.001);
  const avail = Math.max(svg.parentElement.clientWidth - 2, 600);
  const width = Math.max(avail, LABEL_W + 400);
  const plotW = width - LABEL_W - 16;
  const height = AXIS_H + lanes.length * LANE_H + 6;
  const x = (t) => LABEL_W + ((t - t0) / span) * plotW;
  const laneY = (lane) => AXIS_H + lanes.indexOf(lane) * LANE_H;

  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.append(s("defs", {},
    s("pattern", { id: "stripes", width: 8, height: 8, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" },
      s("rect", { width: 8, height: 8, fill: "var(--running)" }),
      s("rect", { width: 3, height: 8, fill: "var(--running-line)", opacity: 0.45 }))));

  lanes.forEach((lane, i) => {
    const y = laneY(lane);
    if (i % 2 === 0) svg.append(s("rect", { class: "lane-bg", x: 0, y, width, height: LANE_H }));
    svg.append(s("text", { class: "lane-label", x: 8, y: y + LANE_H / 2 + 4 }, truncate(lane, 22)));
  });

  const step = niceStep(span, Math.max(2, Math.floor(plotW / 110)));
  for (let t = 0; t <= span + 1e-9; t += step) {
    const xx = x(t0 + t);
    svg.append(
      s("line", { class: "tick", x1: xx, x2: xx, y1: AXIS_H - 4, y2: height }),
      s("text", { class: "tick-label", x: xx, y: 14, "text-anchor": "middle" }, fmtDur(t)));
  }

  // Backoff connectors between consecutive attempts of the same task.
  const byTask = new Map();
  for (const b of bars) {
    if (!byTask.has(b.task)) byTask.set(b.task, []);
    byTask.get(b.task).push(b);
  }
  for (const list of byTask.values()) {
    list.sort((a, b) => a.attempt - b.attempt);
    for (let i = 1; i < list.length; i++) {
      const a = list[i - 1], b = list[i];
      const ya = laneY(a.lane) + LANE_H / 2, yb = laneY(b.lane) + LANE_H / 2;
      const xa = x(a.end), xb = x(b.start);
      svg.append(s("path", { class: "backoff", d: `M${xa},${ya} C${(xa + xb) / 2},${ya} ${(xa + xb) / 2},${yb} ${xb},${yb}` },
        s("title", {}, `${a.task}: backoff ${fmtDur(b.start - a.end)} before attempt ${b.attempt}`)));
    }
  }

  for (const b of bars) {
    const bx = x(b.start), bw = Math.max(x(b.end) - bx, 3);
    const y = laneY(b.lane) + (LANE_H - BAR_H) / 2;
    const tip = `${b.task} · attempt ${b.attempt} · ${b.state.toLowerCase()} · ${fmtDur(b.end - b.start)}` + (b.error ? `\n${firstLine(b.error)}` : "");
    svg.append(s("rect", {
      class: `bar ${b.state}${S.selected === b.task ? " selected" : ""}`, x: bx, y, width: bw, height: BAR_H, rx: 4,
      onclick: () => select(b.task),
    }, s("title", {}, tip)));
    const label = b.attempt > 1 ? `${b.task} #${b.attempt}` : b.task;
    if (bw > label.length * 6.5 + 10) {
      svg.append(s("text", { class: "bar-label", x: bx + 6, y: y + BAR_H / 2 + 4 }, label));
    }
  }
  if (!S.run.finished_at) {
    const xn = x(now);
    svg.append(s("line", { class: "now", x1: xn, x2: xn, y1: AXIS_H - 6, y2: height }));
  }
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
  if (!name || !S.graph || !(name in S.graph.tasks)) {
    body.replaceChildren(h("p", { class: "muted" }, "Select a task in the graph or timeline."));
    return;
  }
  const info = S.graph.tasks[name];
  const t = S.tasks.get(name) || { state: "PENDING", attempts: 0 };
  const attempts = S.attempts.filter((a) => a.task === name).sort((a, b) => a.attempt - b.attempt);
  const downstream = Object.entries(S.graph.tasks).filter(([, i]) => i.after.includes(name)).map(([n]) => n);
  const took = t.started_at && t.finished_at ? t.finished_at - t.started_at : null;

  const link = (n) => h("button", { type: "button", class: "link", onclick: () => select(n) }, n);
  const parts = [
    h("h3", {}, name),
    h("div", {}, pill(t.state), " ", h("span", { class: "muted" }, `${info.kind} worker${info.optional ? " · optional step" : ""}`)),
    info.description ? [h("div", { class: "section-title" }, "What this step does"), h("p", { class: "desc" }, info.description)] : null,
    t.preview ? [h("div", { class: "section-title" }, t.from_cache ? "Result (reused from the previous run)" : "Result"), h("pre", { class: "preview box" }, t.preview)] : null,
    t.sample !== null && t.sample !== undefined && typeof t.sample === "object" ? [
      h("div", { class: "section-title" }, "Sample output"), renderSample(t.sample, t.item_count),
    ] : null,
    h("dl", { class: "kv" },
      h("dt", {}, "Attempts"), h("dd", {}, String(t.attempts || 0)),
      h("dt", {}, "Duration"), h("dd", {}, took === null ? "–" : fmtDur(took)),
      h("dt", {}, "Items"), h("dd", {}, t.item_count == null ? "–" : String(t.item_count)),
      h("dt", {}, "Started"), h("dd", {}, fmtClock(t.started_at)),
      h("dt", {}, "Saved"), h("dd", {}, t.from_cache ? "reused from the previous run" : t.has_result ? "yes, kept for resume" : t.state === "SUCCESS" ? "no (reruns on resume)" : "–"),
      h("dt", {}, "Upstream"), h("dd", {}, info.after.length ? h("div", { class: "links" }, info.after.map(link)) : "none (root)"),
      h("dt", {}, "Downstream"), h("dd", {}, downstream.length ? h("div", { class: "links" }, downstream.map(link)) : "none (sink)"),
    ),
    t.error && t.state !== "SUCCESS" ? [
      h("div", { class: "section-title" }, t.state === "SKIPPED" ? "Why it was skipped" : "What went wrong"),
      h("pre", { class: "error" }, firstLine(t.error)),
      t.error.includes("\n") ? h("details", { class: "tech" }, h("summary", {}, "Technical details (Python traceback)"), h("pre", { class: "error" }, t.error)) : null,
    ] : null,
    t.state === "FAILED" && info.optional ? h("p", { class: "desc" }, "This step is optional, so the job carried on without it.") : null,
    attempts.length ? h("div", { class: "section-title" }, "Attempts") : null,
    attempts.map((a) => h("div", { class: "attempt" },
      h("div", { class: "attempt-head" },
        h("span", {}, `#${a.attempt} `, pill(a.state)),
        h("span", { class: "muted" }, `${a.worker || "timed out"} · ${fmtDur(a.finished_at - a.started_at)}`)),
      a.error ? h("pre", { class: "error" }, firstLine(a.error)) : null)),
  ];
  body.replaceChildren(...parts.flat(Infinity).filter(Boolean));
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
  return h("pre", { class: "preview box" }, JSON.stringify(sample, null, 2));
}

function renderProblems() {
  const box = $("run-problems");
  const problems = [...S.tasks.values()].filter((task) => task.state === "FAILED" || task.state === "SKIPPED");
  box.hidden = problems.length === 0;
  if (!problems.length) return;
  $("run-problems-body").replaceChildren(...problems.map((task) =>
    h("div", { class: "problem-row" },
      h("button", { type: "button", class: "link", onclick: () => select(task.task) }, task.task),
      pill(task.state),
      h("span", {}, firstLine(task.error) || (task.state === "SKIPPED" ? "Skipped after an earlier failure" : "Step failed")))));
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
    ? `Each one is a real job working with ${EX.offline ? "saved real data (offline mode)" : "live public data"}. Pick one and watch it run.`
    : "Run one from a terminal with the command shown on its card.";
  $("example-cards").replaceChildren(...EX.list.map((ex) => {
    const k = ex.kinds;
    const isRunning = busy === ex.name;
    const foot = EX.allowRun
      ? h("button", {
          type: "button", class: "run-btn", disabled: busy ? true : null,
          title: busy && !isRunning ? `Wait for ${busy} to finish` : `Start the ${ex.title.toLowerCase()} now`,
          onclick: () => runExample(ex.name),
        }, isRunning ? "Running…" : "▶ Run")
      : h("code", {}, `miniflow example ${ex.name} --ui`);
    return h("article", { class: "card" },
      h("h3", {}, ex.title),
      h("p", {}, ex.description),
      h("div", { class: "shape-line" }, `${ex.tasks} steps · ${k.io} io · ${k.cpu} cpu · ${k.async} async`),
      h("div", { class: "card-foot" }, foot, EX.offline ? h("span", { class: "badge-soft" }, "offline data") : null));
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
  $("gallery").open = false; // make room for the run
  setTimeout(loadRuns, 400);
  setTimeout(loadExamples, 400);
}

// ------------------------------------------------------- story & outcome
function renderStory() {
  const about = (S.graph && S.graph.about) || {};
  const box = $("story");
  box.hidden = !about.title;
  if (!about.title) return;
  $("story-title").textContent = about.title;
  $("story-text").textContent = about.description || "";
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
  const shown = chosen.length ? chosen : names.filter((n) => !used.has(n));
  const items = shown.map((n) => {
    const t = S.tasks.get(n) || { state: "PENDING" };
    const info = S.graph.tasks[n];
    let text;
    if (t.state === "SUCCESS") text = t.preview || "Done.";
    else if (t.state === "FAILED" || t.state === "SKIPPED") text = firstLine(t.error) || t.state.toLowerCase();
    else text = "Not finished yet: the result appears here when this step is done.";
    return h("div", { class: "outcome-item" },
      h("div", { class: "head" },
        h("button", { type: "button", class: "link", onclick: () => select(n) }, n),
        pill(t.state)),
      info.description ? h("p", { class: "desc muted" }, info.description) : null,
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
  $("ctl-cancel").addEventListener("click", () => control("cancel", {}));
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
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderTimeline, 150);
  });
}

init();
